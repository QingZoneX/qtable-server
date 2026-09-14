from __future__ import annotations

from types import SimpleNamespace

import pytest
from graphql import GraphQLError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register ORM relationships
from app.api import auth as auth_api
from app.api.graphql.mutations import workspace as workspace_mutations
from app.api.graphql.types import WorkspaceMemberInfo
from app.core.config import settings
from app.models.password_reset import PasswordResetToken
from app.models.smart_table import WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole


@pytest.fixture
async def invite_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Workspace.__table__.create)
        await connection.run_sync(WorkspaceMember.__table__.create)
        await connection.run_sync(WorkspaceItem.__table__.create)
        await connection.run_sync(PasswordResetToken.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        owner = User(
            id=931001,
            email="workspace-owner@example.test",
            name="Workspace Owner",
            password_hash="!test",
        )
        workspace = Workspace(id="ws-invite-security", name="Invite Security")
        session.add_all([owner, workspace])
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=owner.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.owner,
                ),
                WorkspaceItem(
                    id="root-invite-security",
                    workspace_id=workspace.id,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
            ]
        )
        await session.commit()
        yield session, owner, workspace
    await engine.dispose()


def _info(db, owner):
    return SimpleNamespace(context={"db": db, "user": owner})


@pytest.fixture(autouse=True)
def invite_security_defaults(monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    monkeypatch.setattr(settings, "SMTP_FROM", None)


@pytest.mark.asyncio
async def test_new_user_invite_without_smtp_fails_before_creating_identity(invite_db):
    db, owner, workspace = invite_db

    with pytest.raises(GraphQLError, match="delivery is not configured"):
        await workspace_mutations.WorkspaceMutations().inviteUserToWorkspace(
            _info(db, owner),
            "new-member@example.test",
            workspace.id,
            "viewer",
        )

    invited = (
        await db.execute(select(User).where(User.email == "new-member@example.test"))
    ).scalars().first()
    assert invited is None
    tokens = (await db.execute(select(PasswordResetToken))).scalars().all()
    assert tokens == []


@pytest.mark.asyncio
async def test_new_user_invite_smtp_failure_rolls_back_user_member_and_token(
    invite_db, monkeypatch
):
    db, owner, workspace = invite_db
    owner_id = owner.id
    workspace_id = workspace.id
    captured: list[str] = []
    monkeypatch.setattr(workspace_mutations, "_smtp_configured", lambda: True)

    def fail_delivery(_email: str, token: str) -> bool:
        captured.append(token)
        return False

    monkeypatch.setattr(workspace_mutations, "_send_reset_email", fail_delivery)

    with pytest.raises(GraphQLError, match="delivery failed"):
        await workspace_mutations.WorkspaceMutations().inviteUserToWorkspace(
            _info(db, owner),
            "failed-member@example.test",
            workspace_id,
            "editor",
        )

    assert len(captured) == 1
    invited = (
        await db.execute(select(User).where(User.email == "failed-member@example.test"))
    ).scalars().first()
    assert invited is None
    assert (await db.execute(select(PasswordResetToken))).scalars().all() == []
    members = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id != owner_id,
            )
        )
    ).scalars().all()
    assert members == []


@pytest.mark.asyncio
async def test_new_user_invite_delivers_raw_token_only_to_smtp_and_persists_hash(
    invite_db, monkeypatch
):
    db, owner, workspace = invite_db
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(workspace_mutations, "_smtp_configured", lambda: True)

    def successful_delivery(email: str, token: str) -> bool:
        delivered.append((email, token))
        return True

    monkeypatch.setattr(workspace_mutations, "_send_reset_email", successful_delivery)

    result = await workspace_mutations.WorkspaceMutations().inviteUserToWorkspace(
        _info(db, owner),
        "delivered-member@example.test",
        workspace.id,
        "viewer",
    )

    assert result.email == "delivered-member@example.test"
    assert not hasattr(result, "reset_token")
    assert len(delivered) == 1
    email, raw_token = delivered[0]
    assert email == result.email

    invited = (
        await db.execute(select(User).where(User.email == result.email))
    ).scalars().one()
    membership = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == invited.id,
            )
        )
    ).scalars().one()
    assert membership.role == WorkspaceRole.viewer

    token = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == invited.id)
        )
    ).scalars().one()
    assert token.token_hash == auth_api._hash_reset_token(raw_token)
    assert token.token_hash != raw_token

    schema_fields = {
        field.graphql_name or field.python_name
        for field in WorkspaceMemberInfo.__strawberry_definition__.fields
    }
    assert "resetToken" not in schema_fields
    assert "reset_token" not in schema_fields


@pytest.mark.asyncio
async def test_existing_user_invite_does_not_require_smtp_or_mint_reset_token(invite_db):
    db, owner, workspace = invite_db
    existing = User(
        id=931002,
        email="existing-member@example.test",
        name="Existing Member",
        password_hash="!existing",
    )
    db.add(existing)
    await db.commit()

    result = await workspace_mutations.WorkspaceMutations().inviteUserToWorkspace(
        _info(db, owner),
        existing.email,
        workspace.id,
        "editor",
    )

    assert result.user_id == existing.id
    membership = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == existing.id,
            )
        )
    ).scalars().one()
    assert membership.role == WorkspaceRole.editor
    assert (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == existing.id)
        )
    ).scalars().all() == []
