"""Small REST API used by the NoteScript Clipper extension.

This API intentionally uses the same email/password JWT as the web app.  It
keeps the extension usable against a local HTTP QTable server without making
the extension depend on OAuth redirects or an HTTPS deployment.
"""
from typing import Any, Optional
from uuid import uuid4
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.smart_table import TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.models.clipper_integration import ClipperTaskReceipt
from app.services.smart_table_store import get_full_store, update_record
from app.services.workspace import (
    ensure_user_default_workspace,
    get_effective_permission_for_item,
    permission_allows,
)
from app.core.config import settings
from app.services.row_permissions import (
    filter_store_for_user,
    require_record_access,
)
from app.services.relation_engine import RelationValidationError
from app.services.relation_store import normalize_relation_payload_db

router = APIRouter(prefix="/api/clipper", tags=["clipper"])


def _task_url(table_id: str, record_id: str) -> str:
    return (
        f"{settings.QTABLE_WEB_URL.rstrip('/')}/table/{table_id}"
        f"?row={record_id}"
    )


class CreateClipperTask(BaseModel):
    annotation_id: str
    annotation_ids: list[str] = Field(default_factory=list)
    title: str = Field(min_length=1, max_length=200)
    note: str = ""
    selected_text: str = ""
    page_url: str = ""
    page_title: str = ""
    mode: str = "highlight"
    screenshot_data_url: Optional[str] = None
    assignee_email: Optional[str] = None
    due_date: Optional[str] = None
    target_table_id: str = Field(min_length=1)
    include_context_url: bool = True
    client_mutation_id: Optional[str] = Field(default=None, max_length=128)
    source: Optional[dict[str, Any]] = None
    qnote_url: Optional[str] = None
    screenshot_url: Optional[str] = None


class UpdateClipperTaskStatus(BaseModel):
    target_table_id: str = Field(min_length=1)
    status_field_id: Optional[str] = None
    value: str = Field(min_length=1, max_length=100)


DEFAULT_STATUS_OPTIONS = [
    {"id": "todo", "label": "待处理"},
    {"id": "in_progress", "label": "进行中"},
    {"id": "done", "label": "已完成"},
]


