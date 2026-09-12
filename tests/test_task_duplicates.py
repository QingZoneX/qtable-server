from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
    WorkspaceItemPermission,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.task_duplicates import (
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    MINIMUM_CANDIDATE_SCORE,
    DuplicateSubject,
    TaskDuplicateScorer,
    canonicalize_url,
    task_duplicate_service,
)


@pytest.fixture
async def duplicate_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'task-duplicates.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                User(id=1, email="alice@example.test", password_hash="test", name="Alice"),
                User(id=2, email="bob@example.test", password_hash="test", name="Bob"),
                User(id=3, email="carol@example.test", password_hash="test", name="Carol"),
                Workspace(id="w1", name="Workspace"),
                WorkspaceMember(
                    user_id=1,
                    workspace_id="w1",
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=2,
                    workspace_id="w1",
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=3,
                    workspace_id="w1",
                    role=WorkspaceRole.viewer,
                ),
                WorkspaceItem(
                    id="root",
                    workspace_id="w1",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="tasks",
                    workspace_id="w1",
                    type="table",
                    name="项目任务",
                    parent_id="root",
                    order_index=1,
                    default_view_id="v1",
                ),
                WorkspaceItemPermission(
                    item_id="tasks",
                    user_id=3,
                    permission="update",
                ),
                TableField(
                    id="title",
                    table_id="tasks",
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="description",
                    table_id="tasks",
                    name="任务描述",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="status",
                    table_id="tasks",
                    name="状态",
                    type="select",
                    options=[
                        {"id": "doing", "label": "进行中"},
                        {"id": "done", "label": "已完成"},
                    ],
                    order_index=2,
                ),
                TableField(
                    id="owner",
                    table_id="tasks",
                    name="负责人",
                    type="member",
                    property={"multiple": False},
                    order_index=3,
                ),
                TableField(
                    id="source_url",
                    table_id="tasks",
                    name="来源链接",
                    type="url",
                    order_index=4,
                ),
                TableView(
                    id="v1",
                    table_id="tasks",
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableRecord(
                    id="visible-semantic",
                    table_id="tasks",
                    data={
                        "title": "优化登录首页首次加载速度",
                        "description": "减少用户登录后首屏等待耗时",
                        "status": "doing",
                        "owner": "1",
                        "source_url": "https://example.com/spec?utm_source=test",
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="hidden-semantic",
                    table_id="tasks",
                    data={
                        "title": "提升登录主页首屏性能",
                        "description": "降低首次加载延迟",
                        "status": "doing",
                        "owner": "2",
                    },
                    order_index=2,
                    created_by_user_id=2,
                    version=1,
                ),
                TableRecord(
                    id="completed",
                    table_id="tasks",
                    data={
                        "title": "修复登录异常通知",
                        "description": "解决登录报错消息提示",
                        "status": "done",
                        "owner": "1",
                    },
                    order_index=3,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="unrelated",
                    table_id="tasks",
                    data={
                        "title": "编写财务月报",
                        "description": "整理本月财务数据",
                        "status": "doing",
                        "owner": "1",
                    },
                    order_index=4,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRowPermissionPolicy(
                    table_id="tasks",
                    mode="creator",
                    member_field_id=None,
                ),
            ]
        )
        await db.commit()
        yield db
    await engine.dispose()


def test_thresholds_align_with_cross_product_contract():
    assert HIGH_THRESHOLD == 0.85
    assert MEDIUM_THRESHOLD == 0.65
    assert MINIMUM_CANDIDATE_SCORE == 0.45
    assert task_duplicate_service.thresholds() == {
        "high": 0.85,
        "medium": 0.65,
        "minimumCandidate": 0.45,
    }


def test_semantic_aliases_recall_synonymous_task_wording():
    scorer = TaskDuplicateScorer()
    result = scorer.compare(
        DuplicateSubject(
            title="提升登录主页首屏性能",
            content="降低首次加载延迟",
        ),
        DuplicateSubject(
            title="优化登录首页首次加载速度",
            content="减少用户登录后首屏等待耗时",
        ),
    )
    assert result.score >= HIGH_THRESHOLD
    assert result.match_type in {"exact_content", "semantic"}


def test_url_normalization_ignores_tracking_and_fragments():
    assert canonicalize_url(
        "HTTPS://Example.com/spec/?utm_source=qnote&b=2&a=1#selection"
    ) == "https://example.com/spec?a=1&b=2"


@pytest.mark.asyncio
async def test_scan_table_is_permission_safe_and_explainable(duplicate_db):
    result = await task_duplicate_service.scan_table(
        duplicate_db,
        user_id=1,
        table_id="tasks",
        title="提升登录主页首屏性能",
        description="降低首次加载延迟",
        source_url="https://example.com/spec?gclid=abc",
    )
    ids = {candidate["recordId"] for candidate in result["candidates"]}
    assert "visible-semantic" in ids
    assert "hidden-semantic" not in ids
    assert "unrelated" not in ids
    candidate = next(
        item for item in result["candidates"] if item["recordId"] == "visible-semantic"
    )
    assert candidate["thresholdBand"] == "high"
    assert candidate["matchType"] in {"exact_source", "semantic", "exact_content"}
    assert candidate["reason"]
    assert candidate["similarFragment"]
    assert candidate["deepLink"].endswith("?recordId=visible-semantic")
    assert candidate["requiresReview"] is True
    assert candidate["blocksCreation"] is False
    assert result["permissionScope"] == {
        "rowVisibilityApplied": True,
        "hiddenRecordsExcluded": True,
        "advisoryOnly": True,
    }


@pytest.mark.asyncio
async def test_manual_preview_matches_existing_update_permission(duplicate_db):
    result = await task_duplicate_service.scan_table(
        duplicate_db,
        user_id=3,
        table_id="tasks",
        title="新增任务",
        description="手工创建前查重",
    )
    # Creator row visibility still applies: update-only access does not expose
    # rows created by other users, but the user can run the same preview gate
    # that precedes an ordinary insert-row mutation.
    assert result["candidates"] == []
    assert result["permissionScope"]["rowVisibilityApplied"] is True

    with pytest.raises(PermissionError):
        await task_duplicate_service.scan_plan(
            duplicate_db,
            user_id=3,
            table_id="tasks",
            plan={
                "root": {
                    "key": "1",
                    "title": "计划",
                    "children": [
                        {"key": "1.1", "title": "子任务", "children": []}
                    ],
                }
            },
        )


@pytest.mark.asyncio
async def test_completed_duplicate_is_visible_but_never_blocks(duplicate_db):
    result = await task_duplicate_service.scan_table(
        duplicate_db,
        user_id=1,
        table_id="tasks",
        title="解决登录报错提醒",
        description="修正认证异常消息",
    )
    candidate = next(
        item for item in result["candidates"] if item["recordId"] == "completed"
    )
    assert candidate["completed"] is True
    assert candidate["requiresReview"] is False
    assert candidate["blocksCreation"] is False
    assert candidate["recommendedAction"] == "review"


@pytest.mark.asyncio
async def test_task_planning_nodes_reuse_the_same_detector(duplicate_db):
    duplicates = await task_duplicate_service.scan_plan(
        duplicate_db,
        user_id=1,
        table_id="tasks",
        plan={
            "root": {
                "key": "1",
                "title": "登录体验改造",
                "children": [
                    {
                        "key": "1.1",
                        "title": "提升登录主页首屏性能",
                        "description": "降低首次加载延迟",
                        "tags": ["登录", "性能"],
                        "children": [],
                    },
                    {
                        "key": "1.2",
                        "title": "整理季度采购计划",
                        "description": "汇总采购清单",
                        "children": [],
                    },
                ],
            }
        },
    )
    assert "1.1" in duplicates
    assert any(
        candidate["recordId"] == "visible-semantic"
        for candidate in duplicates["1.1"]
    )
    assert all(
        candidate["recordId"] != "hidden-semantic"
        for candidates in duplicates.values()
        for candidate in candidates
    )
    assert "1.2" not in duplicates


def test_graphql_schema_exposes_manual_duplicate_preview_contract():
    text = schema.as_str()
    assert "taskDuplicateCandidates(" in text
    assert "sourceQuote:" in text
    assert "excludeRecordIds:" in text
