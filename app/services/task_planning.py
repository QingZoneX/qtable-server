from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.task_split import TaskSplitPlan as TaskSplitPlanModel
from app.schemas.task_planning import (
    TaskPlanningApplyRequest,
    TaskPlanningDecision,
    TaskPlanningPreviewRequest,
)
from app.schemas.task_split import TaskSplitPlan, TaskSplitRequest
from app.services.auto_number_engine import (
    allocate_auto_number_values,
    is_auto_number_field,
)
from app.services.change_history import (
    append_change_set,
    changed_fields,
    record_meta,
    record_version,
)
from app.services.row_permissions import filter_store_for_user, require_record_access
from app.services.smart_table_store import get_full_store
from app.services.task_split import task_split_service
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)


SEMANTIC_FIELDS: dict[str, dict[str, Any]] = {
    "title": {
        "names": ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"],
        "type": "text",
        "name": "任务名称",
    },
    "description": {
        "names": ["任务描述", "描述", "说明", "description", "details"],
        "type": "text",
        "name": "任务描述",
    },
    "deliverable": {
        "names": ["交付物", "交付成果", "deliverable", "deliverables"],
        "type": "text",
        "name": "交付物",
    },
    "acceptance": {
        "names": ["验收标准", "完成标准", "验收条件", "acceptance", "acceptance criteria"],
        "type": "text",
        "name": "验收标准",
    },
    "priority": {
        "names": ["优先级", "priority"],
        "type": "select",
        "name": "优先级",
        "options": [
            {"id": "low", "label": "低", "color": "#E5E7EB"},
            {"id": "medium", "label": "中", "color": "#DBEAFE"},
            {"id": "high", "label": "高", "color": "#FEF3C7"},
            {"id": "urgent", "label": "紧急", "color": "#FEE2E2"},
        ],
    },
    "parent": {
        "names": ["父任务", "上级任务", "parent task", "parent"],
        "type": "relation",
        "name": "父任务",
    },
    "dependencies": {
        "names": ["前置依赖", "依赖", "依赖任务", "dependencies", "depends on"],
        "type": "relation",
        "name": "前置依赖",
    },
    "milestone": {
        "names": ["里程碑", "milestone"],
        "type": "text",
        "name": "里程碑",
    },
    "suggestedRole": {
        "names": ["建议角色", "所需角色", "角色", "suggested role", "role"],
        "type": "text",
        "name": "建议角色",
    },
    "workload": {
        "names": ["预计工作量", "工作量", "预计工时", "工时", "workload", "estimate"],
        "type": "number",
        "name": "预计工作量",
        "property": {"suffix": "h", "precision": 1},
    },
    "sourceTrace": {
        "names": ["来源trace", "来源追踪", "来源", "source trace", "source"],
        "type": "text",
        "name": "来源追踪",
    },
}

WRITE_TYPE_COMPATIBILITY: dict[str, set[str]] = {
    "title": {"text"},
    "description": {"text"},
    "deliverable": {"text"},
    "acceptance": {"text"},
    "priority": {"select", "rating", "text"},
    "parent": {"relation"},
    "dependencies": {"relation"},
    "milestone": {"text", "select"},
    "suggestedRole": {"text"},
    "workload": {"number", "rating"},
    "sourceTrace": {"text", "url"},
}


class TaskPlanningError(ValueError):
    pass


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _normalized_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\s\-_—–·,，。.!！?？:：;；/\\()（）\[\]【】]+", "", text)


def _new_field_id(semantic: str, used: set[str]) -> str:
    base = "f_tp_" + re.sub(r"[^a-zA-Z0-9]+", "_", semantic).strip("_").lower()
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _iter_nodes(
    root,
    parent_key: Optional[str] = None,
) -> Iterable[tuple[Any, Optional[str]]]:
    yield root, parent_key
    for child in root.children:
        yield from _iter_nodes(child, root.key)


def _non_root_nodes(plan: TaskSplitPlan) -> list[tuple[Any, Optional[str]]]:
    all_nodes = list(_iter_nodes(plan.root))
    return all_nodes[1:]