def _find_status_field(fields: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return next(
        (
            field
            for field in fields
            if any(
                keyword in str(field.get("name") or "").strip().lower()
                for keyword in ("任务状态", "状态", "status", "state")
            )
        ),
        None,
    )


def _status_options(field: dict[str, Any]) -> list[dict[str, str]]:
    options = field.get("options")
    if not isinstance(options, list):
        return DEFAULT_STATUS_OPTIONS.copy() if field.get("type") == "text" else []
    return [
        {"id": str(option.get("id")), "label": str(option.get("label", option.get("name", option.get("id"))))}
        for option in options
        if isinstance(option, dict) and option.get("id") is not None
    ]


def _default_status_value(field: dict[str, Any]) -> str:
    options = _status_options(field)
    for option in options:
        if option["label"] in {"待处理", "未开始", "待办", "open", "todo"}:
            return option["id"] if field.get("type") in {"select", "multiSelect"} else option["label"]
    if options:
        return options[0]["id"] if field.get("type") in {"select", "multiSelect"} else options[0]["label"]
    return "待处理"


def _status_meta(field: dict[str, Any], value: Optional[str] = None) -> dict[str, Any]:
    return {
        "field_id": field.get("id"),
        "field_name": field.get("name"),
        "field_type": field.get("type", "text"),
        "options": _status_options(field),
        "value": value,
    }


def _field_value(field: dict[str, Any], payload: CreateClipperTask) -> Any:
    name = str(field.get("name") or "").strip().lower()
    screenshot_url = payload.screenshot_url or payload.screenshot_data_url
    if screenshot_url and field.get("type") in {"attachment", "image"}:
        return [{
            "id": f"att_clip_{payload.annotation_id}",
            "name": f"网页批注-{payload.annotation_id[:8]}.png",
            "url": screenshot_url,
            "size": len(payload.screenshot_data_url or ""),
            "type": "image/png",
        }]
    exact = {
        "批注内容": payload.note,
        "原文摘录": payload.selected_text,
        "来源页面": payload.page_url,
        "批注类型": payload.mode,
        "批注编号": payload.annotation_id,
        "来源引用": json.dumps(payload.source or {}, ensure_ascii=False),
        "QNote回溯": payload.qnote_url or payload.page_url,
        "批注截图": [{
            "id": f"att_clip_{payload.annotation_id}",
            "name": f"网页批注-{payload.annotation_id[:8]}.png",
            "url": screenshot_url,
            "size": len(payload.screenshot_data_url or ""),
            "type": "image/png",
        }] if screenshot_url else None,
    }
    if name in exact:
        return exact[name]
    if any(x in name for x in ("标题", "任务名", "名称", "title", "name")):
        return payload.title
    if any(x in name for x in ("批注", "备注", "说明", "note", "comment", "description")):
        return payload.note
    if any(x in name for x in ("选中", "原文", "摘录", "quote", "selected")):
        return payload.selected_text
    if any(x in name for x in ("链接", "网址", "url", "link")):
        if any(x in name for x in ("qnote", "回溯", "source")):
            return payload.qnote_url or payload.page_url
        return payload.page_url if payload.include_context_url else ""
    if any(x in name for x in ("负责人", "指派", "assignee", "owner")):
        return payload.assignee_email or ""
    if any(x in name for x in ("截止", "到期", "due", "deadline")):
        return payload.due_date or ""
    if any(x in name for x in ("网页标题", "页面", "page")):
        return payload.page_title
    if any(x in name for x in ("类型", "方式", "批注类型", "annotation", "mode")):
        return payload.mode
    if any(x in name for x in ("批注id", "批注编号", "annotation_id")):
        return payload.annotation_id
    return None


def _request_fingerprint(payload: CreateClipperTask) -> str:
    body = payload.model_dump(mode="json", exclude={"screenshot_data_url"})
    if payload.screenshot_data_url:
        body["screenshot_sha256"] = hashlib.sha256(
            payload.screenshot_data_url.encode("utf-8")
        ).hexdigest()
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def _require_clipper_table_permission(
    db: AsyncSession,
    user: User,
    table_id: str,
    required: str,
) -> str:
    permission = await get_effective_permission_for_item(db, user.id, table_id)
    if not permission_allows(permission, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to target table",
        )
    return permission


async def _user_table_ids(
    db: AsyncSession, user_id: int, workspace_id: Optional[str] = None
) -> list[WorkspaceItem]:
    memberships = await db.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    )
    members = memberships.scalars().all()
    workspace_ids = [m.workspace_id for m in members]
    if workspace_id:
        if workspace_id not in workspace_ids:
            return []
        workspace_ids = [workspace_id]
    if not workspace_ids:
        return []
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id.in_(workspace_ids),
            WorkspaceItem.type == "table",
        ).order_by(WorkspaceItem.order_index)
    )
    return list(result.scalars().all())


@router.get("/tables")
async def list_clipper_tables(
    workspace_id: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    await ensure_user_default_workspace(db, user.id, user.name)
    tables = await _user_table_ids(db, user.id, workspace_id)
    result = []
    for table in tables:
        permission = await get_effective_permission_for_item(db, user.id, table.id)
        if not permission_allows(permission, "read"):
            continue
        store = await get_full_store(db, table.id)
        visible_store, _ = await filter_store_for_user(
            db,
            table.id,
            store,
            user_id=user.id,
            table_permission=permission,
        )
        result.append({
            "id": table.id,
            "name": table.name,
            "emoji": None,
            "row_count": len(visible_store.get("records", [])),
            "workspace_id": table.workspace_id,
        })
    return result


@router.get("/users")
async def list_clipper_users(
    workspace_id: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return selectable members from workspaces the current user belongs to."""
    memberships = await db.execute(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    )
    workspace_ids = [row[0] for row in memberships.all()]
    if workspace_id:
        if workspace_id not in workspace_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to workspace")
        workspace_ids = [workspace_id]
    if not workspace_ids:
        return []
    result = await db.execute(
        select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id.in_(workspace_ids))
        .order_by(User.name, User.email)
    )
    seen: set[int] = set()
    users: list[dict[str, Any]] = []
    for member in result.scalars().all():
        if member.id in seen:
            continue
        seen.add(member.id)
        users.append({"id": member.id, "name": member.name, "email": member.email})
    return users


@router.get("/context")
async def get_clipper_context(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Return the shared identity/workspace projection consumed by QNote."""
    await ensure_user_default_workspace(db, user.id, user.name)
    result = await db.execute(
        select(WorkspaceMember, Workspace.name)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.joined_at)
    )
    memberships = result.all()
    workspace_ids = [membership.workspace_id for membership, _ in memberships]
    members_by_workspace: dict[str, list[dict[str, Any]]] = {workspace_id: [] for workspace_id in workspace_ids}
    if workspace_ids:
        member_result = await db.execute(
            select(User, WorkspaceMember)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id.in_(workspace_ids))
            .order_by(User.name, User.email)
        )
        for member_user, member in member_result.all():
            members_by_workspace.setdefault(member.workspace_id, []).append({
                "id": member_user.id,
                "name": member_user.name,
                "email": member_user.email,
                "role": member.role.value if hasattr(member.role, "value") else str(member.role),
            })
    return {
        "user": {"id": user.id, "name": user.name, "email": user.email},
        "workspaces": [
            {
                "id": membership.workspace_id,
                "name": workspace_name,
                "role": membership.role.value
                if hasattr(membership.role, "value")
                else str(membership.role),
            }
            for membership, workspace_name in memberships
        ],
        "default_workspace_id": memberships[0][0].workspace_id if memberships else None,
        "members_by_workspace": members_by_workspace,
    }


