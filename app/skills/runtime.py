from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.task_split import TaskSplitRequest, TaskSplitResponse
from app.services.task_split import task_split_service
from app.services.smart_table_store import (
    create_record,
    create_record_file,
    get_full_store,
    get_full_store_for_table,
    update_record_fields,
    update_record_file,
)
from app.services.workspace.permissions import (
    get_effective_permission_for_item,
    permission_allows,
)
from app.services.row_permissions import filter_store_for_user
from app.skills.contracts import (
    SkillCallRequest,
    SkillCallResponse,
    SkillContext,
    SkillError,
    SkillErrorCode,
    SkillLifecycleState,
    SkillManifestEntry,
    SkillMetadata,
    SkillPermissionRequirement,
    SkillSideEffect,
)


Handler = Callable[["SkillExecutionContext", BaseModel], Awaitable[dict[str, Any]]]


@dataclass
class SkillDefinition:
    metadata: SkillMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler

    def manifest(self) -> SkillManifestEntry:
        return SkillManifestEntry(
            metadata=self.metadata,
            input_schema=self.input_model.model_json_schema(by_alias=True),
            output_schema=self.output_model.model_json_schema(by_alias=True),
        )


@dataclass
class SkillExecutionContext:
    context: SkillContext
    db: Optional[AsyncSession] = None


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, definition: SkillDefinition) -> None:
        self._skills[definition.metadata.name] = definition

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def list_manifest(self) -> list[SkillManifestEntry]:
        manifests = [definition.manifest() for definition in self._skills.values()]
        manifests.sort(key=lambda item: item.metadata.name)
        return manifests