def _source_fingerprint(request: TaskPlanningPreviewRequest) -> str:
    source = request.source_reference or {}
    stable_source: dict[str, Any] = {}
    for key in (
        "sourceId",
        "qnoteItemId",
        "clipperItemId",
        "canonicalUrl",
        "url",
        "anchor",
        "contentHash",
    ):
        value = source.get(key)
        if value not in (None, "", [], {}):
            stable_source[key] = value

    payload: dict[str, Any] = {
        "workspaceId": request.workspace_id,
        "targetTableId": request.target_table_id,
        "projectId": request.project_id,
        "parentTaskId": request.parent_task_id,
    }
    if stable_source:
        # A stable source identity must remain idempotent even if the user
        # paraphrases the planning prompt. allowRepeat is the explicit escape.
        payload["sourceReference"] = stable_source
    else:
        payload.update(
            {
                "goal": " ".join(request.goal.split()),
                "selectedRecordIds": sorted(request.selected_record_ids),
                "sourceReference": source,
            }
        )

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _field_dict(field: TableField) -> dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": _clone(field.options) if field.options else None,
        "property": _clone(field.property) if field.property else None,
        "orderIndex": field.order_index,
    }


def _field_matches(
    field: TableField,
    semantic: str,
    target_table_id: str,
) -> bool:
    if field.type not in WRITE_TYPE_COMPATIBILITY[semantic]:
        return False
    if semantic in {"parent", "dependencies"}:
        relation = field.property or {}
        if str(relation.get("targetTableId") or "") != target_table_id:
            return False
        multiple = bool(relation.get("multiple", True))
        if semantic == "parent" and multiple:
            return False
        if semantic == "dependencies" and not multiple:
            return False
    normalized_name = _normalized_text(field.name)
    return any(
        normalized_name == _normalized_text(candidate)
        or _normalized_text(candidate) in normalized_name
        for candidate in SEMANTIC_FIELDS[semantic]["names"]
    )


def _build_field_mapping(
    fields: list[TableField],
    target_table_id: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    mapping: dict[str, str] = {}
    additions: list[dict[str, Any]] = []
    used_ids = {field.id for field in fields}
    mapped_existing_ids: set[str] = set()

    for semantic in SEMANTIC_FIELDS:
        match = next(
            (
                field
                for field in fields
                if field.id not in mapped_existing_ids
                and _field_matches(field, semantic, target_table_id)
            ),
            None,
        )
        if match:
            mapping[semantic] = match.id
            mapped_existing_ids.add(match.id)

    if "title" not in mapping:
        fallback = next(
            (
                field
                for field in fields
                if field.type == "text"
                and field.id not in mapped_existing_ids
            ),
            None,
        )
        if fallback:
            mapping["title"] = fallback.id
            mapped_existing_ids.add(fallback.id)

    for semantic, spec in SEMANTIC_FIELDS.items():
        if semantic in mapping:
            continue
        field_id = _new_field_id(semantic, used_ids)
        mapping[semantic] = field_id
        additions.append(
            {
                "semantic": semantic,
                "id": field_id,
                "name": spec["name"],
                "type": spec["type"],
                "options": _clone(spec.get("options")),
                "property": _clone(spec.get("property")),
            }
        )

    title_field_id = mapping["title"]
    for addition in additions:
        if addition["semantic"] in {"parent", "dependencies"}:
            addition["property"] = {
                "targetTableId": target_table_id,
                "displayFieldId": title_field_id,
                "multiple": addition["semantic"] == "dependencies",
            }

    return mapping, additions


def _selected_context(
    store: Mapping[str, Any],
    selected_record_ids: list[str],
) -> str:
    if not selected_record_ids:
        return ""
    records = {
        str(record.get("id")): record
        for record in store.get("records", [])
        if record.get("id") is not None
    }
    missing = [record_id for record_id in selected_record_ids if record_id not in records]
    if missing:
        raise PermissionError("Selected records not found or no access")

    field_names = {
        str(field.get("id")): str(field.get("name") or field.get("id"))
        for field in store.get("fields", [])
        if field.get("id") is not None
    }
    rows: list[dict[str, Any]] = []
    for record_id in selected_record_ids[:20]:
        record = records[record_id]
        row: dict[str, Any] = {"recordId": record_id}
        for field_id, value in record.items():
            if field_id == "id":
                continue
            label = field_names.get(str(field_id), str(field_id))
            if value not in (None, "", [], {}):
                row[label] = value
            if len(row) >= 12:
                break
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)[:8000]


