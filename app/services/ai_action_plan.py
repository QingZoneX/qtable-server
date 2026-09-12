from __future__ import annotations

import copy
import json
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModelSettings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_action_plan import AiActionPlanBatch
from app.models.member_assignment import MemberAssignmentBatch
from app.models.project_steward import ProjectStewardDiagnosis
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.schemas.ai_action_plan import (
    AiActionDraft,
    AiActionPlanApplyRequest,
    AiActionPlanPreviewRequest,
    AiActionProposal,
)
from app.services.auto_number_engine import allocate_auto_number_values, is_auto_number_field
from app.services.change_history import (
    append_change_set,
    changed_fields,
    record_meta,
    record_version,
)
from app.services.project_steward import project_steward_service
from app.services.relation_engine import (
    get_target_table_id,
    normalize_relation_value,
    relation_allows_multiple,
)
from app.services.row_permissions import filter_store_for_user, require_record_access
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"]
DESCRIPTION_NAMES = ["任务描述", "描述", "说明", "description", "details"]
OWNER_NAMES = ["负责人", "执行人", "责任人", "成员", "owner", "assignee", "assigned to"]
PRIORITY_NAMES = ["优先级", "priority"]
DEADLINE_NAMES = ["截止时间", "截止日期", "计划完成时间", "计划完成", "due date", "duedate", "deadline"]
STATUS_NAMES = ["状态", "任务状态", "status", "state"]
DEPENDENCY_NAMES = ["前置依赖", "依赖", "依赖任务", "dependencies", "depends on"]
TAG_NAMES = ["标签", "任务标签", "tags", "tag", "技能标签"]

MAX_ACTIONS = 40
AI_SYSTEM_PROMPT = """你是 QTable 的 AI Action Planner。
你的工作不是直接修改数据，而是把已有项目诊断转换成少量、可解释、可预览的结构化动作。

必须遵守：
1. 只能使用输入中明确提供的 tableId、recordId、field option、workspace member userId。
2. 不得猜测隐藏记录、隐藏成员、未知字段或未知枚举值。
3. 每个动作必须有具体 reason；不确定时降低 confidence 或不输出动作。
4. 优先给低风险、高价值动作。不要为了凑数量而创建任务或随意延期。
5. 修改负责人只能使用 eligibleMembers 中的 userId。
6. set_priority / set_status 必须使用 schema 中已有 option id 或 label。
7. set_deadline 必须使用 YYYY-MM-DD。
8. create_task 只在诊断明确指出需要新增跟进工作时使用，避免重复任务。
9. 最多输出 20 个动作。
10. 这些动作仍需用户确认；你不能声称已经执行。
"""


class AiActionPlanError(ValueError):
    pass


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _matches(name: str, candidates: Iterable[str]) -> bool:
    normalized = _norm(name)
    return any(normalized == _norm(item) or _norm(item) in normalized for item in candidates)


def _find_field(
    fields: list[TableField],
    names: list[str],
    types: Optional[set[str]] = None,
) -> Optional[TableField]:
    return next(
        (
            field
            for field in fields
            if (not types or field.type in types) and _matches(field.name, names)
        ),
        None,
    )


def _field_dict(field: TableField) -> dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": _clone(field.options),
        "property": _clone(field.property),
    }


def _option_map(field: TableField) -> dict[str, str]:
    output: dict[str, str] = {}
    for item in list(field.options or []):
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        option_id = str(item["id"])
        output[_norm(option_id)] = option_id
        for key in ("label", "name", "value"):
            if item.get(key) is not None:
                output[_norm(item[key])] = option_id
    return output


