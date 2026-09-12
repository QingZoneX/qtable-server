from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModelSettings
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeSet
from app.models.source_inbox import SourceInboxItem
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.schemas.source_inbox import (
    SourceInboxAiSuggestion,
    SourceInboxConvertRequest,
    SourceInboxIngestRequest,
    SourceInboxPreviewRequest,
    SourceInboxStatusRequest,
)
from app.services.auto_number_engine import allocate_auto_number_values, is_auto_number_field
from app.services.change_history import append_change_set, changed_fields, record_meta
from app.services.member_field import normalize_member_value_for_field, workspace_member_options_for_table
from app.services.project_steward import project_steward_service
from app.services.row_permissions import filter_store_for_user
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


INBOX_STATUSES = {"pending", "converted", "archived", "ignored", "duplicate"}
MUTABLE_STATUSES = {"pending", "archived", "ignored"}
MAX_LIST_LIMIT = 100
MAX_SIMILAR_RECORDS = 500
MAX_SIMILAR_CANDIDATES = 5

TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"]
DESCRIPTION_NAMES = ["任务描述", "描述", "说明", "description", "details"]
PRIORITY_NAMES = ["优先级", "priority"]
OWNER_NAMES = ["负责人", "执行人", "责任人", "成员", "人员", "owner", "assignee"]
WORKLOAD_NAMES = ["预计工作量", "预计工时", "工作量", "p50工时", "workload", "estimate"]
SOURCE_URL_NAMES = ["来源链接", "原文链接", "source url", "source link", "url"]
SOURCE_ID_NAMES = ["来源id", "来源 id", "source id", "sourceid"]
SOURCE_QUOTE_NAMES = ["来源摘录", "原文摘录", "原文", "source quote", "quote"]
SOURCE_NOTE_NAMES = ["来源批注", "批注", "annotation", "source annotation"]

PRIORITY_OPTIONS = [
    {"id": "low", "label": "低", "color": "#E5E7EB"},
    {"id": "medium", "label": "中", "color": "#DBEAFE"},
    {"id": "high", "label": "高", "color": "#FEF3C7"},
    {"id": "urgent", "label": "紧急", "color": "#FEE2E2"},
]
PRIORITY_LABELS = {"low": "低", "medium": "中", "high": "高", "urgent": "紧急"}

AI_SYSTEM_PROMPT = """你是 QTable 来源任务收件箱的整理助手。你只给建议，不执行任何写操作。
输入会包含用户从 QNote/QNoteClipper 保存的网页资料、当前目标表的可见任务摘要以及当前工作空间成员。
规则：
1. 判断资料是否值得转成可执行任务；纯资料收藏可建议不转任务。
2. 任务标题必须具体、可执行，描述应保留来源上下文但不要捏造事实。
3. priority 只能是 low/medium/high/urgent。
4. suggestedAssigneeUserId 只能引用 providedMembers 中真实 userId；没有可靠依据时返回 null。
5. workloadHours 是粗略建议，没有足够依据可返回 null。
6. 不要声称自动合并重复任务；重复候选由服务端确定并给用户选择。
7. 输出只符合结构化 schema。
"""


class SourceInboxError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\s\-_—–·,，。.!！?？:：;；/\\()（）\[\]【】]+", "", text)