@router.post("/tasks")
async def create_clipper_task(
    payload: CreateClipperTask,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if payload.screenshot_data_url and (
        not payload.screenshot_data_url.startswith("data:image/")
        or len(payload.screenshot_data_url) > 5 * 1024 * 1024
    ):
        raise HTTPException(status_code=400, detail="批注截图格式无效或超过 5MB")
    tables = await _user_table_ids(db, user.id)
    table = next((item for item in tables if item.id == payload.target_table_id), None)
    if table is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to target table")
    source_workspace_id = (payload.source or {}).get("workspaceId")
    if source_workspace_id and str(source_workspace_id) != table.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QNote annotation and target table must belong to the same workspace",
        )
    table_permission = await _require_clipper_table_permission(
        db,
        user,
        table.id,
        "update",
    )

    request_fingerprint = _request_fingerprint(payload)
    if payload.client_mutation_id:
        receipt_result = await db.execute(
            select(ClipperTaskReceipt).where(
                ClipperTaskReceipt.user_id == user.id,
                ClipperTaskReceipt.client_mutation_id == payload.client_mutation_id,
            )
        )
        receipt = receipt_result.scalars().first()
        if receipt:
            if receipt.request_fingerprint != request_fingerprint:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key was already used with a different request",
                )
            return dict(receipt.response)

    store = await get_full_store(db, table.id)
    store, _ = await filter_store_for_user(
        db,
        table.id,
        store,
        user_id=user.id,
        table_permission=table_permission,
    )
    fields = list(store.get("fields", []))
    status_field = _find_status_field(fields)
    if status_field is None:
        if not permission_allows(table_permission, "edit"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Target table requires schema changes but is not editable",
            )
        status_field = {
            "id": f"f_clip_status_{uuid4().hex[:8]}",
            "name": "任务状态",
            "type": "select",
            "options": DEFAULT_STATUS_OPTIONS,
        }
        from app.services.smart_table_store import add_field
        await add_field(db, table.id, status_field)
        fields.append(status_field)
    initial_status = _default_status_value(status_field)
    existing_id_field = next(
        (field for field in fields if str(field.get("name") or "").strip() in {"批注编号", "Annotation ID"}),
        None,
    )
    # Preserve legacy direct-Clipper retry behaviour only for callers that do
    # not provide a real operation key. A QNote annotation may legitimately
    # generate multiple tasks when each operation has a distinct key.
    if existing_id_field and not payload.client_mutation_id:
        for existing in store.get("records", []):
            if existing.get(existing_id_field["id"]) == payload.annotation_id:
                return {
                    "task_id": existing.get("id"),
                    "qtable_url": _task_url(table.id, str(existing.get("id"))),
                    "annotation_status": "task_created",
                    "target_table_id": table.id,
                    "status": _status_meta(status_field, existing.get(status_field["id"])),
                }
    required_fields = [
        ("批注内容", "text", "note"),
        ("原文摘录", "text", "selected_text"),
        ("来源页面", "text", "page_url"),
        ("网页标题", "text", "page_title"),
        ("批注类型", "text", "mode"),
        ("批注编号", "text", "annotation_id"),
        ("来源引用", "text", "source"),
        ("QNote回溯", "text", "qnote_url"),
    ]
    if (payload.screenshot_url or payload.screenshot_data_url) and not any(field.get("type") in {"attachment", "image"} for field in fields):
        required_fields.append(("批注截图", "attachment", "screenshot_data_url"))
    field_names = {str(field.get("name") or "").strip() for field in fields}
    missing_required_fields = [
        item for item in required_fields if item[0] not in field_names
    ]
    if missing_required_fields and not permission_allows(table_permission, "edit"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Target table requires schema changes but is not editable",
        )
    for name, field_type, _ in required_fields:
        if name not in field_names:
            field = {"id": f"f_clip_{uuid4().hex[:10]}", "name": name, "type": field_type}
            from app.services.smart_table_store import add_field
            await add_field(db, table.id, field)
            fields.append(field)
    data = {field["id"]: value for field in fields if (value := _field_value(field, payload)) is not None}
    data[status_field["id"]] = initial_status
    if not data:
        raise HTTPException(status_code=400, detail="Target table has no writable fields")
    try:
        data = await normalize_relation_payload_db(
            db,
            fields,
            data,
            user_id=user.id,
        )
    except RelationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    result_max = await db.execute(
        select(func.max(TableRecord.order_index)).where(TableRecord.table_id == table.id)
    )
    record_id = f"r{uuid4().hex[:12]}"
    record = TableRecord(
        id=record_id,
        table_id=table.id,
        data={field["id"]: None for field in fields} | data,
        order_index=(result_max.scalar() or 0) + 1,
        created_by_user_id=user.id,
    )
    db.add(record)
    response = {
        "task_id": record_id,
        "record_id": record_id,
        "qtable_url": _task_url(table.id, record_id),
        "annotation_status": "task_created",
        "target_table_id": table.id,
        "status": _status_meta(status_field, initial_status),
    }
    if payload.client_mutation_id:
        db.add(
            ClipperTaskReceipt(
                id=f"ctr_{uuid4().hex}",
                user_id=user.id,
                client_mutation_id=payload.client_mutation_id,
                request_fingerprint=request_fingerprint,
                response=response,
            )
        )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        if payload.client_mutation_id:
            receipt_result = await db.execute(
                select(ClipperTaskReceipt).where(
                    ClipperTaskReceipt.user_id == user.id,
                    ClipperTaskReceipt.client_mutation_id == payload.client_mutation_id,
                )
            )
            receipt = receipt_result.scalars().first()
            if receipt and receipt.request_fingerprint == request_fingerprint:
                return dict(receipt.response)
        raise
    return response