def _normalize_choice(field: TableField, value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise AiActionPlanError(f"{field.name} cannot be empty")
    if field.type == "text":
        return raw
    if field.type != "select":
        raise AiActionPlanError(f"{field.name} must be a select or text field")
    mapped = _option_map(field).get(_norm(raw))
    if not mapped:
        raise AiActionPlanError(f"Unknown option '{raw}' for field {field.name}")
    return mapped


def _normalize_multi_choice(field: TableField, values: list[str]) -> list[str]:
    if field.type not in {"multiSelect", "multipleSelect"}:
        raise AiActionPlanError(f"{field.name} is not a multi-select field")
    options = _option_map(field)
    output: list[str] = []
    for value in values:
        mapped = options.get(_norm(value))
        if not mapped:
            raise AiActionPlanError(f"Unknown option '{value}' for field {field.name}")
        if mapped not in output:
            output.append(mapped)
    return output


def _display_value(field: Optional[TableField], value: Any) -> Any:
    if field is None or value in (None, "", []):
        return value
    if field.type in {"select", "multiSelect", "multipleSelect"}:
        labels = {
            str(item.get("id")): str(item.get("label") or item.get("name") or item.get("id"))
            for item in list(field.options or [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        if isinstance(value, list):
            return [labels.get(str(item), str(item)) for item in value]
        return labels.get(str(value), str(value))
    if field.type == "member":
        def one(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    "userId": item.get("userId") or item.get("id"),
                    "name": item.get("name") or item.get("label"),
                }
            return item
        return [one(item) for item in value] if isinstance(value, list) else one(value)
    return value


def _member_ids(value: Any) -> list[int]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        output: list[int] = []
        for item in value:
            output.extend(_member_ids(item))
        return list(dict.fromkeys(output))
    if isinstance(value, dict):
        value = value.get("userId") or value.get("id") or value.get("user_id") or value.get("value")
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return []


def _relation_ids(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return list(dict.fromkeys(str(item) for item in value if item not in (None, "")))
    if isinstance(value, dict):
        value = value.get("recordId") or value.get("id") or value.get("value")
    return [str(value)] if value not in (None, "") else []


def _parse_iso_date(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise AiActionPlanError("Deadline must use YYYY-MM-DD") from exc
    return parsed.isoformat()


def _record_key(table_id: str, record_id: str) -> str:
    return f"{table_id}:{record_id}"


def _action_id() -> str:
    return f"act_{uuid.uuid4().hex}"


def _new_record_id() -> str:
    return f"rai_{uuid.uuid4().hex}"


class AiActionPlanService:
    async def _diagnosis(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        diagnosis_id: str,
    ) -> tuple[ProjectStewardDiagnosis, dict[str, Any]]:
        row_result = await db.execute(
            select(ProjectStewardDiagnosis).where(
                ProjectStewardDiagnosis.id == diagnosis_id,
                ProjectStewardDiagnosis.user_id == user_id,
            )
        )
        row = row_result.scalars().first()
        if row is None:
            raise AiActionPlanError("Project diagnosis not found or no access")

        fresh = await project_steward_service.get_diagnosis(
            db,
            user_id=user_id,
            diagnosis_id=diagnosis_id,
        )
        if fresh is None or fresh.get("isStale") or fresh.get("result") is None:
            raise AiActionPlanError(
                "Project diagnosis is stale; rerun the project steward before creating an Action Plan"
            )
        return row, dict(fresh["result"])

    async def _table_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        table_id: str,
        required_permission: str = "read",
    ) -> dict[str, Any]:
        item_result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == table_id,
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "table",
            )
        )
        item = item_result.scalars().first()
        if item is None:
            raise AiActionPlanError("Action target table not found or no access")
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, required_permission):
            raise PermissionError("Action target table not found or no access")

        field_result = await db.execute(
            select(TableField)
            .where(TableField.table_id == table_id)
            .order_by(TableField.order_index)
        )
        fields = list(field_result.scalars().all())
        store = await get_full_store(db, table_id)
        visible_store, _ = await filter_store_for_user(
            db,
            table_id,
            store,
            user_id=user_id,
            table_permission=permission,
        )
        visible_records = {
            str(record.get("id")): dict(record)
            for record in visible_store.get("records", [])
            if isinstance(record, dict) and record.get("id") is not None
        }
        version_result = await db.execute(
            select(TableRecord.id, TableRecord.version).where(
                TableRecord.table_id == table_id,
                TableRecord.id.in_(list(visible_records) or ["__none__"]),
            )
        )
        versions = {
            str(record_id): int(version or 1)
            for record_id, version in version_result.all()
        }
        return {
            "item": item,
            "permission": permission,
            "fields": fields,
            "fieldById": {field.id: field for field in fields},
            "visibleRecords": visible_records,
            "versions": versions,
        }

    async def _eligible_members(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        table_id: str,
    ) -> list[dict[str, Any]]:
        result = await db.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(User.name.asc(), User.id.asc())
        )
        output: list[dict[str, Any]] = []
        for member, user in result.all():
            permission = await get_effective_permission_for_item(db, int(user.id), table_id)
            if not permission_allows(permission, "read"):
                continue
            output.append(
                {
                    "userId": int(user.id),
                    "name": str(user.name or f"User {user.id}"),
                    "workspaceRole": getattr(member.role, "value", str(member.role)),
                }
            )
        return output

    async def _member_value(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        table_id: str,
        field: TableField,
        user_id: int,
    ) -> tuple[Any, dict[str, Any]]:
        result = await db.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == int(user_id),
            )
        )
        row = result.first()
        if row is None:
            raise AiActionPlanError("Suggested assignee is not a workspace member")
        _, user = row
        permission = await get_effective_permission_for_item(db, int(user.id), table_id)
        if not permission_allows(permission, "read"):
            raise AiActionPlanError("Suggested assignee has no access to the task table")
        payload = {
            "id": str(user.id),
            "userId": int(user.id),
            "name": str(user.name or f"User {user.id}"),
        }
        multiple = bool((field.property or {}).get("multiple", True))
        return ([payload] if multiple else payload), payload

    def _semantic_fields(self, fields: list[TableField]) -> dict[str, Optional[TableField]]:
        title = _find_field(fields, TITLE_NAMES, {"text"}) or next(
            (field for field in fields if field.type == "text"),
            None,
        )
        return {
            "title": title,
            "description": _find_field(fields, DESCRIPTION_NAMES, {"text"}),
            "owner": _find_field(fields, OWNER_NAMES, {"member"}),
            "priority": _find_field(fields, PRIORITY_NAMES, {"select", "text"}),
            "deadline": _find_field(fields, DEADLINE_NAMES, {"date", "text"}),
            "status": _find_field(fields, STATUS_NAMES, {"select", "text"}),
            "dependency": _find_field(fields, DEPENDENCY_NAMES, {"relation"}),
            "tags": _find_field(
                fields,
                TAG_NAMES,
                {"text", "multiSelect", "multipleSelect"},
            ),
        }

    async def _validate_relation_targets(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        diagnosis_table_ids: set[str],
        field: TableField,
        record_ids: list[str],
    ) -> list[dict[str, str]]:
        target_table_id = get_target_table_id(_field_dict(field))
        if not target_table_id:
            raise AiActionPlanError("Dependency relation field has no target table")
        if target_table_id not in diagnosis_table_ids:
            raise AiActionPlanError(
                "Dependency target table is outside the current project diagnosis scope"
            )
        target_permission = await get_effective_permission_for_item(
            db,
            user_id,
            target_table_id,
        )
        if not permission_allows(target_permission, "read"):
            raise PermissionError("Dependency target record not found or no access")
        targets: list[dict[str, str]] = []
        for record_id in record_ids:
            await require_record_access(
                db,
                target_table_id,
                record_id,
                user_id=user_id,
                table_permission=target_permission,
            )
            targets.append({"tableId": target_table_id, "recordId": record_id})
        return targets

    def _dependency_cycle(
        self,
        *,
        record_id: str,
        proposed_dependencies: list[str],
        field: TableField,
        context: dict[str, Any],
    ) -> bool:
        target_table_id = get_target_table_id(_field_dict(field))
        if target_table_id != context["item"].id:
            return False
        if record_id in proposed_dependencies:
            return True
        graph: dict[str, list[str]] = {}
        for rid, record in context["visibleRecords"].items():
            graph[rid] = _relation_ids(record.get(field.id))
        graph[record_id] = list(proposed_dependencies)

        def reaches(start: str, target: str, seen: set[str]) -> bool:
            if start == target:
                return True
            if start in seen:
                return False
            seen.add(start)
            return any(reaches(dep, target, seen) for dep in graph.get(start, []))

        return any(reaches(dep, record_id, set()) for dep in proposed_dependencies)

    async def _normalize_action(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        diagnosis_table_ids: set[str],
        contexts: dict[str, dict[str, Any]],
        draft: AiActionDraft,
    ) -> dict[str, Any]:
        if draft.table_id not in diagnosis_table_ids:
            raise AiActionPlanError("Action table is outside the current project diagnosis scope")
        context = contexts[draft.table_id]
        if not permission_allows(context["permission"], "update"):
            raise PermissionError("No update permission for Action Plan target table")

        fields = context["fields"]
        semantic = self._semantic_fields(fields)
        action_id = draft.action_id or _action_id()
        warnings: list[str] = [draft.risk] if draft.risk else []
        common = {
            "actionId": action_id,
            "type": draft.action_type,
            "tableId": draft.table_id,
            "reason": draft.reason or "AI 根据当前项目诊断生成的建议。",
            "riskWarnings": warnings,
            "confidence": round(float(draft.confidence), 2),
            "canApply": True,
        }

        if draft.action_type == "create_task":
            title_field = semantic["title"]
            if title_field is None:
                raise AiActionPlanError("Target table has no writable title field")
            payload: dict[str, Any] = {title_field.id: draft.title}
            field_types: dict[str, str] = {title_field.id: title_field.type}
            member_user_ids: list[int] = []
            dependency_targets: list[dict[str, str]] = []

            if draft.description:
                field = semantic["description"]
                if field is None:
                    raise AiActionPlanError("Target table has no description field")
                payload[field.id] = draft.description
                field_types[field.id] = field.type
            if draft.user_id is not None:
                field = semantic["owner"]
                if field is None:
                    raise AiActionPlanError("Target table has no member/assignee field")
                value, _ = await self._member_value(
                    db,
                    workspace_id=workspace_id,
                    table_id=draft.table_id,
                    field=field,
                    user_id=draft.user_id,
                )
                payload[field.id] = value
                field_types[field.id] = field.type
                member_user_ids.append(int(draft.user_id))
            if draft.priority:
                field = semantic["priority"]
                if field is None:
                    raise AiActionPlanError("Target table has no priority field")
                payload[field.id] = _normalize_choice(field, draft.priority)
                field_types[field.id] = field.type
            if draft.deadline:
                field = semantic["deadline"]
                if field is None:
                    raise AiActionPlanError("Target table has no deadline field")
                payload[field.id] = _parse_iso_date(draft.deadline)
                field_types[field.id] = field.type
            if draft.status:
                field = semantic["status"]
                if field is None:
                    raise AiActionPlanError("Target table has no status field")
                payload[field.id] = _normalize_choice(field, draft.status)
                field_types[field.id] = field.type
            if draft.tags:
                field = semantic["tags"]
                if field is None:
                    raise AiActionPlanError("Target table has no tags field")
                if field.type == "text":
                    payload[field.id] = ", ".join(draft.tags)
                else:
                    payload[field.id] = _normalize_multi_choice(field, draft.tags)
                field_types[field.id] = field.type
            if draft.dependency_record_ids:
                field = semantic["dependency"]
                if field is None:
                    raise AiActionPlanError("Target table has no dependency relation field")
                normalized = normalize_relation_value(
                    _field_dict(field),
                    draft.dependency_record_ids,
                )
                payload[field.id] = normalized
                field_types[field.id] = field.type
                dependency_targets = await self._validate_relation_targets(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    diagnosis_table_ids=diagnosis_table_ids,
                    field=field,
                    record_ids=_relation_ids(normalized),
                )

            new_record_id = _new_record_id()
            common["riskWarnings"] = warnings + [
                "将创建新的正式任务记录；请确认不存在重复任务。"
            ]
            return {
                **common,
                "recordId": new_record_id,
                "fieldId": None,
                "fieldType": None,
                "currentValue": None,
                "suggestedValue": payload,
                "currentDisplayValue": None,
                "suggestedDisplayValue": {
                    field_id: _display_value(context["fieldById"].get(field_id), value)
                    for field_id, value in payload.items()
                },
                "recordVersion": None,
                "fieldTypes": field_types,
                "memberUserIds": member_user_ids,
                "dependencyTargets": dependency_targets,
                "title": draft.title,
            }

        record_id = str(draft.record_id)
        record = context["visibleRecords"].get(record_id)
        if record is None:
            raise PermissionError("Action target record not found or no access")
        version = context["versions"].get(record_id)
        if version is None:
            raise AiActionPlanError("Action target record no longer exists")

        field: Optional[TableField] = None
        suggested_value: Any = None
        member_user_ids: list[int] = []
        dependency_targets: list[dict[str, str]] = []

        if draft.action_type == "assign_owner":
            field = semantic["owner"]
            if field is None:
                raise AiActionPlanError("Target table has no member/assignee field")
            suggested_value, _ = await self._member_value(
                db,
                workspace_id=workspace_id,
                table_id=draft.table_id,
                field=field,
                user_id=int(draft.user_id),
            )
            member_user_ids = [int(draft.user_id)]
            if _member_ids(record.get(field.id)):
                warnings.append("该任务已有负责人，确认后将覆盖当前负责人。")
        elif draft.action_type == "set_priority":
            field = semantic["priority"]
            if field is None:
                raise AiActionPlanError("Target table has no priority field")
            suggested_value = _normalize_choice(field, str(draft.value))
            warnings.append("优先级变化可能影响当前排期与资源顺序。")
        elif draft.action_type == "set_deadline":
            field = semantic["deadline"]
            if field is None:
                raise AiActionPlanError("Target table has no deadline field")
            suggested_value = _parse_iso_date(str(draft.value))
            current_date = record.get(field.id)
            if current_date:
                try:
                    before = date.fromisoformat(str(current_date)[:10])
                    after = date.fromisoformat(suggested_value)
                    if after > before:
                        warnings.append("建议截止日期晚于当前截止日期，属于延期调整。")
                    elif after < before:
                        warnings.append("建议截止日期早于当前截止日期，需要确认交付可行性。")
                except ValueError:
                    warnings.append("当前截止日期格式异常，请重点核对修改结果。")
        elif draft.action_type == "set_status":
            field = semantic["status"]
            if field is None:
                raise AiActionPlanError("Target table has no status field")
            suggested_value = _normalize_choice(field, str(draft.value))
            warnings.append("状态变化会影响项目统计与后续自动化。")
        elif draft.action_type == "set_dependency":
            field = semantic["dependency"]
            if field is None:
                raise AiActionPlanError("Target table has no dependency relation field")
            raw = draft.values or draft.dependency_record_ids
            suggested_value = normalize_relation_value(_field_dict(field), raw)
            relation_ids = _relation_ids(suggested_value)
            dependency_targets = await self._validate_relation_targets(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
                diagnosis_table_ids=diagnosis_table_ids,
                field=field,
                record_ids=relation_ids,
            )
            if self._dependency_cycle(
                record_id=record_id,
                proposed_dependencies=relation_ids,
                field=field,
                context=context,
            ):
                raise AiActionPlanError("Dependency change would create a visible dependency cycle")
            warnings.append("依赖变化可能改变关键路径和阻塞关系。")
        elif draft.action_type == "add_tags":
            field = semantic["tags"]
            if field is None:
                raise AiActionPlanError("Target table has no tags field")
            raw_tags = draft.values or draft.tags
            if not raw_tags:
                raise AiActionPlanError("At least one tag is required")
            if field.type == "text":
                existing = [
                    item.strip()
                    for item in str(record.get(field.id) or "").replace("，", ",").split(",")
                    if item.strip()
                ]
                merged = list(dict.fromkeys(existing + raw_tags))
                suggested_value = ", ".join(merged)
            else:
                existing = list(record.get(field.id) or [])
                merged = list(dict.fromkeys(existing + _normalize_multi_choice(field, raw_tags)))
                suggested_value = merged
        else:
            raise AiActionPlanError(f"Unsupported action type: {draft.action_type}")

        if field is None:
            raise AiActionPlanError("Action field could not be resolved")
        current_value = _clone(record.get(field.id))
        return {
            **common,
            "recordId": record_id,
            "fieldId": field.id,
            "fieldType": field.type,
            "currentValue": current_value,
            "suggestedValue": _clone(suggested_value),
            "currentDisplayValue": _display_value(field, current_value),
            "suggestedDisplayValue": _display_value(field, suggested_value),
            "recordVersion": int(version),
            "fieldTypes": {field.id: field.type},
            "memberUserIds": member_user_ids,
            "dependencyTargets": dependency_targets,
            "title": None,
        }

    async def _assignment_fallback_actions(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        batch_id: Optional[str],
        evidence_ids: set[tuple[str, str]],
    ) -> list[AiActionDraft]:
        if not batch_id:
            return []
        result = await db.execute(
            select(MemberAssignmentBatch).where(
                MemberAssignmentBatch.id == batch_id,
                MemberAssignmentBatch.user_id == user_id,
                MemberAssignmentBatch.workspace_id == workspace_id,
            )
        )
        batch = result.scalars().first()
        if batch is None:
            raise AiActionPlanError("Member assignment batch not found or no access")

        actions: list[AiActionDraft] = []
        for item in list((batch.result_payload or {}).get("tasks") or []):
            record_id = str(item.get("recordId") or "")
            if (batch.table_id, record_id) not in evidence_ids:
                continue
            recommendations = list(item.get("recommendations") or [])
            if not recommendations:
                continue
            current = await db.get(
                TableRecord,
                {"id": record_id, "table_id": batch.table_id},
            )
            expected = int((batch.record_versions or {}).get(record_id) or 1)
            if current is None or record_version(current) != expected:
                continue
            top = recommendations[0]
            if top.get("userId") is None:
                continue
            actions.append(
                AiActionDraft.model_validate(
                    {
                        "type": "assign_owner",
                        "tableId": batch.table_id,
                        "recordId": record_id,
                        "userId": int(top["userId"]),
                        "reason": "复用 AI 人员分配建议中的 Top 1 负责人。",
                        "risk": (
                            "分配后可能超过成员容量，请结合人员分配预览中的负载提示确认。"
                            if top.get("conflicts")
                            else ""
                        ),
                        "confidence": float(top.get("confidence") or item.get("confidence") or 0.65),
                    }
                )
            )
        return actions

    def _priority_fallback_actions(
        self,
        *,
        diagnosis: dict[str, Any],
        contexts: dict[str, dict[str, Any]],
    ) -> list[AiActionDraft]:
        risky_categories = {"overdue", "due_soon", "blocked", "critical_path", "owner_overload"}
        actions: list[AiActionDraft] = []
        seen: set[tuple[str, str]] = set()
        conclusions = (
            list(diagnosis.get("facts") or [])
            + list(diagnosis.get("inferences") or [])
        )
        for conclusion in conclusions:
            if str(conclusion.get("category") or "") not in risky_categories:
                continue
            for evidence in list(conclusion.get("evidence") or []):
                table_id = str(evidence.get("tableId") or "")
                record_id = str(evidence.get("recordId") or "")
                if not table_id or not record_id or (table_id, record_id) in seen:
                    continue
                context = contexts.get(table_id)
                if context is None:
                    continue
                priority = self._semantic_fields(context["fields"])["priority"]
                if priority is None or priority.type != "select":
                    continue
                options = [
                    item for item in list(priority.options or [])
                    if isinstance(item, dict) and item.get("id") is not None
                ]
                if not options:
                    continue
                high = next(
                    (
                        item
                        for item in options
                        if _norm(item.get("label") or item.get("name") or item.get("id"))
                        in {_norm("高"), _norm("高优先级"), _norm("high"), _norm("urgent"), _norm("紧急")}
                    ),
                    options[0],
                )
                seen.add((table_id, record_id))
                actions.append(
                    AiActionDraft.model_validate(
                        {
                            "type": "set_priority",
                            "tableId": table_id,
                            "recordId": record_id,
                            "value": str(high["id"]),
                            "reason": str(conclusion.get("title") or "当前项目诊断识别到风险任务。"),
                            "risk": "仅调整优先级，不自动改变负责人、截止日期或状态。",
                            "confidence": 0.68,
                        }
                    )
                )
                if len(actions) >= 12:
                    return actions
        return actions

    async def _generate_actions(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        row: ProjectStewardDiagnosis,
        diagnosis: dict[str, Any],
        contexts: dict[str, dict[str, Any]],
        request: AiActionPlanPreviewRequest,
    ) -> tuple[list[AiActionDraft], str, Optional[str]]:
        evidence_ids = {
            (
                str(evidence.get("tableId") or ""),
                str(evidence.get("recordId") or ""),
            )
            for conclusion in (
                list(diagnosis.get("facts") or [])
                + list(diagnosis.get("inferences") or [])
                + list(diagnosis.get("suggestions") or [])
            )
            for evidence in list(conclusion.get("evidence") or [])
            if evidence.get("tableId") and evidence.get("recordId")
        }
        assignment_actions = await self._assignment_fallback_actions(
            db,
            user_id=user_id,
            workspace_id=row.workspace_id,
            batch_id=request.member_assignment_batch_id,
            evidence_ids=evidence_ids,
        )

        prompt_context = {
            "diagnosis": {
                "facts": diagnosis.get("facts") or [],
                "inferences": diagnosis.get("inferences") or [],
                "suggestions": diagnosis.get("suggestions") or [],
                "snapshot": diagnosis.get("snapshot") or {},
            },
            "tables": [],
        }
        for table_id, context in contexts.items():
            eligible = await self._eligible_members(
                db,
                workspace_id=row.workspace_id,
                table_id=table_id,
            )
            evidence_records = [
                {
                    "recordId": record_id,
                    "title": next(
                        (
                            evidence.get("title")
                            for conclusion in (
                                list(diagnosis.get("facts") or [])
                                + list(diagnosis.get("inferences") or [])
                                + list(diagnosis.get("suggestions") or [])
                            )
                            for evidence in list(conclusion.get("evidence") or [])
                            if str(evidence.get("tableId") or "") == table_id
                            and str(evidence.get("recordId") or "") == record_id
                        ),
                        record_id,
                    ),
                }
                for source_table, record_id in sorted(evidence_ids)
                if source_table == table_id
            ]
            prompt_context["tables"].append(
                {
                    "tableId": table_id,
                    "fields": [
                        {
                            "id": field.id,
                            "name": field.name,
                            "type": field.type,
                            "options": field.options,
                            "property": field.property,
                        }
                        for field in context["fields"]
                    ],
                    "eligibleMembers": eligible,
                    "diagnosisEvidenceRecords": evidence_records,
                }
            )

        try:
            model, provider, model_name = await project_steward_service._provider(
                db,
                user_id=user_id,
                model_override=request.model,
            )
            agent = Agent(
                model=model,
                deps_type=dict,
                output_type=AiActionProposal,
                system_prompt=AI_SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.1),
            )
            result = await agent.run(
                json.dumps(prompt_context, ensure_ascii=False, default=str),
                deps={},
            )
            generated = list(result.output.actions or [])[:20]
            if generated:
                return generated, provider, model_name
        except Exception:
            pass

        fallback = assignment_actions + self._priority_fallback_actions(
            diagnosis=diagnosis,
            contexts=contexts,
        )
        deduped: list[AiActionDraft] = []
        keys: set[tuple[str, str, str]] = set()
        for action in fallback:
            key = (
                action.table_id,
                action.record_id or "",
                action.action_type,
            )
            if key in keys:
                continue
            keys.add(key)
            deduped.append(action)
        return deduped[:20], "deterministic-fallback", None

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: AiActionPlanPreviewRequest,
    ) -> dict[str, Any]:
        row, diagnosis = await self._diagnosis(
            db,
            user_id=user_id,
            diagnosis_id=request.diagnosis_id,
        )
        diagnosis_table_ids = set(str(item) for item in list(row.table_ids or []))
        contexts: dict[str, dict[str, Any]] = {}
        for table_id in diagnosis_table_ids:
            contexts[table_id] = await self._table_context(
                db,
                user_id=user_id,
                workspace_id=row.workspace_id,
                table_id=table_id,
                required_permission="read",
            )

        explicit = request.proposed_actions is not None
        if explicit:
            drafts = list(request.proposed_actions or [])
            provider = "user-edited"
            model_name = None
        else:
            drafts, provider, model_name = await self._generate_actions(
                db,
                user_id=user_id,
                row=row,
                diagnosis=diagnosis,
                contexts=contexts,
                request=request,
            )

        normalized: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        update_targets: set[tuple[str, str, str]] = set()
        action_ids: set[str] = set()

        for draft in drafts[:MAX_ACTIONS]:
            try:
                action = await self._normalize_action(
                    db,
                    user_id=user_id,
                    workspace_id=row.workspace_id,
                    diagnosis_table_ids=diagnosis_table_ids,
                    contexts=contexts,
                    draft=draft,
                )
                if action["actionId"] in action_ids:
                    raise AiActionPlanError("Duplicate actionId")
                action_ids.add(action["actionId"])
                if action["type"] != "create_task":
                    key = (
                        action["tableId"],
                        action["recordId"],
                        str(action["fieldId"]),
                    )
                    if key in update_targets:
                        raise AiActionPlanError(
                            "Action Plan cannot contain two changes to the same record field"
                        )
                    update_targets.add(key)
                normalized.append(action)
            except (AiActionPlanError, PermissionError, ValueError) as exc:
                if explicit:
                    raise
                rejected.append(
                    {
                        "action": draft.model_dump(mode="json", by_alias=True),
                        "reason": str(exc),
                    }
                )

        record_versions = {
            _record_key(action["tableId"], action["recordId"]): action["recordVersion"]
            for action in normalized
            if action["type"] != "create_task" and action.get("recordVersion") is not None
        }
        plan_id = str(uuid.uuid4())
        trace_id = f"aia_{uuid.uuid4().hex}"
        result_payload = {
            "actions": _clone(normalized),
            "rejectedActions": _clone(rejected),
            "appliedActionIds": [],
            "changeSetIds": [],
            "diagnosisSnapshot": _clone(diagnosis.get("snapshot") or {}),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }
        batch = AiActionPlanBatch(
            id=plan_id,
            trace_id=trace_id,
            user_id=user_id,
            workspace_id=row.workspace_id,
            diagnosis_id=row.id,
            table_ids=sorted(diagnosis_table_ids),
            request_payload=request.model_dump(mode="json", by_alias=True),
            result_payload=result_payload,
            record_versions=record_versions,
            status="previewed",
            change_set_ids=[],
            provider=provider,
            model=model_name,
        )
        db.add(batch)
        await db.commit()

        return {
            "planId": plan_id,
            "traceId": trace_id,
            "diagnosisId": row.id,
            "workspaceId": row.workspace_id,
            "tableIds": sorted(diagnosis_table_ids),
            "provider": provider,
            "model": model_name,
            "actions": normalized,
            "rejectedActions": rejected,
            "actionCount": len(normalized),
            "previewOnly": True,
        }

    async def get_plan(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        plan_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(AiActionPlanBatch).where(
                AiActionPlanBatch.id == plan_id,
                AiActionPlanBatch.user_id == user_id,
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "planId": row.id,
            "traceId": row.trace_id,
            "diagnosisId": row.diagnosis_id,
            "workspaceId": row.workspace_id,
            "tableIds": row.table_ids,
            "request": row.request_payload,
            "result": row.result_payload,
            "recordVersions": row.record_versions,
            "status": row.status,
            "changeSetIds": row.change_set_ids,
            "provider": row.provider,
            "model": row.model,
            "applyError": row.apply_error,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        }

    async def _load_plan_for_apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        plan_id: str,
    ) -> AiActionPlanBatch:
        result = await db.execute(
            select(AiActionPlanBatch)
            .where(
                AiActionPlanBatch.id == plan_id,
                AiActionPlanBatch.user_id == user_id,
            )
            .with_for_update()
        )
        row = result.scalars().first()
        if row is None:
            raise AiActionPlanError("Action Plan not found or no access")
        return row

    async def _revalidate_member(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        table_id: str,
        user_id: int,
    ) -> None:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == int(user_id),
            )
        )
        if result.scalars().first() is None:
            raise AiActionPlanError("Action assignee no longer belongs to the workspace")
        permission = await get_effective_permission_for_item(db, int(user_id), table_id)
        if not permission_allows(permission, "read"):
            raise AiActionPlanError("Action assignee no longer has task-table access")

    async def _revalidate_dependency(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        target: dict[str, str],
    ) -> None:
        table_id = str(target["tableId"])
        record_id = str(target["recordId"])
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, "read"):
            raise PermissionError("Dependency target record not found or no access")
        await require_record_access(
            db,
            table_id,
            record_id,
            user_id=user_id,
            table_permission=permission,
        )

    def _validate_stored_field_value(self, field: TableField, value: Any) -> None:
        if field.type == "select" and value not in (None, ""):
            valid = {str(item.get("id")) for item in list(field.options or []) if isinstance(item, dict)}
            if str(value) not in valid:
                raise AiActionPlanError(f"Field option changed after preview: {field.name}")
        if field.type in {"multiSelect", "multipleSelect"}:
            valid = {str(item.get("id")) for item in list(field.options or []) if isinstance(item, dict)}
            if any(str(item) not in valid for item in list(value or [])):
                raise AiActionPlanError(f"Field options changed after preview: {field.name}")
        if field.type == "date" and value not in (None, ""):
            _parse_iso_date(str(value))
        if field.type == "relation":
            normalize_relation_value(_field_dict(field), value)

    async def apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: AiActionPlanApplyRequest,
    ) -> dict[str, Any]:
        batch = await self._load_plan_for_apply(
            db,
            user_id=user_id,
            plan_id=request.plan_id,
        )
        payload = dict(batch.result_payload or {})
        actions = {
            str(item.get("actionId")): dict(item)
            for item in list(payload.get("actions") or [])
            if item.get("actionId")
        }
        applied = set(str(item) for item in list(payload.get("appliedActionIds") or []))
        requested_ids = request.action_ids or list(actions)
        if any(action_id not in actions for action_id in requested_ids):
            raise AiActionPlanError("Apply selection contains action outside this Action Plan")
        pending_ids = [action_id for action_id in requested_ids if action_id not in applied]
        if not pending_ids:
            return {
                "planId": batch.id,
                "status": batch.status,
                "idempotent": True,
                "appliedActionIds": sorted(applied),
                "changeSetIds": list(batch.change_set_ids or []),
                "affectedTableIds": [],
            }

        selected = [actions[action_id] for action_id in pending_ids]
        affected_table_ids = sorted({str(action["tableId"]) for action in selected})

        try:
            table_fields: dict[str, list[TableField]] = {}
            table_field_by_id: dict[str, dict[str, TableField]] = {}
            table_permissions: dict[str, str] = {}
            for table_id in affected_table_ids:
                item_result = await db.execute(
                    select(WorkspaceItem).where(
                        WorkspaceItem.id == table_id,
                        WorkspaceItem.workspace_id == batch.workspace_id,
                        WorkspaceItem.type == "table",
                    )
                )
                if item_result.scalars().first() is None:
                    raise AiActionPlanError("Action target table no longer exists")
                permission = await get_effective_permission_for_item(db, user_id, table_id)
                if not permission_allows(permission, "update"):
                    raise PermissionError("No update permission for Action Plan target table")
                table_permissions[table_id] = permission
                field_result = await db.execute(
                    select(TableField)
                    .where(TableField.table_id == table_id)
                    .order_by(TableField.order_index)
                    .with_for_update()
                )
                fields = list(field_result.scalars().all())
                table_fields[table_id] = fields
                table_field_by_id[table_id] = {field.id: field for field in fields}

            update_actions = [action for action in selected if action["type"] != "create_task"]
            create_actions = [action for action in selected if action["type"] == "create_task"]

            grouped_updates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for action in update_actions:
                table_id = str(action["tableId"])
                record_id = str(action["recordId"])
                await require_record_access(
                    db,
                    table_id,
                    record_id,
                    user_id=user_id,
                    table_permission=table_permissions[table_id],
                )
                grouped_updates[(table_id, record_id)].append(action)
                for member_user_id in list(action.get("memberUserIds") or []):
                    await self._revalidate_member(
                        db,
                        workspace_id=batch.workspace_id,
                        table_id=table_id,
                        user_id=int(member_user_id),
                    )
                for target in list(action.get("dependencyTargets") or []):
                    await self._revalidate_dependency(
                        db,
                        user_id=user_id,
                        target=target,
                    )

            records_by_key: dict[tuple[str, str], TableRecord] = {}
            for table_id in affected_table_ids:
                record_ids = [
                    record_id
                    for source_table, record_id in grouped_updates
                    if source_table == table_id
                ]
                if not record_ids:
                    continue
                result = await db.execute(
                    select(TableRecord)
                    .where(
                        TableRecord.table_id == table_id,
                        TableRecord.id.in_(record_ids),
                    )
                    .with_for_update()
                )
                for record in result.scalars().all():
                    records_by_key[(table_id, str(record.id))] = record

            for key, record_actions in grouped_updates.items():
                record = records_by_key.get(key)
                if record is None:
                    raise AiActionPlanError("One or more Action Plan records no longer exist")
                expected = int((batch.record_versions or {}).get(_record_key(*key)) or 1)
                if record_version(record) != expected:
                    raise AiActionPlanError(
                        f"Record {key[1]} changed after preview; regenerate the Action Plan before applying"
                    )
                for action in record_actions:
                    field_id = str(action.get("fieldId") or "")
                    field = table_field_by_id[key[0]].get(field_id)
                    if field is None or field.type != str(action.get("fieldType") or ""):
                        raise AiActionPlanError("Action target field changed after preview")
                    self._validate_stored_field_value(field, action.get("suggestedValue"))

            for action in create_actions:
                table_id = str(action["tableId"])
                new_record_id = str(action["recordId"])
                existing = await db.get(
                    TableRecord,
                    {"id": new_record_id, "table_id": table_id},
                )
                if existing is not None:
                    raise AiActionPlanError("Previewed create-task record id already exists")
                field_types = dict(action.get("fieldTypes") or {})
                for field_id, expected_type in field_types.items():
                    field = table_field_by_id[table_id].get(str(field_id))
                    if field is None or field.type != str(expected_type):
                        raise AiActionPlanError("Create-task schema changed after preview")
                    self._validate_stored_field_value(
                        field,
                        (action.get("suggestedValue") or {}).get(field_id),
                    )
                for member_user_id in list(action.get("memberUserIds") or []):
                    await self._revalidate_member(
                        db,
                        workspace_id=batch.workspace_id,
                        table_id=table_id,
                        user_id=int(member_user_id),
                    )
                for target in list(action.get("dependencyTargets") or []):
                    await self._revalidate_dependency(
                        db,
                        user_id=user_id,
                        target=target,
                    )

            history_items: list[dict[str, Any]] = []
            next_record_versions = dict(batch.record_versions or {})

            for key, record_actions in grouped_updates.items():
                record = records_by_key[key]
                before_data = dict(record.data or {})
                after_data = dict(before_data)
                for action in record_actions:
                    after_data[str(action["fieldId"])] = _clone(action.get("suggestedValue"))
                changed = changed_fields(before_data, after_data)
                before_version = record_version(record)
                if changed:
                    before_meta = record_meta(record)
                    record.data = after_data
                    record.version = before_version + 1
                    history_items.append(
                        {
                            "table_id": key[0],
                            "entity_type": "record",
                            "entity_id": key[1],
                            "before_data": before_data,
                            "after_data": after_data,
                            "before_meta": before_meta,
                            "after_meta": record_meta(record),
                            "version_before": before_version,
                            "version_after": record.version,
                            "changed_fields": changed,
                        }
                    )
                    next_record_versions[_record_key(*key)] = record.version
                else:
                    next_record_versions[_record_key(*key)] = before_version

            create_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for action in create_actions:
                create_by_table[str(action["tableId"])].append(action)

            for table_id, table_actions in create_by_table.items():
                fields = table_fields[table_id]
                auto_values: dict[str, list[int]] = {}
                for field in fields:
                    if is_auto_number_field({"type": field.type}):
                        values, next_property = allocate_auto_number_values(
                            field.property,
                            len(table_actions),
                        )
                        auto_values[field.id] = values
                        field.property = next_property

                max_result = await db.execute(
                    select(func.max(TableRecord.order_index)).where(
                        TableRecord.table_id == table_id
                    )
                )
                max_order = int(max_result.scalar() or 0)
                for index, action in enumerate(table_actions):
                    proposed = dict(action.get("suggestedValue") or {})
                    data: dict[str, Any] = {}
                    for field in fields:
                        if field.id in auto_values:
                            data[field.id] = auto_values[field.id][index]
                        else:
                            data[field.id] = _clone(proposed.get(field.id)) if field.id in proposed else None
                    record = TableRecord(
                        id=str(action["recordId"]),
                        table_id=table_id,
                        data=data,
                        order_index=max_order + index + 1,
                        created_by_user_id=user_id,
                        version=1,
                    )
                    db.add(record)
                    history_items.append(
                        {
                            "table_id": table_id,
                            "entity_type": "record",
                            "entity_id": record.id,
                            "before_data": None,
                            "after_data": data,
                            "before_meta": None,
                            "after_meta": record_meta(record),
                            "version_before": None,
                            "version_after": 1,
                            "changed_fields": changed_fields(None, data),
                        }
                    )
                    next_record_versions[_record_key(table_id, record.id)] = 1

            change_set = None
            if history_items:
                primary_table = affected_table_ids[0]
                change_set = await append_change_set(
                    db,
                    table_id=primary_table,
                    actor_id=user_id,
                    actor_type="ai",
                    operation="ai_action_apply",
                    source="ai_action_plan",
                    trace_id=batch.trace_id,
                    summary=f"Apply {len(pending_ids)} AI Action Plan action(s)",
                    items=history_items,
                )

            applied.update(pending_ids)
            change_set_ids = list(batch.change_set_ids or [])
            if change_set is not None and change_set.id not in change_set_ids:
                change_set_ids.append(change_set.id)
            payload["appliedActionIds"] = sorted(applied)
            payload["changeSetIds"] = change_set_ids
            batch.result_payload = payload
            batch.record_versions = next_record_versions
            batch.change_set_ids = change_set_ids
            all_action_ids = set(actions)
            batch.status = "applied" if all_action_ids <= applied else "partially_applied"
            batch.apply_error = None
            batch.applied_at = datetime.now(timezone.utc)
            await db.commit()

            return {
                "planId": batch.id,
                "traceId": batch.trace_id,
                "status": batch.status,
                "idempotent": False,
                "appliedActionIds": sorted(applied),
                "changeSetIds": change_set_ids,
                "changeSetId": change_set.id if change_set is not None else None,
                "affectedTableIds": affected_table_ids,
            }
        except Exception as exc:
            await db.rollback()
            retry_result = await db.execute(
                select(AiActionPlanBatch).where(
                    AiActionPlanBatch.id == request.plan_id,
                    AiActionPlanBatch.user_id == user_id,
                )
            )
            retry = retry_result.scalars().first()
            if retry is not None:
                retry.apply_error = str(exc)[:4000]
                if retry.status == "previewed":
                    retry.status = "apply_failed"
                await db.commit()
            raise


ai_action_plan_service = AiActionPlanService()
