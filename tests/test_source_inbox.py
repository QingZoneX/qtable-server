from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.change_history import ChangeSet
from app.models.source_inbox import SourceInboxItem
from app.models.smart_table import TableField, TableRecord, TableRowPermissionPolicy, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.source_inbox import SourceInboxConvertRequest, SourceInboxIngestRequest, SourceInboxPreviewRequest, SourceInboxStatusRequest
from app.services.source_inbox import SourceInboxError, source_inbox_service


@pytest.fixture
async def inbox_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source-inbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([
            User(id=1, email="alice@example.test", password_hash="test", name="Alice"),
            User(id=2, email="bob@example.test", password_hash="test", name="Bob"),
            User(id=3, email="third@example.test", password_hash="test", name="Third"),
            Workspace(id="w1", name="Workspace"),
            WorkspaceMember(user_id=1, workspace_id="w1", role=WorkspaceRole.owner),
            WorkspaceMember(user_id=2, workspace_id="w1", role=WorkspaceRole.viewer),
            WorkspaceItem(id="root", workspace_id="w1", type="folder", name="Root", parent_id=None, order_index=0),
            WorkspaceItem(id="tasks", workspace_id="w1", type="table", name="项目任务", parent_id="root", order_index=1, default_view_id="v1"),
            TableField(id="title", table_id="tasks", name="任务名称", type="text", order_index=0),
            TableField(id="priority", table_id="tasks", name="优先级", type="select", options=[{"id":"low","label":"低"},{"id":"medium","label":"中"},{"id":"high","label":"高"},{"id":"urgent","label":"紧急"}], order_index=1),
            TableField(id="owner", table_id="tasks", name="负责人", type="member", property={"multiple":False}, order_index=2),
            TableView(id="v1", table_id="tasks", name="Grid", type="grid", config={}),
            TableRecord(id="visible", table_id="tasks", data={"title":"完善登录页面","priority":"high","owner":"1"}, order_index=1, created_by_user_id=1, version=1),
            TableRecord(id="other", table_id="tasks", data={"title":"修复另一个登录缺陷","priority":"urgent","owner":"2"}, order_index=2, created_by_user_id=2, version=1),
        ])
        await db.commit()
        yield db
    await engine.dispose()


def ingest_request(source_id="qnote-1", **updates):
    source={"sourceId":source_id,"sourceType":"qnote","url":"https://example.com/spec","pageTitle":"登录改造设计","quote":"需要完善登录页面并补充错误提示","annotation":"整理为开发任务","tags":["登录","前端"]}
    source.update(updates)
    return SourceInboxIngestRequest.model_validate({"workspaceId":"w1","source":source})


@pytest.mark.asyncio
async def test_ingest_idempotent_and_conflict_safe(inbox_db):
    first=await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request())
    second=await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request())
    assert not first["idempotent"] and second["idempotent"]
    assert first["item"]["id"]==second["item"]["id"]
    with pytest.raises(SourceInboxError,match="different content"):
        await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request(annotation="另一条内容"))


@pytest.mark.asyncio
async def test_duplicate_content_gets_warning_state(inbox_db):
    first=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request("a")))["item"]
    second=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request("b")))["item"]
    assert second["status"]=="duplicate" and second["duplicateOfId"]==first["id"]


@pytest.mark.asyncio
async def test_preview_never_creates_task_and_respects_row_visibility(inbox_db):
    item=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request()))["item"]
    before=int((await inbox_db.execute(select(func.count()).select_from(TableRecord).where(TableRecord.table_id=="tasks"))).scalar() or 0)
    inbox_db.add(TableRowPermissionPolicy(table_id="tasks",mode="creator",member_field_id=None))
    await inbox_db.commit()
    preview=await source_inbox_service.preview(inbox_db,user_id=1,request=SourceInboxPreviewRequest.model_validate({"itemId":item["id"],"targetTableId":"tasks"}))
    after=int((await inbox_db.execute(select(func.count()).select_from(TableRecord).where(TableRecord.table_id=="tasks"))).scalar() or 0)
    assert before==after
    assert all(candidate["recordId"]!="other" for candidate in preview["similarTasks"])
    assert {member["userId"] for member in preview["members"]}=={1,2}


@pytest.mark.asyncio
async def test_confirmed_conversion_keeps_source_and_audit(inbox_db):
    item=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request()))["item"]
    request=SourceInboxConvertRequest.model_validate({"itemId":item["id"],"targetTableId":"tasks","title":"整理登录改造需求","description":"按批注执行","priority":"high","assigneeUserId":1,"workloadHours":6})
    first=await source_inbox_service.convert(inbox_db,user_id=1,request=request)
    second=await source_inbox_service.convert(inbox_db,user_id=1,request=request)
    assert first["item"]["status"]=="converted" and second["idempotent"]
    record=await inbox_db.get(TableRecord,{"id":first["task"]["recordId"],"table_id":"tasks"})
    fields=(await inbox_db.execute(select(TableField).where(TableField.table_id=="tasks"))).scalars().all()
    by_name={field.name:field for field in fields}
    assert record.data[by_name["来源ID"].id]=="qnote-1"
    assert record.data[by_name["来源链接"].id]=="https://example.com/spec"
    assert record.data["owner"]=="1"
    change=await inbox_db.get(ChangeSet,first["task"]["changeSetId"])
    assert change.operation=="source_inbox_convert" and change.trace_id==item["id"]


@pytest.mark.asyncio
async def test_assignee_and_project_permissions_are_enforced(inbox_db):
    item=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request()))["item"]
    with pytest.raises(SourceInboxError,match="current workspace member"):
        await source_inbox_service.convert(inbox_db,user_id=1,request=SourceInboxConvertRequest.model_validate({"itemId":item["id"],"targetTableId":"tasks","title":"任务","assigneeUserId":3}))
    await inbox_db.rollback()
    bob=(await source_inbox_service.ingest(inbox_db,user_id=2,request=ingest_request("bob-note")))["item"]
    with pytest.raises(PermissionError,match="edit permission"):
        await source_inbox_service.convert(inbox_db,user_id=2,request=SourceInboxConvertRequest.model_validate({"itemId":bob["id"],"targetTableId":"tasks","title":"任务"}))
    await inbox_db.rollback()


@pytest.mark.asyncio
async def test_batch_archive_and_user_isolation(inbox_db):
    a=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request("a")))["item"]
    b=(await source_inbox_service.ingest(inbox_db,user_id=1,request=ingest_request("b",quote="另一条资料")))["item"]
    result=await source_inbox_service.update_status(inbox_db,user_id=1,request=SourceInboxStatusRequest.model_validate({"itemIds":[a["id"],b["id"]],"status":"archived"}))
    assert result["updatedCount"]==2
    page=await source_inbox_service.list_items(inbox_db,user_id=1,workspace_id="w1",status="archived")
    assert page["totalCount"]==2
    other=await source_inbox_service.list_items(inbox_db,user_id=2,workspace_id="w1")
    assert other["totalCount"]==0


def test_graphql_schema_exposes_source_inbox_contract():
    text=schema.as_str()
    for token in ["sourceInboxItems(","sourceInboxItem(","ingestSourceInboxItem(","previewSourceInboxItem(","convertSourceInboxItem(","updateSourceInboxStatus("]:
        assert token in text