def _matches(name: str, candidates: Iterable[str]) -> bool:
    normalized = _norm(name)
    return any(normalized == _norm(item) or _norm(item) in normalized for item in candidates)


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_url(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Fragments often encode a transient browser selection; anchor is hashed separately.
    return raw.split("#", 1)[0].rstrip("/")


def _content_hash(source: Mapping[str, Any]) -> str:
    return _hash(
        {
            "url": _normalized_url(source.get("canonicalUrl") or source.get("url")),
            "quote": " ".join(str(source.get("quote") or "").split()),
            "annotation": " ".join(str(source.get("annotation") or "").split()),
            "anchor": source.get("anchor") or {},
        }
    )


def _request_fingerprint(source: Mapping[str, Any]) -> str:
    return _hash(source)


def _parse_captured_at(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise SourceInboxError("capturedAt must be an ISO datetime") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _serialize_item(item: SourceInboxItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "workspaceId": item.workspace_id,
        "sourceId": item.source_id,
        "sourceType": item.source_type,
        "url": item.url,
        "canonicalUrl": item.canonical_url,
        "pageTitle": item.page_title,
        "quote": item.quote,
        "annotation": item.annotation,
        "anchor": _clone(item.anchor) if item.anchor else None,
        "capturedAt": item.captured_at.isoformat() if item.captured_at else None,
        "tags": list(item.tags or []),
        "sourceAuthor": item.source_author,
        "screenshotUrl": item.screenshot_url,
        "preview": _clone(item.preview) if item.preview else None,
        "status": item.status,
        "duplicateOfId": item.duplicate_of_id,
        "suggestion": _clone(item.suggestion) if item.suggestion else None,
        "suggestionProvider": item.suggestion_provider,
        "suggestionModel": item.suggestion_model,
        "targetTableId": item.target_table_id,
        "targetRecordId": item.target_record_id,
        "convertedChangeSetId": item.converted_change_set_id,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
        "convertedAt": item.converted_at.isoformat() if item.converted_at else None,
    }


def _source_text(item: SourceInboxItem) -> str:
    parts = [item.page_title or "", item.annotation or "", item.quote or ""]
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _similarity(left: str, right: str) -> float:
    a = _norm(left)
    b = _norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    left_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", left.casefold()))
    right_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", right.casefold()))
    jaccard = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0.0
    )
    return round(max(sequence, jaccard), 4)


def _field_by_names(
    fields: Sequence[Mapping[str, Any]],
    names: Iterable[str],
    types: set[str],
) -> Optional[Mapping[str, Any]]:
    return next(
        (
            field
            for field in fields
            if str(field.get("type") or "") in types
            and _matches(str(field.get("name") or ""), names)
        ),
        None,
    )


def _field_model_by_names(
    fields: Sequence[TableField],
    names: Iterable[str],
    types: set[str],
) -> Optional[TableField]:
    return next(
        (field for field in fields if field.type in types and _matches(field.name, names)),
        None,
    )


def _label(field: Optional[Mapping[str, Any]], value: Any) -> str:
    if value in (None, "", []):
        return ""
    options = {
        str(item.get("id")): str(item.get("label") or item.get("name") or item.get("id"))
        for item in list((field or {}).get("options") or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    if isinstance(value, list):
        return ", ".join(part for part in (_label(field, item) for item in value) if part)
    if isinstance(value, dict):
        candidate = value.get("name") or value.get("label") or value.get("id") or value.get("userId") or value.get("value")
        return options.get(str(candidate), str(candidate or ""))
    return options.get(str(value), str(value))


def _field_payload(field: TableField) -> dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": _clone(field.options) if field.options else None,
        "property": _clone(field.property) if field.property else None,
    }


def _new_field_id(semantic: str, used: set[str]) -> str:
    base = "f_inbox_" + re.sub(r"[^a-zA-Z0-9]+", "_", semantic).strip("_").lower()
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


class SourceInboxService:
    async def _require_workspace_member(self, db: AsyncSession, user_id: int, workspace_id: str) -> WorkspaceMember:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == workspace_id,
            )
        )
        member = result.scalars().first()
        if member is None:
            raise PermissionError("Workspace not found or no access")
        return member

    async def _require_item(self, db: AsyncSession, user_id: int, item_id: str, *, lock: bool = False) -> SourceInboxItem:
        stmt = select(SourceInboxItem).where(
            SourceInboxItem.id == item_id,
            SourceInboxItem.user_id == user_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        item = result.scalars().first()
        if item is None:
            raise SourceInboxError("Inbox item not found or no access")
        return item

    async def ingest(self, db: AsyncSession, *, user_id: int, request: SourceInboxIngestRequest) -> dict[str, Any]:
        await self._require_workspace_member(db, user_id, request.workspace_id)
        payload = request.source.model_dump(mode="json", by_alias=True)
        fingerprint = _request_fingerprint(payload)
        content_hash = _content_hash(payload)

        existing_result = await db.execute(
            select(SourceInboxItem).where(
                SourceInboxItem.user_id == user_id,
                SourceInboxItem.workspace_id == request.workspace_id,
                SourceInboxItem.source_id == request.source.source_id,
            )
        )
        existing = existing_result.scalars().first()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise SourceInboxError(
                    "sourceId already exists with different content; use a new sourceId for a new capture"
                )
            return {"item": _serialize_item(existing), "idempotent": True}

        duplicate_result = await db.execute(
            select(SourceInboxItem)
            .where(
                SourceInboxItem.user_id == user_id,
                SourceInboxItem.workspace_id == request.workspace_id,
                SourceInboxItem.content_hash == content_hash,
            )
            .order_by(SourceInboxItem.created_at.asc())
            .limit(1)
        )
        duplicate = duplicate_result.scalars().first()
        item = SourceInboxItem(
            id=f"sin_{uuid.uuid4().hex}",
            user_id=user_id,
            workspace_id=request.workspace_id,
            source_id=request.source.source_id,
            source_type=request.source.source_type,
            url=request.source.url,
            canonical_url=request.source.canonical_url,
            page_title=request.source.page_title,
            quote=request.source.quote,
            annotation=request.source.annotation,
            anchor=_clone(request.source.anchor) if request.source.anchor else None,
            captured_at=_parse_captured_at(payload.get("capturedAt")),
            tags=list(request.source.tags),
            source_author=request.source.source_author,
            screenshot_url=request.source.screenshot_url,
            preview=_clone(request.source.preview) if request.source.preview else None,
            content_hash=content_hash,
            request_fingerprint=fingerprint,
            status="duplicate" if duplicate else "pending",
            duplicate_of_id=duplicate.id if duplicate else None,
        )
        db.add(item)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            # Race-safe idempotency: another retry may have won the unique key.
            result = await db.execute(
                select(SourceInboxItem).where(
                    SourceInboxItem.user_id == user_id,
                    SourceInboxItem.workspace_id == request.workspace_id,
                    SourceInboxItem.source_id == request.source.source_id,
                )
            )
            raced = result.scalars().first()
            if raced and raced.request_fingerprint == fingerprint:
                return {"item": _serialize_item(raced), "idempotent": True}
            raise
        return {"item": _serialize_item(item), "idempotent": False}

    async def list_items(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        status: Optional[str] = None,
        search: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        await self._require_workspace_member(db, user_id, workspace_id)
        if status and status not in INBOX_STATUSES:
            raise SourceInboxError("Invalid inbox status")
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(MAX_LIST_LIMIT, int(limit)))
        predicates = [
            SourceInboxItem.user_id == user_id,
            SourceInboxItem.workspace_id == workspace_id,
        ]
        if status:
            predicates.append(SourceInboxItem.status == status)
        term = str(search or "").strip().casefold()
        if term:
            like = f"%{term}%"
            predicates.append(
                or_(
                    func.lower(func.coalesce(SourceInboxItem.page_title, "")).like(like),
                    func.lower(func.coalesce(SourceInboxItem.annotation, "")).like(like),
                    func.lower(func.coalesce(SourceInboxItem.quote, "")).like(like),
                    func.lower(func.coalesce(SourceInboxItem.url, "")).like(like),
                )
            )
        total = int(
            (
                await db.execute(
                    select(func.count()).select_from(SourceInboxItem).where(*predicates)
                )
            ).scalar()
            or 0
        )
        result = await db.execute(
            select(SourceInboxItem)
            .where(*predicates)
            .order_by(SourceInboxItem.created_at.desc(), SourceInboxItem.id.desc())
            .offset(safe_offset)
            .limit(safe_limit)
        )
        items = list(result.scalars().all())
        next_offset = safe_offset + len(items)
        return {
            "items": [_serialize_item(item) for item in items],
            "totalCount": total,
            "offset": safe_offset,
            "limit": safe_limit,
            "hasMore": next_offset < total,
            "nextOffset": next_offset if next_offset < total else None,
        }

    async def get_item(self, db: AsyncSession, *, user_id: int, item_id: str) -> dict[str, Any]:
        item = await self._require_item(db, user_id, item_id)
        await self._require_workspace_member(db, user_id, item.workspace_id)
        return _serialize_item(item)

    async def _editable_tables(self, db: AsyncSession, *, user_id: int, workspace_id: str) -> list[dict[str, Any]]:
        result = await db.execute(
            select(WorkspaceItem)
            .where(
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "table",
            )
            .order_by(WorkspaceItem.order_index, WorkspaceItem.id)
        )
        output: list[dict[str, Any]] = []
        for table in result.scalars().all():
            permission = await get_effective_permission_for_item(db, user_id, table.id)
            if not permission_allows(permission, "edit"):
                continue
            field_result = await db.execute(
                select(TableField).where(TableField.table_id == table.id).order_by(TableField.order_index)
            )
            fields = list(field_result.scalars().all())
            title = _field_model_by_names(fields, TITLE_NAMES, {"text"}) or next(
                (field for field in fields if field.type == "text"), None
            )
            output.append(
                {
                    "tableId": table.id,
                    "name": table.name,
                    "defaultViewId": table.default_view_id,
                    "permission": permission,
                    "titleFieldId": title.id if title else None,
                    "fieldCount": len(fields),
                }
            )
        return output

    async def _visible_task_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        permission: str,
        item: SourceInboxItem,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        store = await get_full_store(db, table_id)
        visible, _ = await filter_store_for_user(
            db,
            table_id,
            store,
            user_id=user_id,
            table_permission=permission,
        )
        fields = [dict(field) for field in visible.get("fields", []) if isinstance(field, dict)]
        records = [dict(record) for record in visible.get("records", []) if isinstance(record, dict)]
        truncated = len(records) > MAX_SIMILAR_RECORDS
        records = records[:MAX_SIMILAR_RECORDS]
        title_field = _field_by_names(fields, TITLE_NAMES, {"text"}) or next(
            (field for field in fields if field.get("type") == "text"), None
        )
        desc_field = _field_by_names(fields, DESCRIPTION_NAMES, {"text"})
        status_field = _field_by_names(fields, ["状态", "status", "state"], {"select", "text"})
        owner_field = _field_by_names(fields, OWNER_NAMES, {"member"})
        source_text = _source_text(item)
        candidates: list[dict[str, Any]] = []
        if title_field:
            title_id = str(title_field.get("id") or "")
            desc_id = str((desc_field or {}).get("id") or "")
            status_id = str((status_field or {}).get("id") or "")
            owner_id = str((owner_field or {}).get("id") or "")
            for record in records:
                record_id = str(record.get("id") or "")
                title = str(record.get(title_id) or "").strip()
                if not record_id or not title:
                    continue
                description = str(record.get(desc_id) or "").strip() if desc_id else ""
                score = max(_similarity(source_text, title), _similarity(source_text, f"{title}\n{description}"))
                if score < 0.62:
                    continue
                candidates.append(
                    {
                        "recordId": record_id,
                        "title": title,
                        "similarity": score,
                        "reason": "来源内容与现有任务标题/描述语义接近",
                        "status": _label(status_field, record.get(status_id)) if status_id else "",
                        "owner": _label(owner_field, record.get(owner_id)) if owner_id else "",
                        "deepLink": (
                            f"/workbench/{table_id}/{visible.get('defaultViewId')}?recordId={record_id}"
                            if visible.get("defaultViewId")
                            else f"/workbench/{table_id}?recordId={record_id}"
                        ),
                    }
                )
        candidates.sort(key=lambda candidate: (-candidate["similarity"], candidate["title"]))
        return fields, candidates[:MAX_SIMILAR_CANDIDATES], truncated

    def _fallback_suggestion(self, item: SourceInboxItem) -> SourceInboxAiSuggestion:
        annotation = str(item.annotation or "").strip()
        quote = str(item.quote or "").strip()
        page_title = str(item.page_title or "").strip()
        title_source = annotation.splitlines()[0].strip() if annotation else page_title or quote.splitlines()[0].strip()
        title = title_source[:180] or "整理来源资料"
        should_convert = bool(annotation) or any(
            token in _norm(quote)
            for token in ["todo", "待办", "需要", "修复", "实现", "完成", "跟进", "优化"]
        )
        description_parts = []
        if annotation:
            description_parts.append(f"批注：{annotation}")
        if quote:
            description_parts.append(f"原文：{quote}")
        if item.url:
            description_parts.append(f"来源：{item.url}")
        return SourceInboxAiSuggestion.model_validate(
            {
                "shouldConvert": should_convert,
                "title": title,
                "description": "\n\n".join(description_parts)[:10000],
                "priority": "medium",
                "suggestedAssigneeUserId": None,
                "workloadHours": None,
                "reason": "根据批注和来源文本生成保守建议；最终是否转任务由用户确认。",
                "confidence": 0.55 if annotation else 0.35,
            }
        )

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: SourceInboxPreviewRequest,
    ) -> dict[str, Any]:
        item = await self._require_item(db, user_id, request.item_id)
        await self._require_workspace_member(db, user_id, item.workspace_id)
        if item.status == "converted":
            raise SourceInboxError("Inbox item is already converted")

        tables = await self._editable_tables(db, user_id=user_id, workspace_id=item.workspace_id)
        if not tables:
            raise PermissionError("No editable project table is available in this workspace")
        table_by_id = {table["tableId"]: table for table in tables}
        target = table_by_id.get(str(request.target_table_id or "")) if request.target_table_id else tables[0]
        if target is None:
            raise PermissionError("Target table not found or no edit permission")

        fields, similar, truncated = await self._visible_task_context(
            db,
            user_id=user_id,
            table_id=target["tableId"],
            permission=target["permission"],
            item=item,
        )
        member_options = await workspace_member_options_for_table(db, target["tableId"])
        allowed_member_ids = {int(member["id"]) for member in member_options if str(member.get("id") or "").isdigit()}
        fallback = self._fallback_suggestion(item)
        suggestion = fallback
        provider = "deterministic-fallback"
        model_name: Optional[str] = None
        warnings: list[str] = []
        try:
            model, provider, model_name = await project_steward_service._provider(
                db,
                user_id=user_id,
                model_override=request.model,
            )
            agent = Agent(
                model=model,
                output_type=SourceInboxAiSuggestion,
                system_prompt=AI_SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.1),
                output_retries=2,
            )
            context = {
                "source": {
                    "pageTitle": item.page_title,
                    "url": item.url,
                    "quote": item.quote,
                    "annotation": item.annotation,
                    "tags": item.tags,
                    "sourceAuthor": item.source_author,
                },
                "targetTable": {"tableId": target["tableId"], "name": target["name"]},
                "providedMembers": [
                    {"userId": int(member["id"]), "name": member.get("label"), "role": member.get("role")}
                    for member in member_options
                    if str(member.get("id") or "").isdigit()
                ],
                "similarVisibleTasks": similar,
            }
            run = await agent.run(json.dumps(context, ensure_ascii=False, default=str))
            candidate = run.output
            if candidate.suggested_assignee_user_id is not None and candidate.suggested_assignee_user_id not in allowed_member_ids:
                raise SourceInboxError("AI suggested a user outside the current workspace")
            suggestion = candidate
        except Exception:
            provider = "deterministic-fallback"
            model_name = None
            warnings.append("AI 模型不可用或建议未通过校验，已使用安全的本地建议。")

        field_plan = self._field_plan_from_dicts(fields)
        payload = {
            "itemId": item.id,
            "targetTable": target,
            "availableTables": tables,
            "suggestion": suggestion.model_dump(mode="json", by_alias=True),
            "similarTasks": similar,
            "similarityScanTruncated": truncated,
            "fieldPlan": field_plan,
            "members": [
                {
                    "userId": int(member["id"]),
                    "name": member.get("label"),
                    "email": member.get("email"),
                    "role": member.get("role"),
                }
                for member in member_options
                if str(member.get("id") or "").isdigit()
            ],
            "provider": provider,
            "model": model_name,
            "warnings": warnings,
            "permissionScope": {
                "sourceInboxPrivateToUser": True,
                "similarTasksUseCurrentUserRowVisibility": True,
                "targetRequiresEditPermission": True,
            },
        }
        item.suggestion = _clone(payload)
        item.suggestion_provider = provider
        item.suggestion_model = model_name
        item.target_table_id = target["tableId"]
        item.updated_at = _now()
        await db.commit()
        return payload

    def _field_plan_from_dicts(self, fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        semantic_specs = [
            ("title", TITLE_NAMES, {"text"}, "任务名称", "text", None, None),
            ("description", DESCRIPTION_NAMES, {"text"}, "任务描述", "text", None, None),
            ("priority", PRIORITY_NAMES, {"select", "text"}, "优先级", "select", PRIORITY_OPTIONS, None),
            ("owner", OWNER_NAMES, {"member"}, "负责人", "member", None, {"multiple": False}),
            ("workload", WORKLOAD_NAMES, {"number"}, "预计工作量", "number", None, {"suffix": "h", "precision": 1}),
            ("sourceUrl", SOURCE_URL_NAMES, {"url", "text"}, "来源链接", "url", None, None),
            ("sourceId", SOURCE_ID_NAMES, {"text"}, "来源ID", "text", None, None),
            ("sourceQuote", SOURCE_QUOTE_NAMES, {"text"}, "来源摘录", "text", None, None),
            ("sourceAnnotation", SOURCE_NOTE_NAMES, {"text"}, "来源批注", "text", None, None),
        ]
        mapping: dict[str, str] = {}
        additions: list[dict[str, Any]] = []
        used = {str(field.get("id")) for field in fields if field.get("id") is not None}
        for semantic, names, types, name, field_type, options, prop in semantic_specs:
            match = _field_by_names(fields, names, types)
            if match:
                mapping[semantic] = str(match["id"])
                continue
            field_id = _new_field_id(semantic, used)
            mapping[semantic] = field_id
            additions.append(
                {
                    "semantic": semantic,
                    "id": field_id,
                    "name": name,
                    "type": field_type,
                    "options": _clone(options) if options else None,
                    "property": _clone(prop) if prop else None,
                }
            )
        return {"mapping": mapping, "schemaAdditions": additions}

    async def _prepare_fields(self, db: AsyncSession, table_id: str) -> tuple[dict[str, str], list[TableField], list[dict[str, Any]]]:
        result = await db.execute(
            select(TableField).where(TableField.table_id == table_id).order_by(TableField.order_index)
        )
        fields = list(result.scalars().all())
        plan = self._field_plan_from_dicts([_field_payload(field) for field in fields])
        additions = plan["schemaAdditions"]
        next_order = max((int(field.order_index or 0) for field in fields), default=-1) + 1
        created_fields: list[TableField] = []
        for index, addition in enumerate(additions):
            field = TableField(
                id=addition["id"],
                table_id=table_id,
                name=addition["name"],
                type=addition["type"],
                options=_clone(addition["options"]) if addition.get("options") else None,
                property=_clone(addition["property"]) if addition.get("property") else None,
                order_index=next_order + index,
            )
            db.add(field)
            fields.append(field)
            created_fields.append(field)
        await db.flush()
        return dict(plan["mapping"]), fields, additions

    def _priority_value(self, field: TableField, priority: str) -> Any:
        if field.type == "text":
            return PRIORITY_LABELS.get(priority, priority)
        desired = {_norm(priority), _norm(PRIORITY_LABELS.get(priority, priority))}
        for option in list(field.options or []):
            if not isinstance(option, dict) or option.get("id") is None:
                continue
            option_id = str(option["id"])
            label = str(option.get("label") or option_id)
            if _norm(option_id) in desired or _norm(label) in desired:
                return option_id
        raise SourceInboxError(
            f"Priority field '{field.name}' has no option matching {priority}"
        )

    async def convert(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: SourceInboxConvertRequest,
    ) -> dict[str, Any]:
        item = await self._require_item(db, user_id, request.item_id, lock=True)
        await self._require_workspace_member(db, user_id, item.workspace_id)
        if item.status == "converted":
            if item.target_table_id == request.target_table_id and item.target_record_id:
                target_item = await db.get(WorkspaceItem, request.target_table_id)
                deep_link = (
                    f"/workbench/{request.target_table_id}/{target_item.default_view_id}?recordId={item.target_record_id}"
                    if target_item and target_item.default_view_id
                    else f"/workbench/{request.target_table_id}?recordId={item.target_record_id}"
                )
                return {
                    "item": _serialize_item(item),
                    "task": {"tableId": item.target_table_id, "recordId": item.target_record_id, "deepLink": deep_link},
                    "idempotent": True,
                }
            raise SourceInboxError("Inbox item was already converted to another task")

        table_result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == request.target_table_id,
                WorkspaceItem.workspace_id == item.workspace_id,
                WorkspaceItem.type == "table",
            )
        )
        table_item = table_result.scalars().first()
        if table_item is None:
            raise PermissionError("Target table not found or no edit permission")
        permission = await get_effective_permission_for_item(db, user_id, request.target_table_id)
        if not permission_allows(permission, "edit"):
            raise PermissionError("Target table not found or no edit permission")

        if request.assignee_user_id is not None:
            member_result = await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == item.workspace_id,
                    WorkspaceMember.user_id == request.assignee_user_id,
                )
            )
            if member_result.scalars().first() is None:
                raise SourceInboxError("Assignee is not a current workspace member")

        mapping, fields, schema_additions = await self._prepare_fields(db, request.target_table_id)
        by_id = {field.id: field for field in fields}
        row_data: dict[str, Any] = {}
        row_data[mapping["title"]] = request.title
        row_data[mapping["description"]] = request.description
        row_data[mapping["priority"]] = self._priority_value(by_id[mapping["priority"]], request.priority)
        if request.workload_hours is not None:
            row_data[mapping["workload"]] = float(request.workload_hours)
        row_data[mapping["sourceUrl"]] = item.canonical_url or item.url
        row_data[mapping["sourceId"]] = item.source_id
        row_data[mapping["sourceQuote"]] = item.quote
        row_data[mapping["sourceAnnotation"]] = item.annotation
        if request.assignee_user_id is not None:
            row_data[mapping["owner"]] = await normalize_member_value_for_field(
                db,
                request.target_table_id,
                mapping["owner"],
                str(request.assignee_user_id),
            )

        # Match ordinary row creation semantics, including auto-number allocation.
        for field in fields:
            if is_auto_number_field({"type": field.type}):
                values, next_property = allocate_auto_number_values(field.property, 1)
                field.property = next_property
                row_data[field.id] = values[0]
            elif field.id not in row_data:
                row_data[field.id] = None

        max_order = int(
            (
                await db.execute(
                    select(func.max(TableRecord.order_index)).where(
                        TableRecord.table_id == request.target_table_id
                    )
                )
            ).scalar()
            or 0
        )
        record_id = f"r{int(_now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}"
        record = TableRecord(
            id=record_id,
            table_id=request.target_table_id,
            data=_clone(row_data),
            order_index=max_order + 1,
            created_by_user_id=user_id,
            version=1,
        )
        db.add(record)
        await db.flush()
        change_set = await append_change_set(
            db,
            table_id=request.target_table_id,
            actor_id=user_id,
            actor_type="user",
            operation="source_inbox_convert",
            source="source_inbox",
            trace_id=item.id,
            summary=f"Convert source inbox item {item.source_id} to task",
            items=[
                {
                    "table_id": request.target_table_id,
                    "entity_type": "record",
                    "entity_id": record_id,
                    "before_data": None,
                    "after_data": _clone(row_data),
                    "before_meta": None,
                    "after_meta": record_meta(record),
                    "version_before": None,
                    "version_after": 1,
                    "changed_fields": changed_fields(None, row_data),
                }
            ],
        )
        item.status = "converted"
        item.target_table_id = request.target_table_id
        item.target_record_id = record_id
        item.converted_change_set_id = change_set.id
        item.converted_at = _now()
        item.updated_at = _now()
        await db.commit()

        deep_link = (
            f"/workbench/{request.target_table_id}/{table_item.default_view_id}?recordId={record_id}"
            if table_item.default_view_id
            else f"/workbench/{request.target_table_id}?recordId={record_id}"
        )
        return {
            "item": _serialize_item(item),
            "task": {
                "tableId": request.target_table_id,
                "recordId": record_id,
                "deepLink": deep_link,
                "changeSetId": change_set.id,
                "schemaAdditions": schema_additions,
            },
            "idempotent": False,
        }

    async def update_status(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: SourceInboxStatusRequest,
    ) -> dict[str, Any]:
        result = await db.execute(
            select(SourceInboxItem).where(
                SourceInboxItem.user_id == user_id,
                SourceInboxItem.id.in_(request.item_ids),
            )
        )
        items = list(result.scalars().all())
        if len(items) != len(request.item_ids):
            raise SourceInboxError("One or more inbox items were not found or are not accessible")
        workspace_ids = {item.workspace_id for item in items}
        for workspace_id in workspace_ids:
            await self._require_workspace_member(db, user_id, workspace_id)
        changed = 0
        for item in items:
            if item.status == "converted":
                raise SourceInboxError("Converted inbox items cannot be manually reclassified")
            if request.status not in MUTABLE_STATUSES:
                raise SourceInboxError("Invalid mutable inbox status")
            if item.status != request.status:
                item.status = request.status
                if request.status != "pending":
                    # Keeping duplicateOfId is harmless but would misleadingly keep
                    # a manually archived item in a duplicate visual state.
                    item.duplicate_of_id = None
                item.updated_at = _now()
                changed += 1
        await db.commit()
        return {"updatedCount": changed, "items": [_serialize_item(item) for item in items]}


source_inbox_service = SourceInboxService()
