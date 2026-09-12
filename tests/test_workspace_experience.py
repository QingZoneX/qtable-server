from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from graphql import GraphQLError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.api.graphql.mutations.workspace_experience import WorkspaceExperienceMutations
from app.api.graphql.queries.workspace_experience import WorkspaceExperienceQueries
from app.core.config import settings
from app.db.base import Base
from app.models.smart_table import TableRecord, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.workspace_experience import (
    WorkspaceExperienceError,
    get_workspace_experience,
    set_user_experience_mode,
    set_workspace_default_experience_mode,
)


OWNER_ID = 160001
EDITOR_ID = 160002
OUTSIDER_ID = 160003
WORKSPACE_ID = "ws-experience-test"
TABLE_ID = "tbl-experience-test"
VIEW_ID = "view-experience-test"
RECORD_ID = "record-experience-test"


@pytest_asyncio.fixture
async def experience_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workspace-experience.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(
                    id=OWNER_ID,
                    email="owner-experience@example.test",
                    name="Owner",
                    password_hash="!test",
                ),
                User(
                    id=EDITOR_ID,
                    email="editor-experience@example.test",
                    name="Editor",
                    password_hash="!test",
                ),
                User(
                    id=OUTSIDER_ID,
                    email="outsider-experience@example.test",
                    name="Outsider",
                    password_hash="!test",
                ),
                Workspace(id=WORKSPACE_ID, name="Experience Test"),
                WorkspaceItem(
                    id="root-experience-test",
                    workspace_id=WORKSPACE_ID,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=TABLE_ID,
                    workspace_id=WORKSPACE_ID,
                    type="table",
                    name="Tasks",
                    parent_id="root-experience-test",
                    order_index=1,
                    default_view_id=VIEW_ID,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=OWNER_ID,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.owner,
                ),
                WorkspaceMember(
                    user_id=EDITOR_ID,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                TableView(
                    id=VIEW_ID,
                    table_id=TABLE_ID,
                    name="Grid",
                    type="grid",
                    config={"toolbar": {"items": ["fields", "filter"]}},
                ),
                TableRecord(
                    id=RECORD_ID,
                    table_id=TABLE_ID,
                    data={"title": "Stable business row"},
                    order_index=0,
                    created_by_user_id=OWNER_ID,
                    version=7,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _load_record(session):
    return (
        await session.execute(
            select(TableRecord).where(
                TableRecord.id == RECORD_ID,
                TableRecord.table_id == TABLE_ID,
            )
        )
    ).scalars().one()


async def _load_view(session):
    return (
        await session.execute(
            select(TableView).where(
                TableView.id == VIEW_ID,
                TableView.table_id == TABLE_ID,
            )
        )
    ).scalars().one()


@pytest.mark.asyncio
async def test_workspace_default_and_member_override_resolution(experience_db):
    owner = await get_workspace_experience(
        experience_db,
        user_id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
    )
    editor = await get_workspace_experience(
        experience_db,
        user_id=EDITOR_ID,
        workspace_id=WORKSPACE_ID,
    )

    assert owner["workspaceDefaultMode"] == "simple"
    assert owner["userMode"] is None
    assert owner["effectiveMode"] == "simple"
    assert owner["followsWorkspaceDefault"] is True
    assert owner["canManageWorkspaceDefault"] is True
    assert editor["effectiveMode"] == "simple"
    assert editor["canManageWorkspaceDefault"] is False

    editor = await set_user_experience_mode(
        experience_db,
        user_id=EDITOR_ID,
        workspace_id=WORKSPACE_ID,
        mode=" ADVANCED ",
    )
    owner = await get_workspace_experience(
        experience_db,
        user_id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert editor["userMode"] == "advanced"
    assert editor["effectiveMode"] == "advanced"
    assert owner["userMode"] is None
    assert owner["effectiveMode"] == "simple"

    owner = await set_workspace_default_experience_mode(
        experience_db,
        user_id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
        mode="advanced",
    )
    assert owner["workspaceDefaultMode"] == "advanced"
    assert owner["effectiveMode"] == "advanced"

    editor = await set_user_experience_mode(
        experience_db,
        user_id=EDITOR_ID,
        workspace_id=WORKSPACE_ID,
        mode="simple",
    )
    assert editor["effectiveMode"] == "simple"
    assert editor["followsWorkspaceDefault"] is False

    editor = await set_user_experience_mode(
        experience_db,
        user_id=EDITOR_ID,
        workspace_id=WORKSPACE_ID,
        mode=None,
    )
    assert editor["userMode"] is None
    assert editor["effectiveMode"] == "advanced"
    assert editor["followsWorkspaceDefault"] is True


@pytest.mark.asyncio
async def test_workspace_default_is_owner_only_and_outsider_fails_closed(experience_db):
    with pytest.raises(PermissionError, match="Only workspace owner"):
        await set_workspace_default_experience_mode(
            experience_db,
            user_id=EDITOR_ID,
            workspace_id=WORKSPACE_ID,
            mode="advanced",
        )
    await experience_db.rollback()

    with pytest.raises(PermissionError, match="no access"):
        await get_workspace_experience(
            experience_db,
            user_id=OUTSIDER_ID,
            workspace_id=WORKSPACE_ID,
        )

    payload = await get_workspace_experience(
        experience_db,
        user_id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert payload["workspaceDefaultMode"] == "simple"


@pytest.mark.asyncio
async def test_invalid_mode_is_rejected_without_persisting(experience_db):
    with pytest.raises(WorkspaceExperienceError, match="simple.*advanced"):
        await set_user_experience_mode(
            experience_db,
            user_id=EDITOR_ID,
            workspace_id=WORKSPACE_ID,
            mode="expert",
        )
    await experience_db.rollback()

    member = (
        await experience_db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == EDITOR_ID,
                WorkspaceMember.workspace_id == WORKSPACE_ID,
            )
        )
    ).scalars().one()
    assert member.experience_mode is None


@pytest.mark.asyncio
async def test_preference_changes_do_not_mutate_table_view_or_record_data(experience_db):
    record_before = await _load_record(experience_db)
    view_before = await _load_view(experience_db)
    before_data = dict(record_before.data)
    before_version = record_before.version
    before_view_config = dict(view_before.config or {})

    await set_user_experience_mode(
        experience_db,
        user_id=EDITOR_ID,
        workspace_id=WORKSPACE_ID,
        mode="advanced",
    )
    await set_workspace_default_experience_mode(
        experience_db,
        user_id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
        mode="advanced",
    )

    experience_db.expire_all()
    record_after = await _load_record(experience_db)
    view_after = await _load_view(experience_db)
    assert record_after.data == before_data
    assert record_after.version == before_version
    assert view_after.config == before_view_config


@pytest.mark.asyncio
async def test_graphql_contract_exposes_effective_mode_and_enforces_owner(experience_db):
    owner = await experience_db.get(User, OWNER_ID)
    editor = await experience_db.get(User, EDITOR_ID)
    assert owner is not None
    assert editor is not None

    queries = WorkspaceExperienceQueries()
    mutations = WorkspaceExperienceMutations()
    owner_info = SimpleNamespace(context={"db": experience_db, "user": owner})
    editor_info = SimpleNamespace(context={"db": experience_db, "user": editor})

    initial = await queries.workspace_experience_preference(
        owner_info,
        workspace_id=WORKSPACE_ID,
    )
    assert initial["persistent"] is True
    assert initial["effectiveMode"] == "simple"

    personal = await mutations.set_my_workspace_experience_mode(
        editor_info,
        workspace_id=WORKSPACE_ID,
        mode="advanced",
    )
    assert personal["effectiveMode"] == "advanced"
    assert personal["canManageWorkspaceDefault"] is False

    with pytest.raises(GraphQLError, match="Only workspace owner"):
        await mutations.set_workspace_default_experience_mode_mutation(
            editor_info,
            workspace_id=WORKSPACE_ID,
            mode="advanced",
        )

    schema_text = schema.as_str()
    assert "workspaceExperiencePreference" in schema_text
    assert "setMyWorkspaceExperienceMode" in schema_text
    assert "setWorkspaceDefaultExperienceMode" in schema_text