class SkillRuntimeError(Exception):
    def __init__(
        self,
        code: SkillErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class SkillAuthorizer:
    async def authorize(
        self,
        definition: SkillDefinition,
        call_input: dict[str, Any],
        context: SkillExecutionContext,
    ) -> None:
        if not context.context.user_id:
            raise SkillRuntimeError(
                SkillErrorCode.UNAUTHORIZED,
                "Unauthorized",
            )

        for requirement in definition.metadata.permissions:
            if requirement.resource != "table":
                continue
            await self._authorize_table_requirement(requirement, call_input, context)

    async def _authorize_table_requirement(
        self,
        requirement: SkillPermissionRequirement,
        call_input: dict[str, Any],
        context: SkillExecutionContext,
    ) -> None:
        table_param = requirement.target_param or "table_id"
        table_id = call_input.get(table_param)
        if not table_id and table_param == "table_id":
            table_id = call_input.get("tableId")
        if not table_id:
            if requirement.optional:
                return
            raise SkillRuntimeError(
                SkillErrorCode.INVALID_INPUT,
                f"Missing required permission target: {table_param}",
            )

        backend = (settings.DATA_BACKEND or "").lower()
        if backend not in {"db", "database", "postgres", "sqlite"}:
            return
        if context.db is None:
            return
        if table_id in {"dstDefault", "table"}:
            return

        permission = await get_effective_permission_for_item(
            context.db,
            int(context.context.user_id),
            str(table_id),
        )
        required_permission = "edit" if requirement.action == "write" else requirement.action
        if not permission or not permission_allows(permission, required_permission):
            raise SkillRuntimeError(
                SkillErrorCode.FORBIDDEN,
                "Forbidden",
                details={"tableId": str(table_id), "required": required_permission},
            )


class SkillRuntime:
    def __init__(self, registry: SkillRegistry, authorizer: SkillAuthorizer | None = None) -> None:
        self.registry = registry
        self.authorizer = authorizer or SkillAuthorizer()

    async def invoke(
        self,
        request: SkillCallRequest,
        context: SkillExecutionContext,
    ) -> SkillCallResponse:
        call_id = request.call_id or str(uuid.uuid4())
        definition = self.registry.get(request.skill_name)
        if not definition:
            return SkillCallResponse(
                call_id=call_id,
                skill_name=request.skill_name,
                state=SkillLifecycleState.FAILED,
                error=SkillError(
                    code=SkillErrorCode.SKILL_NOT_FOUND,
                    message=f"Skill not found: {request.skill_name}",
                ),
            )

        try:
            await self.authorizer.authorize(definition, request.input, context)
            validated_input = definition.input_model.model_validate(request.input)

            needs_confirmation = (
                definition.metadata.side_effect != SkillSideEffect.NONE
                and definition.metadata.confirmation_required
                and not request.confirmed
                and not request.dry_run
            )
            if needs_confirmation:
                return SkillCallResponse(
                    call_id=call_id,
                    skill_name=request.skill_name,
                    state=SkillLifecycleState.REQUIRES_CONFIRMATION,
                    requires_confirmation=True,
                    metadata={
                        "sideEffect": definition.metadata.side_effect.value,
                        "previewInput": validated_input.model_dump(mode="json"),
                    },
                )

            output = await definition.handler(context, validated_input)
            validated_output = definition.output_model.model_validate(output)
            return SkillCallResponse(
                call_id=call_id,
                skill_name=request.skill_name,
                state=SkillLifecycleState.COMPLETED,
                output=validated_output.model_dump(mode="json"),
                metadata={
                    "dryRun": request.dry_run,
                    "confirmed": request.confirmed,
                    "sideEffect": definition.metadata.side_effect.value,
                },
            )
        except SkillRuntimeError as exc:
            return SkillCallResponse(
                call_id=call_id,
                skill_name=request.skill_name,
                state=SkillLifecycleState.FAILED,
                error=SkillError(
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    details=exc.details or None,
                ),
            )
        except Exception as exc:
            return SkillCallResponse(
                call_id=call_id,
                skill_name=request.skill_name,
                state=SkillLifecycleState.FAILED,
                error=SkillError(
                    code=SkillErrorCode.EXECUTION_FAILED,
                    message=str(exc) or "Skill execution failed",
                ),
            )


class DescribeTableInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    include_sample_rows: bool = Field(default=True, alias="includeSampleRows")
    sample_limit: int = Field(default=5, ge=1, le=20, alias="sampleLimit")


class DescribeTableOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    field_count: int = Field(..., alias="fieldCount")
    record_count: int = Field(..., alias="recordCount")
    fields: list[dict[str, Any]]
    sample_rows: list[dict[str, Any]] = Field(default_factory=list, alias="sampleRows")


class CreateRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    values: dict[str, Any] = Field(default_factory=dict)


class CreateRecordOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    record_id: Optional[str] = Field(default=None, alias="recordId")
    values: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = Field(default=False, alias="dryRun")


async def handle_describe_table(
    context: SkillExecutionContext,
    data: DescribeTableInput,
) -> dict[str, Any]:
    backend = (settings.DATA_BACKEND or "").lower()
    if backend in {"db", "database", "postgres", "sqlite"}:
        if context.db is None:
            raise SkillRuntimeError(
                SkillErrorCode.EXECUTION_FAILED,
                "Database session is not available",
            )
        if context.context.user_id is None:
            raise SkillRuntimeError(
                SkillErrorCode.UNAUTHORIZED,
                "Unauthorized",
            )
        table_permission = await get_effective_permission_for_item(
            context.db,
            int(context.context.user_id),
            data.table_id,
        )
        if not permission_allows(table_permission, "read"):
            raise SkillRuntimeError(
                SkillErrorCode.FORBIDDEN,
                "Forbidden",
                details={"tableId": data.table_id, "required": "read"},
            )
        store = await get_full_store(context.db, data.table_id)
        store, _ = await filter_store_for_user(
            context.db,
            data.table_id,
            store,
            user_id=int(context.context.user_id),
            table_permission=table_permission,
        )
    else:
        store = await get_full_store_for_table(data.table_id)

    records = list(store.get("records", []))
    return {
        "tableId": data.table_id,
        "fieldCount": len(store.get("fields", [])),
        "recordCount": len(records),
        "fields": store.get("fields", []),
        "sampleRows": records[: data.sample_limit] if data.include_sample_rows else [],
    }


async def handle_create_record(
    context: SkillExecutionContext,
    data: CreateRecordInput,
) -> dict[str, Any]:
    if context.context.dry_run:
        return {
            "tableId": data.table_id,
            "recordId": None,
            "values": data.values,
            "dryRun": True,
        }

    backend = (settings.DATA_BACKEND or "").lower()
    if backend in {"db", "database", "postgres", "sqlite"}:
        if context.db is None:
            raise SkillRuntimeError(
                SkillErrorCode.EXECUTION_FAILED,
                "Database session is not available",
            )
        if context.context.user_id is None:
            raise SkillRuntimeError(
                SkillErrorCode.UNAUTHORIZED,
                "Unauthorized",
            )
        record = await create_record(
            context.db,
            data.table_id,
            created_by_user_id=int(context.context.user_id),
        )
        record_id = str(record.get("id"))
        updated = await update_record_fields(
            context.db,
            data.table_id,
            record_id,
            data.values,
            user_id=int(context.context.user_id),
        )
        return {
            "tableId": data.table_id,
            "recordId": record_id,
            "values": updated or data.values,
            "dryRun": False,
        }

    record = await create_record_file(data.table_id)
    record_id = str(record.get("id"))
    for field_id, value in data.values.items():
        await update_record_file(data.table_id, record_id, field_id, value)
    return {
        "tableId": data.table_id,
        "recordId": record_id,
        "values": data.values,
        "dryRun": False,
    }


class SplitTaskInput(TaskSplitRequest):
    prompt: str = Field(..., min_length=1)


class GenerateGanttInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_plan: dict[str, Any] = Field(..., alias="taskPlan")
    start_date: Optional[str] = Field(default=None, alias="startDate")
    working_hours_per_day: float = Field(default=6.0, ge=1.0, le=24.0, alias="workingHoursPerDay")


class GenerateGanttOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list, alias="criticalPath")
    summary: str = ""


class CreateTaskRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    task_plan: dict[str, Any] = Field(..., alias="taskPlan")
    gantt_data: Optional[dict[str, Any]] = Field(default=None, alias="ganttData")
    title_field_id: Optional[str] = Field(default=None, alias="titleFieldId")
    description_field_id: Optional[str] = Field(default=None, alias="descriptionFieldId")
    status_field_id: Optional[str] = Field(default=None, alias="statusFieldId")
    start_date_field_id: Optional[str] = Field(default=None, alias="startDateFieldId")
    end_date_field_id: Optional[str] = Field(default=None, alias="endDateFieldId")
    estimate_field_id: Optional[str] = Field(default=None, alias="estimateFieldId")
    dependency_field_id: Optional[str] = Field(default=None, alias="dependencyFieldId")
    dry_run: bool = Field(default=False, alias="dryRun")


class CreateTaskRecordsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(..., alias="tableId")
    total_count: int = Field(default=0, alias="totalCount")
    dry_run: bool = Field(default=False, alias="dryRun")
    mapped_field_ids: dict[str, Optional[str]] = Field(default_factory=dict, alias="mappedFieldIds")
    created_records: list[dict[str, Any]] = Field(default_factory=list, alias="createdRecords")


def _normalize_task_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def _flatten_task_nodes(node: dict[str, Any], *, include_root: bool = False) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    children = list(node.get("children") or [])
    should_include = include_root or not children or int(node.get("depth") or 0) > 0
    if should_include:
        flattened.append(node)
    for child in children:
        if isinstance(child, dict):
            flattened.extend(_flatten_task_nodes(child, include_root=True))
    return flattened


def _estimate_task_hours(node: dict[str, Any]) -> float:
    estimate = node.get("estimate") or {}
    if not isinstance(estimate, dict):
        return 0.0
    return float(
        estimate.get("bufferedHours")
        or estimate.get("likelyHours")
        or estimate.get("optimisticHours")
        or 0.0
    )


def _match_field_id(
    fields: list[dict[str, Any]],
    *,
    keywords: list[str],
    preferred_types: list[str] | None = None,
) -> Optional[str]:
    preferred_types = preferred_types or []
    normalized_keywords = [item.lower() for item in keywords]
    best_field: tuple[int, str] | None = None
    for field in fields:
        name = str(field.get("name") or "").lower()
        field_type = str(field.get("type") or "").lower()
        score = 0
        for keyword in normalized_keywords:
            if keyword in name:
                score += 10
        if preferred_types and field_type in {item.lower() for item in preferred_types}:
            score += 4
        if score <= 0:
            continue
        field_id = str(field.get("id") or "")
        candidate = (score, field_id)
        if best_field is None or candidate > best_field:
            best_field = candidate
    return best_field[1] if best_field else None


def _find_field_by_type(
    fields: list[dict[str, Any]],
    *,
    preferred_types: list[str],
    exclude_ids: set[str] | None = None,
) -> Optional[str]:
    """Find the first field matching one of the preferred types, excluding already-mapped fields."""
    exclude_ids = exclude_ids or set()
    for field in fields:
        field_type = str(field.get("type") or "").lower()
        field_id = str(field.get("id") or "")
        if field_id in exclude_ids:
            continue
        if field_type in {item.lower() for item in preferred_types}:
            return field_id
    return None


def _get_select_default_value(field: dict[str, Any]) -> str:
    """Get the first option label from a select field, or a safe fallback."""
    options = field.get("options") or []
    if options and isinstance(options[0], dict):
        return str(options[0].get("label") or options[0].get("id") or "未开始")
    return "未开始"