def _build_planning_prompt(
    request: TaskPlanningPreviewRequest,
    selected_context: str,
) -> str:
    sections = [request.goal.strip()]
    constraints: list[str] = []
    if request.deadline:
        constraints.append(f"目标截止时间：{request.deadline}")
    if request.team_size:
        constraints.append(f"团队规模：{request.team_size} 人")
    constraints.append(f"拆分粒度：{request.granularity}")
    if request.parent_task_id:
        constraints.append("这是对一条已有父任务继续向下拆解，根节点只作为规划容器。")
    if constraints:
        sections.append("\n".join(constraints))
    if selected_context:
        sections.append("用户选中的现有任务/记录上下文：\n" + selected_context)
    if request.source_reference:
        source_text = json.dumps(request.source_reference, ensure_ascii=False)
        sections.append("来源资料引用：\n" + source_text[:5000])
    sections.append(
        "输出要求：根节点仅代表本次规划目标，真正需要写入 QTable 的执行任务从 root.children 开始。"
        "每个执行任务必须有清晰标题、描述、交付物、验收标准、priority、milestone、suggestedRole、工时和关键 dependsOn。"
    )
    return "\n\n".join(sections)


def _similarity(left: str, right: str) -> float:
    a = _normalized_text(left)
    b = _normalized_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    left_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", str(left).casefold()))
    right_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", str(right).casefold()))
    jaccard = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0.0
    )
    return round(max(sequence, jaccard), 4)