@router.patch("/tasks/{task_id}/status")
async def update_clipper_task_status(
    task_id: str,
    payload: UpdateClipperTaskStatus,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tables = await _user_table_ids(db, user.id)
    table = next((item for item in tables if item.id == payload.target_table_id), None)
    if table is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to target table")
    table_permission = await _require_clipper_table_permission(
        db,
        user,
        table.id,
        "update",
    )
    try:
        await require_record_access(
            db,
            table.id,
            task_id,
            user_id=user.id,
            table_permission=table_permission,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到对应的 QTable 任务记录",
        ) from exc

    store = await get_full_store(db, table.id)
    fields = list(store.get("fields", []))
    status_field = next((field for field in fields if field.get("id") == payload.status_field_id), None) if payload.status_field_id else None
    status_field = status_field or _find_status_field(fields)
    if status_field is None:
        raise HTTPException(status_code=404, detail="未找到任务状态字段")
    options = _status_options(status_field)
    allowed_values = {option["id"] for option in options} | {option["label"] for option in options}
    if options and payload.value not in allowed_values:
        raise HTTPException(status_code=400, detail="状态值不在 QTable 字段选项中")
    value = payload.value
    if status_field.get("type") in {"select", "multiSelect"}:
        value = next((option["id"] for option in options if payload.value in {option["id"], option["label"]}), payload.value)
    elif options:
        value = next((option["label"] for option in options if payload.value in {option["id"], option["label"]}), payload.value)
    updated = await update_record(
        db,
        table.id,
        task_id,
        str(status_field["id"]),
        value,
        user_id=user.id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="未找到对应的 QTable 任务记录")
    return {"task_id": task_id, "target_table_id": table.id, "status": _status_meta(status_field, value)}


@router.get("/tasks/{task_id}/status")
async def get_clipper_task_status(
    task_id: str,
    target_table_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tables = await _user_table_ids(db, user.id)
    table = next((item for item in tables if item.id == target_table_id), None)
    if table is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to target table")
    table_permission = await _require_clipper_table_permission(
        db,
        user,
        table.id,
        "read",
    )
    try:
        record_model = await require_record_access(
            db,
            table.id,
            task_id,
            user_id=user.id,
            table_permission=table_permission,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到对应的 QTable 任务记录",
        ) from exc
    store = await get_full_store(db, table.id)
    fields = list(store.get("fields", []))
    status_field = _find_status_field(fields)
    if status_field is None:
        raise HTTPException(status_code=404, detail="未找到任务状态字段")
    record = dict(record_model.data or {})
    record["id"] = record_model.id
    return {
        "task_id": task_id,
        "target_table_id": table.id,
        "status": _status_meta(status_field, record.get(status_field["id"])),
    }