def _get_select_valid_options(field: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (labels, ids) for a select field's options, used for value validation."""
    options = field.get("options") or []
    labels: list[str] = []
    ids: list[str] = []
    for opt in options:
        if isinstance(opt, dict):
            label = str(opt.get("label") or "")
            opt_id = str(opt.get("id") or "")
            if label:
                labels.append(label)
            if opt_id:
                ids.append(opt_id)
    return labels, ids


def _validate_select_value(
    value: str,
    field: dict[str, Any],
    default_value: str,
) -> str:
    """Validate and normalize a select field value against the field's options.

    Returns the valid option label if matched, otherwise falls back to default_value.
    Supports fuzzy matching: if value matches an option's meaning in Chinese/English,
    it returns the correct label.
    """
    if not value:
        return default_value

    labels, opt_ids = _get_select_valid_options(field)
    if not labels:
        return value  # No options to validate against, accept as-is

    # Direct match (case-insensitive)
    value_lower = value.lower().strip()
    for label in labels:
        if label.lower() == value_lower:
            return label

    # Match by option id
    if value in opt_ids:
        idx = opt_ids.index(value)
        return labels[idx] if idx < len(labels) else default_value

    # Fuzzy semantic mapping for common status values
    semantic_map: dict[str, str] = {
        "pending": "未开始",
        "todo": "未开始",
        "not started": "未开始",
        "not_started": "未开始",
        "in progress": "进行中",
        "in_progress": "进行中",
        "doing": "进行中",
        "active": "进行中",
        "done": "已完成",
        "completed": "已完成",
        "complete": "已完成",
        "finished": "已完成",
        "closed": "已完成",
        "delayed": "已延迟",
        "late": "已延迟",
        "overdue": "已延迟",
        "blocked": "已延迟",
        "cancelled": "已延迟",
        "canceled": "已延迟",
    }
    mapped = semantic_map.get(value_lower)
    if mapped and mapped in labels:
        return mapped

    # If value doesn't match any valid option, fallback to default
    return default_value


def _convert_date_to_timestamp(value: Any, *, timezone_name: str = "Asia/Shanghai") -> int | None:
    """Convert various date formats to millisecond timestamp.

    Supports:
    - ISO string: "2026-05-25", "2026-05-25T10:00:00"
    - Already a timestamp integer
    - Date object / string that can be parsed

    Uses timezone-aware conversion to ensure correct date representation
    regardless of server timezone settings.

    Returns None if conversion fails.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # If it's already a reasonable millisecond timestamp
        return int(value)
    if isinstance(value, str):
        try:
            # Try timezone-aware full ISO format first
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                import zoneinfo
                dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(timezone_name))
            return int(dt.timestamp() * 1000)
        except (ValueError, Exception):
            pass
        try:
            # Try date-only format (YYYY-MM-DD) — timezone-aware
            d = date.fromisoformat(value)
            import zoneinfo
            tz = zoneinfo.ZoneInfo(timezone_name)
            dt = datetime(d.year, d.month, d.day, tzinfo=tz)
            return int(dt.timestamp() * 1000)
        except (ValueError, Exception):
            pass
    return None


def _normalize_member_value(field: dict[str, Any], value: Any) -> Any:
    """Normalize member field value to the correct array format.

    Member fields expect an array of user IDs like ["u1"].
    If a single string is passed, wrap it in an array.
    """
    if value is None:
        return None
    field_type = str(field.get("type") or "").lower()
    if field_type != "member":
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return [str(value)]


def _convert_flat_tasks_to_nodes(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a flat tasks array into the tree structure expected by task record creation.

    Input: [{title, assignee, startDate, endDate, priority, status, budget, description}, ...]
    Output: {root: {key, title, children: [{key, title, ...}, ...]}}
    """
    children: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        node_key = task.get("key") or f"flat-task-{i}"
        node = {
            "key": node_key,
            "title": task.get("title") or task.get("name") or f"Task {i+1}",
            "depth": 1,
        }
        # Map common AI-generated task fields to node attributes
        if task.get("assignee"):
            node["assignee"] = task["assignee"]
        elif task.get("owner"):
            node["assignee"] = task["owner"]
        if task.get("startDate"):
            node["startDate"] = task["startDate"]
        if task.get("endDate"):
            node["endDate"] = task["endDate"]
        if task.get("description"):
            node["description"] = task["description"]
        if task.get("status"):
            node["status"] = task["status"]
        if task.get("priority") is not None:
            node["priority"] = task["priority"]
        if task.get("budget") is not None:
            node["budget"] = task["budget"]
        if task.get("progress") is not None:
            node["progress"] = task["progress"]
        children.append(node)

    if not children:
        return {}

    return {
        "root": {
            "key": "flat-root",
            "title": "Tasks",
            "children": children,
        }
    }


def _compute_gantt_items(task_plan: dict[str, Any], start_date_value: str | None, working_hours_per_day: float) -> dict[str, Any]:
    plan = _normalize_task_plan_payload(task_plan)
    root = plan.get("root") or {}
    nodes = _flatten_task_nodes(root)
    dependencies = list(plan.get("dependencies") or [])
    predecessor_map: dict[str, list[str]] = {}
    for item in dependencies:
        if not isinstance(item, dict):
            continue
        predecessor = str(item.get("predecessorKey") or "")
        successor = str(item.get("successorKey") or "")
        if predecessor and successor:
            predecessor_map.setdefault(successor, []).append(predecessor)

    if start_date_value:
        try:
            base_date = datetime.fromisoformat(start_date_value).date()
        except ValueError:
            base_date = date.today()
    else:
        base_date = date.today()

    remaining = {str(node.get("key") or f"node-{index}"): node for index, node in enumerate(nodes)}
    scheduled: dict[str, dict[str, Any]] = {}
    cursor_date = base_date
    while remaining:
        progressed = False
        for key in list(remaining.keys()):
            node = remaining[key]
            predecessors = predecessor_map.get(key, [])
            if any(predecessor not in scheduled for predecessor in predecessors):
                continue
            start = cursor_date
            if predecessors:
                latest_predecessor_end = max(
                    datetime.fromisoformat(scheduled[predecessor]["endDate"]).date()
                    for predecessor in predecessors
                )
                start = max(start, latest_predecessor_end)
            hours = max(1.0, _estimate_task_hours(node))
            duration_days = max(1, int((hours + working_hours_per_day - 1) // working_hours_per_day))
            end = start + timedelta(days=max(0, duration_days - 1))
            scheduled[key] = {
                "nodeKey": key,
                "title": node.get("title") or key,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "durationDays": duration_days,
                "estimatedHours": hours,
                "dependsOn": predecessors,
            }
            cursor_date = max(cursor_date, end)
            remaining.pop(key, None)
            progressed = True
        if not progressed:
            key, node = next(iter(remaining.items()))
            scheduled[key] = {
                "nodeKey": key,
                "title": node.get("title") or key,
                "startDate": cursor_date.isoformat(),
                "endDate": cursor_date.isoformat(),
                "durationDays": 1,
                "estimatedHours": _estimate_task_hours(node),
                "dependsOn": predecessor_map.get(key, []),
            }
            remaining.pop(key, None)
    critical_path = [item["nodeKey"] for item in scheduled.values() if item["dependsOn"]]
    return {
        "items": list(scheduled.values()),
        "criticalPath": critical_path,
        "summary": f"Generated {len(scheduled)} gantt items.",
    }


async def handle_split_task(
    context: SkillExecutionContext,
    data: SplitTaskInput,
) -> dict[str, Any]:
    if context.context.user_id is None or context.db is None:
        raise SkillRuntimeError(
            SkillErrorCode.UNAUTHORIZED,
            "Split task skill requires an authenticated user and database session",
        )
    request = data.model_copy(
        update={
            "workspace_id": data.workspace_id or context.context.workspace_id,
            "session_id": data.session_id or context.context.session_id,
            "conversation_id": data.conversation_id or context.context.conversation_id,
            "project_id": data.project_id or context.context.project_id,
            "table_ids": data.table_ids or context.context.table_ids,
            "view_id": data.view_id or context.context.view_id,
            "task_id": data.task_id or context.context.task_id,
            "team_id": data.team_id or context.context.team_id,
            "organization_id": data.organization_id or context.context.organization_id,
            "workflow_id": data.workflow_id or context.context.workflow_id,
            "agent_id": data.agent_id or context.context.agent_id,
            "locale": data.locale or context.context.locale,
            "timezone": data.timezone or context.context.timezone,
            "dry_run": bool(context.context.dry_run or data.dry_run),
        }
    )
    response = await task_split_service.run(
        db=context.db,
        user_id=int(context.context.user_id),
        request=request,
    )
    if response.error:
        raise SkillRuntimeError(
            SkillErrorCode.EXECUTION_FAILED,
            response.error.get("message") or "Task split failed",
            retryable=True,
            details=response.error,
        )
    return response.model_dump(mode="json", by_alias=True)


async def handle_generate_gantt(
    context: SkillExecutionContext,
    data: GenerateGanttInput,
) -> dict[str, Any]:
    return _compute_gantt_items(
        task_plan=data.task_plan,
        start_date_value=data.start_date,
        working_hours_per_day=data.working_hours_per_day,
    )


async def handle_create_task_records(
    context: SkillExecutionContext,
    data: CreateTaskRecordsInput,
) -> dict[str, Any]:
    plan = _normalize_task_plan_payload(data.task_plan)
    root = plan.get("root")

    # Support flat tasks array format from AI (e.g., {tasks: [{title, assignee, ...}, ...]})
    if not isinstance(root, dict) and isinstance(plan.get("tasks"), list):
        plan = _convert_flat_tasks_to_nodes(plan["tasks"])
        root = plan.get("root")

    if not isinstance(root, dict):
        raise SkillRuntimeError(
            SkillErrorCode.INVALID_INPUT,
            "taskPlan.root is required, or provide a flat 'tasks' array",
        )

    backend = (settings.DATA_BACKEND or "").lower()
    if backend in {"db", "database", "postgres", "sqlite"}:
        if context.db is None:
            raise SkillRuntimeError(
                SkillErrorCode.EXECUTION_FAILED,
                "Database session is not available",
            )
        store = await get_full_store(context.db, data.table_id)
    else:
        store = await get_full_store_for_table(data.table_id)

    fields = list(store.get("fields") or [])
    # Build a lookup for field metadata (name, type, options) keyed by field id
    field_index: dict[str, dict[str, Any]] = {
        str(f.get("id") or ""): f for f in fields if f.get("id")
    }

    # Primary keyword-based field mapping
    title_fid = data.title_field_id or _match_field_id(
        fields, keywords=["title", "name", "task", "任务", "标题"], preferred_types=["text"]
    )
    description_fid = data.description_field_id or _match_field_id(
        fields, keywords=["description", "detail", "desc", "说明", "描述", "备注"], preferred_types=["text"]
    )
    status_fid = data.status_field_id or _match_field_id(
        fields, keywords=["status", "state", "阶段", "状态"], preferred_types=["select", "text"]
    )
    start_date_fid = data.start_date_field_id or _match_field_id(
        fields, keywords=["start", "begin", "开始"], preferred_types=["date"]
    )
    end_date_fid = data.end_date_field_id or _match_field_id(
        fields, keywords=["end", "due", "finish", "结束", "截止"], preferred_types=["date"]
    )
    estimate_fid = data.estimate_field_id or _match_field_id(
        fields, keywords=["estimate", "hours", "工时", "story", "budget", "预算", "预估"], preferred_types=["number", "text"]
    )
    dependency_fid = data.dependency_field_id or _match_field_id(
        fields, keywords=["dependency", "depends", "依赖", "link", "链接", "关联"], preferred_types=["text", "url"]
    )
    priority_fid = _match_field_id(
        fields, keywords=["priority", "优先级", "重要", "优先"], preferred_types=["rating", "number", "select"]
    )
    owner_fid = _match_field_id(
        fields, keywords=["owner", "assignee", "负责人", "成员", "member", "处理人"], preferred_types=["member", "text"]
    )
    progress_fid = _match_field_id(
        fields, keywords=["progress", "进度", "percent", "完成", "percentage"], preferred_types=["progress", "number"]
    )

    # Track already-mapped field ids to avoid double-mapping
    mapped_ids: set[str] = {
        fid for fid in [title_fid, description_fid, status_fid, start_date_fid,
                        end_date_fid, estimate_fid, dependency_fid, priority_fid,
                        owner_fid, progress_fid] if fid
    }

    # Fallback: if no explicit description field, try to store descriptions in any remaining text/url field
    if not description_fid:
        description_fid = _find_field_by_type(fields, preferred_types=["text", "url"], exclude_ids=mapped_ids)
        if description_fid:
            mapped_ids.add(description_fid)

    # Fallback: if no estimate field and we have a number field like BUDGET, use it
    if not estimate_fid:
        estimate_fid = _find_field_by_type(fields, preferred_types=["number"], exclude_ids=mapped_ids)
        if estimate_fid:
            mapped_ids.add(estimate_fid)

    # Fallback: if no dependency field, try url or remaining text fields
    if not dependency_fid:
        dependency_fid = _find_field_by_type(fields, preferred_types=["url", "text"], exclude_ids=mapped_ids)
        if dependency_fid:
            mapped_ids.add(dependency_fid)

    # Determine the default status value based on field type
    default_status = "未开始"
    if status_fid and status_fid in field_index:
        status_field = field_index[status_fid]
        if str(status_field.get("type") or "").lower() == "select":
            default_status = _get_select_default_value(status_field)

    field_mapping = {
        "titleFieldId": title_fid,
        "descriptionFieldId": description_fid,
        "statusFieldId": status_fid,
        "startDateFieldId": start_date_fid,
        "endDateFieldId": end_date_fid,
        "estimateFieldId": estimate_fid,
        "dependencyFieldId": dependency_fid,
        "priorityFieldId": priority_fid,
        "ownerFieldId": owner_fid,
        "progressFieldId": progress_fid,
    }

    gantt_index: dict[str, dict[str, Any]] = {}
    if data.gantt_data and isinstance(data.gantt_data.get("items"), list):
        for item in data.gantt_data.get("items") or []:
            if isinstance(item, dict) and item.get("nodeKey"):
                gantt_index[str(item["nodeKey"])] = item

    nodes = _flatten_task_nodes(root)
    created_records: list[dict[str, Any]] = []
    # 如果用户已确认操作，即使 data.dry_run 为 True 也必须实际写入
    # AI Agent 可能在 arguments 中保留了 dryRun:true，确认后应忽略
    if context.context.confirmed:
        should_dry_run = False
    else:
        should_dry_run = bool(context.context.dry_run or data.dry_run)
    for node in nodes:
        node_key = str(node.get("key") or "")
        gantt_item = gantt_index.get(node_key, {})
        values: dict[str, Any] = {}
        if field_mapping["titleFieldId"]:
            values[field_mapping["titleFieldId"]] = node.get("title") or node.get("name")
        if field_mapping["descriptionFieldId"]:
            desc = node.get("description") or node.get("objective") or ""
            values[field_mapping["descriptionFieldId"]] = desc
        if field_mapping["statusFieldId"]:
            ai_status = node.get("status")
            if ai_status is not None and isinstance(ai_status, str):
                # Validate and normalize status value against select field options
                sfid = field_mapping["statusFieldId"]
                status_field_info = field_index.get(sfid, {})
                if str(status_field_info.get("type") or "").lower() == "select":
                    values[sfid] = _validate_select_value(ai_status, status_field_info, default_status)
                else:
                    values[sfid] = ai_status
            else:
                values[field_mapping["statusFieldId"]] = default_status
        # Handle start/end dates - first from gantt, then from node attributes, with auto-conversion
        # Use timezone from context for accurate date-to-timestamp conversion
        tz_name = context.context.timezone or "Asia/Shanghai"
        start_date_raw = gantt_item.get("startDate") or node.get("startDate")
        end_date_raw = gantt_item.get("endDate") or node.get("endDate")
        if field_mapping["startDateFieldId"] and start_date_raw is not None:
            start_date_ts = _convert_date_to_timestamp(start_date_raw, timezone_name=tz_name)
            if start_date_ts is not None:
                values[field_mapping["startDateFieldId"]] = start_date_ts
            else:
                values[field_mapping["startDateFieldId"]] = start_date_raw
        if field_mapping["endDateFieldId"] and end_date_raw is not None:
            end_date_ts = _convert_date_to_timestamp(end_date_raw, timezone_name=tz_name)
            if end_date_ts is not None:
                values[field_mapping["endDateFieldId"]] = end_date_ts
            else:
                values[field_mapping["endDateFieldId"]] = end_date_raw
        if field_mapping["estimateFieldId"]:
            values[field_mapping["estimateFieldId"]] = _estimate_task_hours(node)
        if field_mapping["dependencyFieldId"]:
            depends = node.get("dependsOn") or node.get("dependencies") or []
            if isinstance(depends, list):
                values[field_mapping["dependencyFieldId"]] = ", ".join(depends)
            else:
                values[field_mapping["dependencyFieldId"]] = str(depends)
        # Map priority (int) to rating/number/select field
        if field_mapping["priorityFieldId"]:
            priority_val = node.get("priority")
            if priority_val is not None:
                pfid = field_mapping["priorityFieldId"]
                pf_type = str(field_index.get(pfid, {}).get("type") or "").lower()
                if pf_type == "rating":
                    # For rating fields, ensure value is within range (typically 1-5)
                    values[pfid] = max(1, min(5, int(priority_val)))
                elif pf_type == "select":
                    values[pfid] = str(priority_val)
                else:
                    values[pfid] = int(priority_val) if isinstance(priority_val, (int, float)) else priority_val
        # Map owner / assignee suggestions, with member field normalization
        if field_mapping["ownerFieldId"]:
            owner_val = node.get("owner") or node.get("assignee")
            if owner_val is not None:
                owner_fid = field_mapping["ownerFieldId"]
                owner_field_info = field_index.get(owner_fid, {})
                values[owner_fid] = _normalize_member_value(owner_field_info, owner_val)
        # Initialize progress to 0
        if field_mapping["progressFieldId"]:
            progress_val = node.get("progress")
            if progress_val is not None:
                values[field_mapping["progressFieldId"]] = progress_val
            else:
                values[field_mapping["progressFieldId"]] = 0

        if should_dry_run:
            created_records.append(
                {
                    "nodeKey": node_key,
                    "recordId": None,
                    "values": values,
                }
            )
            continue

        if backend in {"db", "database", "postgres", "sqlite"}:
            if context.context.user_id is None:
                raise SkillRuntimeError(
                    SkillErrorCode.UNAUTHORIZED,
                    "Unauthorized",
                )
            record = await create_record(
                context.db,
                data.table_id,
                created_by_user_id=int(context.context.user_id),
            )
            record_id = str(record.get("id"))
            updated = await update_record_fields(
                context.db,
                data.table_id,
                record_id,
                values,
                user_id=int(context.context.user_id),
            )
            created_records.append(
                {
                    "nodeKey": node_key,
                    "recordId": record_id,
                    "values": updated or values,
                }
            )
        else:
            record = await create_record_file(data.table_id)
            record_id = str(record.get("id"))
            for field_id, value in values.items():
                await update_record_file(data.table_id, record_id, field_id, value)
            created_records.append(
                {
                    "nodeKey": node_key,
                    "recordId": record_id,
                    "values": values,
                }
            )

    return {
        "tableId": data.table_id,
        "totalCount": len(created_records),
        "dryRun": should_dry_run,
        "mappedFieldIds": field_mapping,
        "createdRecords": created_records,
    }


def build_default_registry() -> SkillRegistry:
    from app.skills.estimate_workload import build_estimate_workload_skill_definition
    from app.skills.task_management.registry import build_task_management_registry

    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            metadata=SkillMetadata(
                name="qtable.table.describe",
                title="Describe QTable Table",
                description="Read table schema and sample rows for AI planning and tool calling.",
                tags=["table", "read", "metadata"],
                side_effect=SkillSideEffect.NONE,
                confirmation_required=False,
                idempotent=True,
                supports_dry_run=True,
                permissions=[
                    SkillPermissionRequirement(
                        resource="table",
                        action="read",
                        target_param="table_id",
                    )
                ],
            ),
            input_model=DescribeTableInput,
            output_model=DescribeTableOutput,
            handler=handle_describe_table,
        )
    )

    # 注册 Task Management 工具（v2.0 新增）
    # Layer 1: 环境感知 - get_current_datetime, get_current_user, get_current_workspace
    # Layer 2: Schema 感知 - describe_table_schema
    # Layer 3: 领域智能 - get_overdue_tasks, calculate_project_progress, get_member_workload,
    #           detect_blocking_tasks, predict_project_delay
    # Layer 4: 工作流 - create_execution_plan
    try:
        tm_registry = build_task_management_registry()
        for manifest in tm_registry.list_manifest():
            definition = tm_registry.get(manifest.metadata.name)
            if definition is not None:
                registry.register(definition)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to register Task Management tools, skipping.",
            exc_info=True,
        )
    registry.register(
        SkillDefinition(
            metadata=SkillMetadata(
                name="qtable.record.create",
                title="Create QTable Record",
                description="Create a new record in a QTable table with confirmation support.",
                tags=["record", "write", "mutation"],
                side_effect=SkillSideEffect.WRITE,
                confirmation_required=True,
                idempotent=False,
                supports_dry_run=True,
                permissions=[
                    SkillPermissionRequirement(
                        resource="table",
                        action="write",
                        target_param="table_id",
                    )
                ],
            ),
            input_model=CreateRecordInput,
            output_model=CreateRecordOutput,
            handler=handle_create_record,
        )
    )
    registry.register(
        SkillDefinition(
            metadata=SkillMetadata(
                name="qtable.task.split",
                title="Split Complex Task",
                description="Split a complex project goal into a task tree with dependencies, estimates, risks, Mermaid, and persisted task records. Invoke when planning delivery or decomposing large work.",
                tags=["task", "planning", "project", "agent", "workflow"],
                side_effect=SkillSideEffect.WRITE,
                confirmation_required=True,
                idempotent=False,
                supports_dry_run=True,
                permissions=[],
            ),
            input_model=SplitTaskInput,
            output_model=TaskSplitResponse,
            handler=handle_split_task,
        )
    )
    registry.register(
        SkillDefinition(
            metadata=SkillMetadata(
                name="qtable.gantt.generate",
                title="Generate Gantt Data",
                description="Generate gantt-ready scheduling data from task split output and dependency graph.",
                tags=["gantt", "timeline", "planning", "read"],
                side_effect=SkillSideEffect.NONE,
                confirmation_required=False,
                idempotent=True,
                supports_dry_run=True,
                permissions=[],
            ),
            input_model=GenerateGanttInput,
            output_model=GenerateGanttOutput,
            handler=handle_generate_gantt,
        )
    )
    registry.register(
        SkillDefinition(
            metadata=SkillMetadata(
                name="qtable.task.records.create",
                title="Create Task Records From Plan",
                description=(
                    "将任务计划（来自 qtable.task.split 或手动构建）批量写入 QTable 任务表。\n"
                    "重要约束：\n"
                    "1. 调用前必须先通过 qtable.schema.describe 获取目标表的完整 Schema，了解字段名称、类型、枚举值。\n"
                    "2. taskPlan 中的任务字段（如 status, owner, priority, startDate, endDate 等）必须与表的字段格式匹配：\n"
                    "   - STATUS 必须使用表的枚举选项值（如'未开始/进行中/已完成/已延迟'），而非英文值\n"
                    "   - OWNER 必须使用表的成员 ID（如'u1'），而非成员名称\n"
                    "   - PRIORITY 必须符合 rating 字段的值范围（如 1-5）\n"
                    "   - 日期字段会自动转换为时间戳，支持 ISO 格式（如'2026-05-25'）\n"
                    "3. 本 Skill 会自动读取表结构并按关键字匹配字段（如 title→标题、status→状态、owner→负责人）。\n"
                    "4. 若 taskPlan 中缺少某字段数据，对应列将使用默认值（status 默认'未开始'，progress 默认 0）。"
                ),
                tags=["task", "record", "write", "workflow"],
                side_effect=SkillSideEffect.WRITE,
                confirmation_required=True,
                idempotent=False,
                supports_dry_run=True,
                permissions=[
                    SkillPermissionRequirement(
                        resource="table",
                        action="write",
                        target_param="table_id",
                    )
                ],
            ),
            input_model=CreateTaskRecordsInput,
            output_model=CreateTaskRecordsOutput,
            handler=handle_create_task_records,
        )
    )
    registry.register(build_estimate_workload_skill_definition())
    return registry