def _duplicate_candidates(
    plan: TaskSplitPlan,
    store: Mapping[str, Any],
    field_mapping: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    title_field_id = field_mapping.get("title")
    if not title_field_id:
        return {}
    description_field_id = field_mapping.get("description")
    existing: list[tuple[str, str, str]] = []
    for record in store.get("records", []):
        record_id = str(record.get("id") or "")
        title = str(record.get(title_field_id) or "").strip()
        if not record_id or not title:
            continue
        description = (
            str(record.get(description_field_id) or "").strip()
            if description_field_id
            else ""
        )
        existing.append((record_id, title, description))

    output: dict[str, list[dict[str, Any]]] = {}
    for node, _parent_key in _non_root_nodes(plan):
        candidates: list[dict[str, Any]] = []
        for record_id, title, description in existing:
            title_score = _similarity(node.title, title)
            description_score = (
                _similarity(node.description, description)
                if node.description and description
                else 0.0
            )
            score = round(max(title_score, title_score * 0.85 + description_score * 0.15), 4)
            if score < 0.68:
                continue
            reason = (
                "标题完全一致"
                if score >= 0.995
                else "标题/描述高度相似"
                if score >= 0.86
                else "存在相似任务"
            )
            candidates.append(
                {
                    "recordId": record_id,
                    "title": title,
                    "score": score,
                    "reason": reason,
                    "recommendedAction": "reuse" if score >= 0.9 else "review",
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["title"], item["recordId"]))
        if candidates:
            output[node.key] = candidates[:3]
    return output


def _priority_value(field: TableField, priority: str) -> Any:
    if field.type == "rating":
        return {"low": 1, "medium": 2, "high": 4, "urgent": 5}.get(priority, 2)
    if field.type == "select":
        aliases = {
            "low": {"low", "低"},
            "medium": {"medium", "中", "普通"},
            "high": {"high", "高"},
            "urgent": {"urgent", "紧急"},
        }
        for option in field.options or []:
            option_id = str(option.get("id") or "")
            label = str(option.get("label") or "")
            if option_id.casefold() in aliases.get(priority, set()) or label.casefold() in aliases.get(priority, set()):
                return option_id
        return None
    return priority


def _select_or_text_value(field: TableField, value: str) -> Any:
    if field.type != "select":
        return value
    normalized = _normalized_text(value)
    for option in field.options or []:
        if _normalized_text(option.get("label")) == normalized or _normalized_text(option.get("id")) == normalized:
            return option.get("id")
    return None


def _node_scalar_values(
    node,
    *,
    fields_by_id: Mapping[str, TableField],
    mapping: Mapping[str, str],
    plan_id: str,
    trace_id: str,
    source_reference: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def put(semantic: str, value: Any) -> None:
        field_id = mapping.get(semantic)
        field = fields_by_id.get(field_id or "")
        if not field or value is None:
            return
        values[field.id] = value

    put("title", node.title)
    put(
        "description",
        "\n\n".join(
            item
            for item in [node.description.strip(), node.objective.strip()]
            if item
        ),
    )
    put("deliverable", "\n".join(node.deliverables))
    put("acceptance", "\n".join(node.acceptance_criteria))

    priority_id = mapping.get("priority")
    priority_field = fields_by_id.get(priority_id or "")
    if priority_field:
        put("priority", _priority_value(priority_field, node.priority))

    milestone_id = mapping.get("milestone")
    milestone_field = fields_by_id.get(milestone_id or "")
    if milestone_field:
        put("milestone", _select_or_text_value(milestone_field, node.milestone))

    put("suggestedRole", node.suggested_role)
    put("workload", round(float(node.estimate.buffered_hours or node.estimate.likely_hours or 0.0), 2))
    trace_payload = {
        "planId": plan_id,
        "traceId": trace_id,
        "nodeKey": node.key,
        "sourceReference": source_reference or node.source_reference,
    }
    put("sourceTrace", json.dumps(trace_payload, ensure_ascii=False, separators=(",", ":")))
    return values


class TaskPlanningService:
    async def _load_visible_target_store(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        target_table_id: str,
        table_permission: str,
    ) -> dict[str, Any]:
        store = await get_full_store(db, target_table_id)
        visible_store, _ = await filter_store_for_user(
            db,
            target_table_id,
            store,
            user_id=user_id,
            table_permission=table_permission,
        )
        return visible_store

    async def _require_target_table(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        target_table_id: str,
        permission: str = "edit",
    ) -> str:
        result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == target_table_id,
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "table",
            )
        )
        if result.scalars().first() is None:
            raise TaskPlanningError("Target task table not found or no access")
        effective = await get_effective_permission_for_item(db, user_id, target_table_id)
        if not permission_allows(effective, permission):
            raise PermissionError("Target task table not found or no access")
        return effective

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: TaskPlanningPreviewRequest,
    ) -> dict[str, Any]:
        table_permission = await self._require_target_table(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            target_table_id=request.target_table_id,
            permission="edit",
        )
        store = await self._load_visible_target_store(
            db,
            user_id=user_id,
            target_table_id=request.target_table_id,
            table_permission=table_permission,
        )
        selected_context = _selected_context(store, request.selected_record_ids)
        if request.parent_task_id:
            await require_record_access(
                db,
                request.target_table_id,
                request.parent_task_id,
                user_id=user_id,
                table_permission=table_permission,
            )

        prompt = _build_planning_prompt(request, selected_context)
        split_request = TaskSplitRequest(
            prompt=prompt,
            autoCreateRecords=True,
            dryRun=False,
            maxDepth=request.max_depth,
            maxChildrenPerNode=request.max_children_per_node,
            workspaceId=request.workspace_id,
            projectId=request.project_id,
            tableIds=[request.target_table_id],
            taskId=request.parent_task_id,
            locale=request.locale,
            timezone=request.timezone,
        )
        response = await task_split_service.run(
            db=db,
            user_id=user_id,
            request=split_request,
        )
        if response.error or not response.plan_id:
            raise TaskPlanningError(
                str((response.error or {}).get("message") or "Task planning failed")
            )

        fields_result = await db.execute(
            select(TableField)
            .where(TableField.table_id == request.target_table_id)
            .order_by(TableField.order_index)
        )
        fields = list(fields_result.scalars().all())
        mapping, additions = _build_field_mapping(fields, request.target_table_id)
        duplicates = _duplicate_candidates(response.result, store, mapping)
        fingerprint = _source_fingerprint(request)

        plan_row = await db.get(TaskSplitPlanModel, response.plan_id)
        if plan_row is None:
            raise TaskPlanningError("Task planning trace was not persisted")
        plan_row.target_table_id = request.target_table_id
        plan_row.source_fingerprint = fingerprint
        plan_row.source_reference = _clone(request.source_reference) if request.source_reference else None
        plan_row.selected_record_ids = list(request.selected_record_ids)
        plan_row.application_status = "previewed"
        await db.commit()

        previous_result = await db.execute(
            select(TaskSplitPlanModel)
            .where(
                TaskSplitPlanModel.id != plan_row.id,
                TaskSplitPlanModel.created_by == str(user_id),
                TaskSplitPlanModel.workspace_id == request.workspace_id,
                TaskSplitPlanModel.target_table_id == request.target_table_id,
                TaskSplitPlanModel.source_fingerprint == fingerprint,
                TaskSplitPlanModel.application_status.in_(["applied", "deduplicated"]),
            )
            .order_by(TaskSplitPlanModel.applied_at.desc())
            .limit(1)
        )
        previous = previous_result.scalars().first()
        return {
            "planId": response.plan_id,
            "traceId": response.trace_id,
            "provider": response.provider,
            "model": response.model,
            "plan": response.result.model_dump(mode="json", by_alias=True),
            "fieldMapping": mapping,
            "schemaAdditions": additions,
            "duplicates": duplicates,
            "sourceFingerprint": fingerprint,
            "parentTaskId": request.parent_task_id,
            "selectedRecordIds": list(request.selected_record_ids),
            "previousApplication": (
                {
                    "planId": previous.id,
                    "appliedRecords": previous.applied_records,
                    "appliedAt": previous.applied_at.isoformat() if previous.applied_at else None,
                }
                if previous
                else None
            ),
        }

    def _validate_final_plan(self, plan: TaskSplitPlan) -> TaskSplitPlan:
        copied = plan.model_copy(deep=True)
        seen: set[str] = set()

        def walk(node, depth: int) -> None:
            if not node.key or node.key in seen:
                raise TaskPlanningError("Task node keys must be unique and non-empty")
            if depth > 4:
                raise TaskPlanningError("Task tree depth cannot exceed 4")
            if not node.title.strip():
                raise TaskPlanningError("Task title cannot be empty")
            seen.add(node.key)
            node.depth = depth
            if not node.children:
                if not node.deliverables:
                    raise TaskPlanningError(f"Leaf task {node.key} must have a deliverable")
                if not node.acceptance_criteria:
                    raise TaskPlanningError(
                        f"Leaf task {node.key} must have acceptance criteria"
                    )
            for child in node.children:
                walk(child, depth + 1)

        walk(copied.root, 0)
        copied.dependencies = task_split_service._derive_dependency_specs(copied)
        task_split_service._assert_dependency_acyclic(copied)
        copied.mermaid = task_split_service._generate_mermaid(copied)
        return copied

    async def _load_fields_for_update(
        self,
        db: AsyncSession,
        target_table_id: str,
    ) -> list[TableField]:
        result = await db.execute(
            select(TableField)
            .where(TableField.table_id == target_table_id)
            .order_by(TableField.order_index)
            .with_for_update()
        )
        return list(result.scalars().all())

    async def apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: TaskPlanningApplyRequest,
    ) -> dict[str, Any]:
        table_permission = await self._require_target_table(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            target_table_id=request.target_table_id,
            permission="edit",
        )
        plan_result = await db.execute(
            select(TaskSplitPlanModel)
            .where(
                TaskSplitPlanModel.id == request.plan_id,
                TaskSplitPlanModel.created_by == str(user_id),
                TaskSplitPlanModel.workspace_id == request.workspace_id,
                TaskSplitPlanModel.target_table_id == request.target_table_id,
            )
            .with_for_update()
        )
        plan_row = plan_result.scalars().first()
        if plan_row is None:
            raise TaskPlanningError("Task planning preview not found or no access")

        if plan_row.application_status in {"applied", "deduplicated"} and plan_row.applied_records:
            return {
                "planId": plan_row.id,
                "traceId": plan_row.trace_id,
                "status": plan_row.application_status,
                "idempotent": True,
                "result": _clone(plan_row.applied_records),
            }

        if plan_row.source_fingerprint and not request.allow_repeat:
            previous_result = await db.execute(
                select(TaskSplitPlanModel)
                .where(
                    TaskSplitPlanModel.id != plan_row.id,
                    TaskSplitPlanModel.created_by == str(user_id),
                    TaskSplitPlanModel.workspace_id == request.workspace_id,
                    TaskSplitPlanModel.target_table_id == request.target_table_id,
                    TaskSplitPlanModel.source_fingerprint == plan_row.source_fingerprint,
                    TaskSplitPlanModel.application_status.in_(["applied", "deduplicated"]),
                )
                .order_by(TaskSplitPlanModel.applied_at.desc())
                .limit(1)
            )
            previous = previous_result.scalars().first()
            if previous and previous.applied_records:
                plan_row.application_status = "deduplicated"
                plan_row.applied_records = _clone(previous.applied_records)
                plan_row.edited_output = request.plan.model_dump(mode="json", by_alias=True)
                plan_row.applied_at = datetime.now(timezone.utc)
                await db.commit()
                return {
                    "planId": plan_row.id,
                    "traceId": plan_row.trace_id,
                    "status": "deduplicated",
                    "idempotent": True,
                    "reusedPreviousPlanId": previous.id,
                    "result": _clone(previous.applied_records),
                }

        final_plan = self._validate_final_plan(request.plan)
        nodes_with_parent = _non_root_nodes(final_plan)
        node_by_key = {node.key: node for node, _ in nodes_with_parent}
        parent_by_key = {node.key: parent for node, parent in nodes_with_parent}
        decisions = {decision.node_key: decision for decision in request.decisions}
        unknown_decisions = [key for key in decisions if key not in node_by_key]
        if unknown_decisions:
            raise TaskPlanningError(f"Unknown decision node: {unknown_decisions[0]}")

        if not nodes_with_parent:
            raise TaskPlanningError("Task plan contains no executable tasks")

        try:
            fields = await self._load_fields_for_update(db, request.target_table_id)
            mapping, additions = _build_field_mapping(fields, request.target_table_id)
            max_field_order = max([field.order_index or 0 for field in fields], default=-1)
            for index, addition in enumerate(additions):
                field = TableField(
                    id=addition["id"],
                    table_id=request.target_table_id,
                    name=addition["name"],
                    type=addition["type"],
                    options=_clone(addition.get("options")),
                    property=_clone(addition.get("property")),
                    order_index=max_field_order + index + 1,
                )
                db.add(field)
                fields.append(field)
            await db.flush()
            fields_by_id = {field.id: field for field in fields}

            parent_task_record: Optional[TableRecord] = None
            parent_task_id = plan_row.task_id
            if parent_task_id:
                parent_task_record = await require_record_access(
                    db,
                    request.target_table_id,
                    parent_task_id,
                    user_id=user_id,
                    table_permission=table_permission,
                )

            record_by_key: dict[str, str] = {}
            created_records: dict[str, TableRecord] = {}
            merged_records: dict[str, tuple[TableRecord, dict[str, Any], dict[str, Any], int]] = {}
            reused: list[dict[str, Any]] = []
            skipped: list[str] = []
            max_order_result = await db.execute(
                select(func.max(TableRecord.order_index)).where(
                    TableRecord.table_id == request.target_table_id
                )
            )
            next_order = int(max_order_result.scalar() or 0) + 1

            create_nodes = [
                node
                for node, _parent in nodes_with_parent
                if decisions.get(node.key, TaskPlanningDecision(nodeKey=node.key)).action
                == "create"
            ]
            auto_values: dict[str, list[int]] = {}
            for field in fields:
                if is_auto_number_field({"type": field.type}):
                    allocated, next_property = allocate_auto_number_values(
                        field.property,
                        len(create_nodes),
                    )
                    auto_values[field.id] = allocated
                    field.property = next_property
            create_index = 0

            for node, _parent_key in nodes_with_parent:
                decision = decisions.get(
                    node.key,
                    TaskPlanningDecision(nodeKey=node.key, action="create"),
                )
                if decision.action == "skip":
                    skipped.append(node.key)
                    continue
                if decision.action in {"reuse", "merge"}:
                    record = await require_record_access(
                        db,
                        request.target_table_id,
                        str(decision.record_id),
                        user_id=user_id,
                        table_permission=table_permission,
                    )
                    record_by_key[node.key] = record.id
                    if decision.action == "reuse":
                        reused.append(
                            {
                                "nodeKey": node.key,
                                "recordId": record.id,
                                "action": "reuse",
                            }
                        )
                        continue

                    before_data = dict(record.data or {})
                    before_meta = record_meta(record)
                    before_version = record_version(record)
                    generated = _node_scalar_values(
                        node,
                        fields_by_id=fields_by_id,
                        mapping=mapping,
                        plan_id=plan_row.id,
                        trace_id=plan_row.trace_id or "",
                        source_reference=plan_row.source_reference,
                    )
                    next_data = dict(before_data)
                    for field_id, value in generated.items():
                        if next_data.get(field_id) in (None, "", [], {}):
                            next_data[field_id] = value
                    record.data = next_data
                    merged_records[node.key] = (
                        record,
                        before_data,
                        before_meta,
                        before_version,
                    )
                    continue

                record_id = "rtp_" + uuid.uuid4().hex
                data = {field.id: None for field in fields}
                for field_id, values in auto_values.items():
                    data[field_id] = values[create_index]
                data.update(
                    _node_scalar_values(
                        node,
                        fields_by_id=fields_by_id,
                        mapping=mapping,
                        plan_id=plan_row.id,
                        trace_id=plan_row.trace_id or "",
                        source_reference=plan_row.source_reference,
                    )
                )
                record = TableRecord(
                    id=record_id,
                    table_id=request.target_table_id,
                    data=data,
                    order_index=next_order,
                    created_by_user_id=user_id,
                    version=1,
                )
                next_order += 1
                create_index += 1
                db.add(record)
                created_records[node.key] = record
                record_by_key[node.key] = record_id

            await db.flush()

            block_dependencies: dict[str, list[str]] = {}
            for dependency in final_plan.dependencies:
                if dependency.dependency_type != "blocks":
                    continue
                block_dependencies.setdefault(dependency.successor_key, []).append(
                    dependency.predecessor_key
                )

            parent_field_id = mapping.get("parent")
            dependency_field_id = mapping.get("dependencies")

            def nearest_parent_record(node_key: str) -> Optional[str]:
                parent_key = parent_by_key.get(node_key)
                while parent_key:
                    if parent_key in record_by_key:
                        return record_by_key[parent_key]
                    parent_key = parent_by_key.get(parent_key)
                return parent_task_record.id if parent_task_record else None

            for node, _parent_key in nodes_with_parent:
                record_id = record_by_key.get(node.key)
                if not record_id:
                    continue
                if node.key in created_records:
                    record = created_records[node.key]
                    next_data = dict(record.data or {})
                    parent_id = nearest_parent_record(node.key)
                    if parent_field_id:
                        next_data[parent_field_id] = parent_id
                    if dependency_field_id:
                        dependency_ids = [
                            record_by_key[key]
                            for key in block_dependencies.get(node.key, [])
                            if key in record_by_key
                        ]
                        next_data[dependency_field_id] = list(dict.fromkeys(dependency_ids))
                    record.data = next_data
                elif node.key in merged_records:
                    record, before_data, before_meta, before_version = merged_records[node.key]
                    next_data = dict(record.data or {})
                    parent_id = nearest_parent_record(node.key)
                    if parent_field_id and next_data.get(parent_field_id) in (None, "", []):
                        next_data[parent_field_id] = parent_id
                    if dependency_field_id and next_data.get(dependency_field_id) in (None, "", []):
                        next_data[dependency_field_id] = [
                            record_by_key[key]
                            for key in block_dependencies.get(node.key, [])
                            if key in record_by_key
                        ]
                    record.data = next_data
                    merged_records[node.key] = (
                        record,
                        before_data,
                        before_meta,
                        before_version,
                    )

            history_items: list[dict[str, Any]] = []
            for node_key, record in created_records.items():
                history_items.append(
                    {
                        "table_id": request.target_table_id,
                        "entity_type": "record",
                        "entity_id": record.id,
                        "before_data": None,
                        "after_data": dict(record.data or {}),
                        "before_meta": None,
                        "after_meta": record_meta(record),
                        "version_before": None,
                        "version_after": 1,
                        "changed_fields": changed_fields(None, record.data or {}),
                    }
                )

            merged_out: list[dict[str, Any]] = []
            for node_key, (
                record,
                before_data,
                before_meta,
                before_version,
            ) in merged_records.items():
                after_data = dict(record.data or {})
                changed = changed_fields(before_data, after_data)
                if changed:
                    record.version = before_version + 1
                    history_items.append(
                        {
                            "table_id": request.target_table_id,
                            "entity_type": "record",
                            "entity_id": record.id,
                            "before_data": before_data,
                            "after_data": after_data,
                            "before_meta": before_meta,
                            "after_meta": record_meta(record),
                            "version_before": before_version,
                            "version_after": record.version,
                            "changed_fields": changed,
                        }
                    )
                merged_out.append(
                    {
                        "nodeKey": node_key,
                        "recordId": record.id,
                        "action": "merge",
                        "changedFields": changed,
                    }
                )

            change_set = await append_change_set(
                db,
                table_id=request.target_table_id,
                actor_id=user_id,
                actor_type="ai",
                operation="task_planning_apply",
                source="task_planning",
                trace_id=plan_row.trace_id,
                summary=f"Apply task plan {plan_row.id}: {len(created_records)} created, {len(merged_records)} merged",
                items=history_items,
            )

            result_payload = {
                "recordByNodeKey": record_by_key,
                "created": [
                    {
                        "nodeKey": node_key,
                        "recordId": record.id,
                        "title": node_by_key[node_key].title,
                    }
                    for node_key, record in created_records.items()
                ],
                "reused": reused,
                "merged": merged_out,
                "skipped": skipped,
                "fieldMapping": mapping,
                "schemaAdditions": additions,
                "changeSetId": change_set.id,
                "parentTaskId": parent_task_record.id if parent_task_record else None,
            }
            plan_row.edited_output = final_plan.model_dump(mode="json", by_alias=True)
            plan_row.applied_records = _clone(result_payload)
            plan_row.application_status = "applied"
            plan_row.apply_error = None
            plan_row.applied_at = datetime.now(timezone.utc)
            await db.commit()
            return {
                "planId": plan_row.id,
                "traceId": plan_row.trace_id,
                "status": "applied",
                "idempotent": False,
                "result": result_payload,
            }
        except Exception as exc:
            await db.rollback()
            retry_result = await db.execute(
                select(TaskSplitPlanModel).where(
                    TaskSplitPlanModel.id == request.plan_id,
                    TaskSplitPlanModel.created_by == str(user_id),
                )
            )
            retry_row = retry_result.scalars().first()
            if retry_row:
                retry_row.application_status = "failed"
                retry_row.apply_error = str(exc)[:2000]
                retry_row.edited_output = request.plan.model_dump(mode="json", by_alias=True)
                await db.commit()
            raise

    async def get_plan(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        plan_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(TaskSplitPlanModel).where(
                TaskSplitPlanModel.id == plan_id,
                TaskSplitPlanModel.created_by == str(user_id),
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "planId": row.id,
            "traceId": row.trace_id,
            "workspaceId": row.workspace_id,
            "targetTableId": row.target_table_id,
            "status": row.application_status,
            "plan": row.edited_output or row.structured_output,
            "appliedRecords": row.applied_records,
            "sourceReference": row.source_reference,
            "selectedRecordIds": row.selected_record_ids or [],
            "applyError": row.apply_error,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        }


task_planning_service = TaskPlanningService()
