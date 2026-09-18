from __future__ import annotations

import copy
import asyncio
import json
import math
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.core.config import settings
from app.models.change_history import ChangeSet
from app.models.estimate_workload import WorkloadEstimateRun
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.workload_planning import WorkloadPlanningBatch
from app.schemas.estimate_workload import (
    EstimateFeedbackRequest,
    EstimateWorkloadRequest,
    TeamCapabilityProfile,
)
from app.schemas.workload_planning import (
    WorkloadPlanningApplyRequest,
    WorkloadPlanningFeedbackRequest,
    WorkloadPlanningPreviewRequest,
    WorkloadPlanningWhatIfRequest,
)
from app.services.change_history import (
    append_change_set,
    changed_fields,
    record_meta,
    record_version,
)
from app.services.estimate_workload import estimate_workload_service
from app.services.row_permissions import filter_store_for_user, require_record_access
from app.services.smart_table_store import get_full_store
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)


MAX_PROJECT_TASKS = 50
WORKDAY_HOURS = 8.0
UTILIZATION = 0.85

FIELD_SPECS: dict[str, dict[str, Any]] = {
    "storyPoints": {
        "names": ["story point", "story points", "故事点", "估算点数"],
        "types": {"number"},
        "name": "Story Point",
        "type": "number",
        "property": {"precision": 1},
    },
    "p50Hours": {
        "names": ["p50工时", "p50", "预计工作量", "预计工时", "工作量"],
        "types": {"number"},
        "name": "P50 工时",
        "type": "number",
        "property": {"suffix": "h", "precision": 1},
    },
    "p90Hours": {
        "names": ["p90工时", "p90", "保守工时", "风险工时"],
        "types": {"number"},
        "name": "P90 工时",
        "type": "number",
        "property": {"suffix": "h", "precision": 1},
    },
    "confidence": {
        "names": ["估算置信度", "置信度", "confidence"],
        "types": {"number"},
        "name": "估算置信度",
        "type": "number",
        "property": {"precision": 2},
    },
    "estimateTrace": {
        "names": ["估算trace", "估算追踪", "workload trace", "estimate trace"],
        "types": {"text"},
        "name": "估算 Trace",
        "type": "text",
    },
}

TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"]
DESCRIPTION_NAMES = ["任务描述", "描述", "说明", "description", "details"]
DEPENDENCY_NAMES = ["前置依赖", "依赖", "依赖任务", "dependencies", "depends on"]


class WorkloadPlanningError(ValueError):
    pass


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _matches_name(name: str, candidates: Iterable[str]) -> bool:
    normalized = _normalize_text(name)
    return any(
        normalized == _normalize_text(candidate)
        or _normalize_text(candidate) in normalized
        for candidate in candidates
    )


def _field_to_dict(field: TableField) -> dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": _clone(field.options),
        "property": _clone(field.property),
        "orderIndex": field.order_index,
    }


def _find_title_field(fields: list[TableField]) -> Optional[TableField]:
    match = next(
        (
            field
            for field in fields
            if field.type == "text" and _matches_name(field.name, TITLE_NAMES)
        ),
        None,
    )
    if match:
        return match
    return next((field for field in fields if field.type == "text"), None)


def _find_description_field(fields: list[TableField]) -> Optional[TableField]:
    return next(
        (
            field
            for field in fields
            if field.type == "text"
            and _matches_name(field.name, DESCRIPTION_NAMES)
        ),
        None,
    )


def _find_dependency_field(
    fields: list[TableField],
    table_id: str,
) -> Optional[TableField]:
    for field in fields:
        if field.type != "relation" or not _matches_name(field.name, DEPENDENCY_NAMES):
            continue
        prop = field.property or {}
        if str(prop.get("targetTableId") or "") != table_id:
            continue
        if not bool(prop.get("multiple", True)):
            continue
        return field
    return None


def _new_field_id(semantic: str) -> str:
    safe = "".join(ch for ch in semantic.lower() if ch.isalnum())
    return f"f_wp_{safe}_{uuid.uuid4().hex[:8]}"


def _build_writeback_mapping(
    fields: list[TableField],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    mapping: dict[str, str] = {}
    additions: list[dict[str, Any]] = []
    used_existing: set[str] = set()

    for semantic, spec in FIELD_SPECS.items():
        match = next(
            (
                field
                for field in fields
                if field.id not in used_existing
                and field.type in spec["types"]
                and _matches_name(field.name, spec["names"])
            ),
            None,
        )
        if match:
            mapping[semantic] = match.id
            used_existing.add(match.id)
            continue
        field_id = _new_field_id(semantic)
        mapping[semantic] = field_id
        additions.append(
            {
                "semantic": semantic,
                "id": field_id,
                "name": spec["name"],
                "type": spec["type"],
                "options": None,
                "property": _clone(spec.get("property")),
            }
        )
    return mapping, additions


def _relation_ids(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _business_days_until(deadline: Optional[str]) -> Optional[int]:
    if not deadline:
        return None
    try:
        target = date.fromisoformat(deadline)
    except ValueError as exc:
        raise WorkloadPlanningError("Deadline must use YYYY-MM-DD") from exc
    today = datetime.now(timezone.utc).date()
    if target < today:
        return 0
    days = 0
    cursor = today
    while cursor <= target:
        if cursor.weekday() < 5:
            days += 1
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return days


def _topological_order(
    task_ids: list[str],
    dependencies: Mapping[str, list[str]],
) -> list[str]:
    task_set = set(task_ids)
    indegree = {task_id: 0 for task_id in task_ids}
    outgoing = {task_id: [] for task_id in task_ids}
    for task_id in task_ids:
        for predecessor in dependencies.get(task_id, []):
            if predecessor not in task_set or predecessor == task_id:
                continue
            indegree[task_id] += 1
            outgoing[predecessor].append(task_id)

    queue = sorted([task_id for task_id, count in indegree.items() if count == 0])
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for successor in sorted(outgoing[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
                queue.sort()
    if len(order) != len(task_ids):
        raise WorkloadPlanningError("Task dependency cycle detected; calendar duration cannot be estimated safely")
    return order


def _critical_path(
    task_ids: list[str],
    dependencies: Mapping[str, list[str]],
    durations: Mapping[str, float],
) -> tuple[float, list[str]]:
    order = _topological_order(task_ids, dependencies)
    task_set = set(task_ids)
    longest: dict[str, float] = {}
    previous: dict[str, Optional[str]] = {}

    for task_id in order:
        predecessors = [
            item
            for item in dependencies.get(task_id, [])
            if item in task_set and item != task_id
        ]
        if not predecessors:
            longest[task_id] = float(durations.get(task_id, 0.0))
            previous[task_id] = None
            continue
        best = max(predecessors, key=lambda item: longest.get(item, 0.0))
        longest[task_id] = longest.get(best, 0.0) + float(durations.get(task_id, 0.0))
        previous[task_id] = best

    if not longest:
        return 0.0, []
    end = max(longest, key=longest.get)
    path: list[str] = []
    cursor: Optional[str] = end
    while cursor:
        path.append(cursor)
        cursor = previous.get(cursor)
    path.reverse()
    return round(longest[end], 2), path


def _aggregate_project(
    tasks: list[dict[str, Any]],
    *,
    team_size: int,
    parallel_streams: int,
    deadline: Optional[str],
) -> dict[str, Any]:
    if not tasks:
        raise WorkloadPlanningError("No tasks available for workload planning")

    task_ids = [str(item["recordId"]) for item in tasks]
    task_set = set(task_ids)
    dependencies: dict[str, list[str]] = {}
    external_edges: list[dict[str, str]] = []
    for task in tasks:
        record_id = str(task["recordId"])
        deps: list[str] = []
        for predecessor in task.get("dependencyRecordIds") or []:
            predecessor = str(predecessor)
            if predecessor in task_set:
                deps.append(predecessor)
            else:
                external_edges.append(
                    {"recordId": record_id, "dependencyRecordId": predecessor}
                )
        dependencies[record_id] = list(dict.fromkeys(deps))

    p50_map = {str(item["recordId"]): float(item["p50Hours"]) for item in tasks}
    p90_map = {str(item["recordId"]): float(item["p90Hours"]) for item in tasks}
    critical_p50, critical_path = _critical_path(task_ids, dependencies, p50_map)
    critical_p90, _ = _critical_path(task_ids, dependencies, p90_map)

    total_p50 = round(sum(p50_map.values()), 2)
    total_p90 = round(sum(p90_map.values()), 2)
    story_points = round(sum(float(item.get("storyPoints") or 0.0) for item in tasks), 2)

    effective_parallelism = max(
        1,
        min(int(team_size), int(parallel_streams), len(tasks)),
    )
    capacity = max(effective_parallelism * UTILIZATION, 1.0)
    calendar_p50_hours = round(max(critical_p50, total_p50 / capacity), 2)
    calendar_p90_hours = round(max(critical_p90, total_p90 / capacity), 2)
    calendar_p50_days = round(calendar_p50_hours / WORKDAY_HOURS, 2)
    calendar_p90_days = round(calendar_p90_hours / WORKDAY_HOURS, 2)

    confidence_scores = [float(item.get("confidenceScore") or 0.0) for item in tasks]
    confidence = round(sum(confidence_scores) / max(len(confidence_scores), 1), 2)

    uncertainty = sorted(
        [
            {
                "recordId": str(item["recordId"]),
                "title": item.get("title") or str(item["recordId"]),
                "spreadHours": round(float(item["p90Hours"]) - float(item["p50Hours"]), 2),
                "spreadRatio": round(
                    (float(item["p90Hours"]) - float(item["p50Hours"]))
                    / max(float(item["p50Hours"]), 0.1),
                    3,
                ),
                "confidenceScore": float(item.get("confidenceScore") or 0.0),
            }
            for item in tasks
        ],
        key=lambda item: (-item["spreadRatio"], item["confidenceScore"], item["title"]),
    )

    available_days = _business_days_until(deadline)
    deadline_risk = None
    if available_days is not None:
        if available_days <= 0:
            level = "critical"
        elif calendar_p50_days > available_days:
            level = "critical"
        elif calendar_p90_days > available_days:
            level = "high"
        elif calendar_p90_days > available_days * 0.85:
            level = "medium"
        else:
            level = "low"
        deadline_risk = {
            "deadline": deadline,
            "availableBusinessDays": available_days,
            "level": level,
            "p50SlackDays": round(available_days - calendar_p50_days, 2),
            "p90SlackDays": round(available_days - calendar_p90_days, 2),
        }

    return {
        "taskCount": len(tasks),
        "totalStoryPoints": story_points,
        "totalWorkP50Hours": total_p50,
        "totalWorkP90Hours": total_p90,
        "criticalPathP50Hours": critical_p50,
        "criticalPathP90Hours": critical_p90,
        "criticalPathRecordIds": critical_path,
        "effectiveParallelStreams": effective_parallelism,
        "calendarP50Hours": calendar_p50_hours,
        "calendarP90Hours": calendar_p90_hours,
        "calendarP50Days": calendar_p50_days,
        "calendarP90Days": calendar_p90_days,
        "confidenceScore": confidence,
        "topUncertainty": uncertainty[:8],
        "externalDependencies": external_edges[:20],
        "deadlineRisk": deadline_risk,
        "calculation": (
            "calendar = max(critical_path, total_work / "
            f"(effective_parallel_streams × {UTILIZATION}))"
        ),
    }


def _task_prompt(
    *,
    title: str,
    description: str,
    deadline: Optional[str],
) -> str:
    parts = [
        f"请估算以下可执行任务的工作量与工期。任务标题：{title}",
    ]
    if description:
        parts.append(f"任务说明：{description}")
    if deadline:
        parts.append(f"项目目标截止时间：{deadline}")
    parts.append(
        "请把结果聚焦在这一个任务本身，输出 Story Point、P50/P90 工时、confidence、依据、影响因素与风险；不要把整个项目其它任务重复计入。"
    )
    return "\n".join(parts)


class WorkloadPlanningService:
    async def _require_table(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        table_id: str,
        permission: str,
    ) -> str:
        result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == table_id,
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "table",
            )
        )
        if result.scalars().first() is None:
            raise WorkloadPlanningError("Target task table not found or no access")
        effective = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(effective, permission):
            raise PermissionError("Target task table not found or no access")
        return effective

    async def _visible_store(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        table_permission: str,
    ) -> dict[str, Any]:
        store = await get_full_store(db, table_id)
        visible, _ = await filter_store_for_user(
            db,
            table_id,
            store,
            user_id=user_id,
            table_permission=table_permission,
        )
        return visible

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: WorkloadPlanningPreviewRequest,
    ) -> dict[str, Any]:
        permission = await self._require_table(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            table_id=request.table_id,
            permission="read",
        )
        store = await self._visible_store(
            db,
            user_id=user_id,
            table_id=request.table_id,
            table_permission=permission,
        )

        fields_result = await db.execute(
            select(TableField)
            .where(TableField.table_id == request.table_id)
            .order_by(TableField.order_index)
        )
        fields = list(fields_result.scalars().all())
        title_field = _find_title_field(fields)
        if title_field is None:
            raise WorkloadPlanningError("Task table requires a readable title/text field")
        description_field = _find_description_field(fields)
        dependency_field = _find_dependency_field(fields, request.table_id)

        visible_records = {
            str(record.get("id")): record
            for record in store.get("records", [])
            if record.get("id") is not None
        }
        selected_ids = request.record_ids or list(visible_records.keys())
        if not selected_ids:
            raise WorkloadPlanningError("No visible task records to estimate")
        if len(selected_ids) > MAX_PROJECT_TASKS:
            raise WorkloadPlanningError(
                f"At most {MAX_PROJECT_TASKS} tasks can be estimated in one batch; select a smaller scope"
            )
        missing = [record_id for record_id in selected_ids if record_id not in visible_records]
        if missing:
            raise PermissionError("Selected task records not found or no access")

        version_result = await db.execute(
            select(TableRecord.id, TableRecord.version).where(
                TableRecord.table_id == request.table_id,
                TableRecord.id.in_(selected_ids),
            )
        )
        record_versions = {
            str(record_id): int(version or 1)
            for record_id, version in version_result.all()
        }
        if len(record_versions) != len(selected_ids):
            raise WorkloadPlanningError("One or more selected tasks no longer exist")

        mapping, additions = _build_writeback_mapping(fields)
        task_results: list[dict[str, Any]] = []
        estimate_ids: list[str] = []
        try:
            async def estimate_task(
                record_id: str,
                estimate_db: AsyncSession,
            ) -> tuple[str, str, Any]:
                record = visible_records[record_id]
                title = str(record.get(title_field.id) or record_id).strip()
                description = (
                    str(record.get(description_field.id) or "").strip()
                    if description_field
                    else ""
                )
                estimate_request = EstimateWorkloadRequest(
                    prompt=_task_prompt(
                        title=title,
                        description=description,
                        deadline=request.deadline,
                    ),
                    persistResult=True,
                    dryRun=False,
                    workspaceId=request.workspace_id,
                    projectId=None,
                    # The estimate Agent builds an access-checked table
                    # context and may call its table-context tool. Passing an
                    # empty scope made that tool infer a target without the
                    # caller's verified table context, causing false "No
                    # access" errors for otherwise authorized users.
                    tableIds=[request.table_id],
                    taskId=record_id,
                    businessDomain=request.business_domain,
                    qualityBar=request.quality_bar,
                    teamProfile=TeamCapabilityProfile(
                        teamSize=request.team_size,
                        parallelStreams=request.parallel_streams or request.team_size,
                    ),
                )
                response = await estimate_workload_service.run(
                    db=estimate_db,
                    user_id=user_id,
                    request=estimate_request,
                )
                if response.error or not response.estimate_id:
                    raise WorkloadPlanningError(
                        str((response.error or {}).get("message") or f"Estimate failed for task {title}")
                    )
                return record_id, title, response

            # Each model estimate opens its own database session. SQLAlchemy
            # sessions are not safe to share across concurrent tasks, so the
            # request session remains dedicated to the final batch write.
            # SQLite test/local sessions retain deterministic sequential work.
            bind = db.get_bind()
            can_parallelize = bind.dialect.name == "postgresql"
            concurrency = min(
                int(request.parallel_streams or 1),
                settings.AI_WORKLOAD_MAX_CONCURRENCY,
            )
            if can_parallelize and concurrency > 1:
                semaphore = asyncio.Semaphore(concurrency)

                async def estimate_with_own_session(record_id: str):
                    async with semaphore:
                        async with AsyncSessionLocal() as estimate_db:
                            return await estimate_task(record_id, estimate_db)

                estimates = await asyncio.gather(
                    *(estimate_with_own_session(record_id) for record_id in selected_ids)
                )
            else:
                estimates = [
                    await estimate_task(record_id, db)
                    for record_id in selected_ids
                ]

            for record_id, title, response in estimates:
                record = visible_records[record_id]
                estimate_ids.append(response.estimate_id)
                estimate_row = await db.get(WorkloadEstimateRun, response.estimate_id)
                if estimate_row:
                    estimate_row.status = "previewed"
                result = response.result
                task_results.append(
                    {
                        "recordId": record_id,
                        "recordVersion": record_versions[record_id],
                        "title": title,
                        "storyPoints": round(result.adjusted_story_points, 2),
                        "p50Hours": round(result.p50_hours, 2),
                        "p90Hours": round(result.p90_hours, 2),
                        "confidenceScore": round(result.confidence.score, 2),
                        "confidenceLevel": result.confidence.level,
                        "confidenceRationale": result.confidence.rationale,
                        "summary": result.summary,
                        "basis": {
                            "context": result.context_insight.model_dump(mode="json", by_alias=True),
                            "formula": result.formula,
                            "assumptions": result.assumptions,
                            "historicalLearning": result.historical_learning.model_dump(
                                mode="json",
                                by_alias=True,
                            ),
                        },
                        "risks": [
                            item.model_dump(mode="json", by_alias=True)
                            for item in result.top_risks
                        ],
                        "dependencyRecordIds": (
                            _relation_ids(record.get(dependency_field.id))
                            if dependency_field
                            else []
                        ),
                        "estimateId": response.estimate_id,
                        "traceId": response.trace_id,
                    }
                )

            aggregate = _aggregate_project(
                task_results,
                team_size=request.team_size,
                parallel_streams=request.parallel_streams or request.team_size,
                deadline=request.deadline,
            )
            batch_id = f"wpb_{uuid.uuid4().hex}"
            batch = WorkloadPlanningBatch(
                id=batch_id,
                user_id=user_id,
                workspace_id=request.workspace_id,
                table_id=request.table_id,
                record_ids=list(selected_ids),
                request_payload=request.model_dump(mode="json", by_alias=True),
                result_payload={
                    "tasks": _clone(task_results),
                    "aggregate": _clone(aggregate),
                    "appliedRecordIds": [],
                    "changeSetIds": [],
                },
                record_versions={
                    item["recordId"]: item["recordVersion"] for item in task_results
                },
                field_mapping=_clone(mapping),
                schema_additions=_clone(additions),
                status="previewed",
            )
            db.add(batch)
            await db.commit()
            return {
                "batchId": batch_id,
                "tasks": task_results,
                "aggregate": aggregate,
                "fieldMapping": mapping,
                "schemaAdditions": additions,
                "recordIds": list(selected_ids),
            }
        except Exception:
            await db.rollback()
            if estimate_ids:
                result = await db.execute(
                    select(WorkloadEstimateRun).where(
                        WorkloadEstimateRun.id.in_(estimate_ids),
                        WorkloadEstimateRun.created_by == str(user_id),
                    )
                )
                for row in result.scalars().all():
                    row.status = "cancelled_preview"
                await db.commit()
            raise

    async def get_batch(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        batch_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(WorkloadPlanningBatch).where(
                WorkloadPlanningBatch.id == batch_id,
                WorkloadPlanningBatch.user_id == user_id,
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "batchId": row.id,
            "workspaceId": row.workspace_id,
            "tableId": row.table_id,
            "recordIds": row.record_ids,
            "request": row.request_payload,
            "result": row.result_payload,
            "fieldMapping": row.field_mapping,
            "schemaAdditions": row.schema_additions,
            "status": row.status,
            "changeSetId": row.change_set_id,
            "applyError": row.apply_error,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        }

    async def what_if(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: WorkloadPlanningWhatIfRequest,
    ) -> dict[str, Any]:
        batch = await self.get_batch(db, user_id=user_id, batch_id=request.batch_id)
        if batch is None:
            raise WorkloadPlanningError("Workload planning batch not found or no access")
        stored_request = batch["request"]
        base_result = batch["result"]
        tasks = list(base_result.get("tasks") or [])
        base_aggregate = dict(base_result.get("aggregate") or {})
        team_size = request.team_size or int(stored_request.get("teamSize") or 1)
        streams = request.parallel_streams or int(
            stored_request.get("parallelStreams") or team_size
        )
        streams = max(1, min(streams, team_size))
        deadline = request.deadline if request.deadline is not None else stored_request.get("deadline")
        scenario = _aggregate_project(
            tasks,
            team_size=team_size,
            parallel_streams=streams,
            deadline=deadline,
        )
        return {
            "batchId": request.batch_id,
            "base": base_aggregate,
            "scenario": scenario,
            "delta": {
                "teamSize": team_size - int(stored_request.get("teamSize") or 1),
                "calendarP50Days": round(
                    scenario["calendarP50Days"] - float(base_aggregate.get("calendarP50Days") or 0.0),
                    2,
                ),
                "calendarP90Days": round(
                    scenario["calendarP90Days"] - float(base_aggregate.get("calendarP90Days") or 0.0),
                    2,
                ),
            },
            "riskRecordIds": list(
                dict.fromkeys(
                    (scenario.get("criticalPathRecordIds") or [])
                    + [
                        item["recordId"]
                        for item in scenario.get("topUncertainty") or []
                    ][:5]
                )
            ),
        }

    async def _load_batch_for_apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        batch_id: str,
    ) -> WorkloadPlanningBatch:
        result = await db.execute(
            select(WorkloadPlanningBatch)
            .where(
                WorkloadPlanningBatch.id == batch_id,
                WorkloadPlanningBatch.user_id == user_id,
            )
            .with_for_update()
        )
        row = result.scalars().first()
        if row is None:
            raise WorkloadPlanningError("Workload planning batch not found or no access")
        return row

    async def apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: WorkloadPlanningApplyRequest,
    ) -> dict[str, Any]:
        table_permission = await self._require_table(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            table_id=request.table_id,
            permission="edit",
        )
        batch = await self._load_batch_for_apply(
            db,
            user_id=user_id,
            batch_id=request.batch_id,
        )
        if batch.workspace_id != request.workspace_id or batch.table_id != request.table_id:
            raise WorkloadPlanningError("Workload planning batch scope mismatch")

        payload = dict(batch.result_payload or {})
        tasks = {
            str(item["recordId"]): item
            for item in payload.get("tasks") or []
        }
        all_ids = list(batch.record_ids or [])
        requested_ids = request.record_ids or all_ids
        if any(record_id not in tasks for record_id in requested_ids):
            raise WorkloadPlanningError("Apply selection contains records outside the preview batch")
        applied_ids = set(payload.get("appliedRecordIds") or [])
        pending_ids = [record_id for record_id in requested_ids if record_id not in applied_ids]
        if not pending_ids:
            return {
                "batchId": batch.id,
                "status": batch.status,
                "idempotent": True,
                "appliedRecordIds": sorted(applied_ids),
                "changeSetIds": payload.get("changeSetIds") or [],
                "fieldMapping": batch.field_mapping,
            }

        try:
            # Re-check row visibility at apply time. A record that was visible
            # during preview may have become hidden by a row-permission policy
            # change; AI writeback must never bypass the current permission scope.
            for record_id in pending_ids:
                await require_record_access(
                    db,
                    request.table_id,
                    record_id,
                    user_id=user_id,
                    table_permission=table_permission,
                )

            records_result = await db.execute(
                select(TableRecord)
                .where(
                    TableRecord.table_id == request.table_id,
                    TableRecord.id.in_(pending_ids),
                )
                .with_for_update()
            )
            records = {row.id: row for row in records_result.scalars().all()}
            if len(records) != len(pending_ids):
                raise WorkloadPlanningError("One or more previewed tasks no longer exist")

            for record_id in pending_ids:
                expected_version = int((batch.record_versions or {}).get(record_id) or 1)
                current_version = record_version(records[record_id])
                if current_version != expected_version:
                    raise WorkloadPlanningError(
                        f"Task {record_id} changed after preview; regenerate the estimate before applying"
                    )

            fields_result = await db.execute(
                select(TableField)
                .where(TableField.table_id == request.table_id)
                .order_by(TableField.order_index)
                .with_for_update()
            )
            fields = list(fields_result.scalars().all())
            fields_by_id = {field.id: field for field in fields}
            max_order = max([field.order_index or 0 for field in fields], default=-1)

            for index, addition in enumerate(batch.schema_additions or []):
                existing = fields_by_id.get(addition["id"])
                if existing:
                    if existing.type != addition["type"]:
                        raise WorkloadPlanningError(
                            f"Writeback field {addition['id']} changed after preview"
                        )
                    continue
                field = TableField(
                    id=addition["id"],
                    table_id=request.table_id,
                    name=addition["name"],
                    type=addition["type"],
                    options=_clone(addition.get("options")),
                    property=_clone(addition.get("property")),
                    order_index=max_order + index + 1,
                )
                db.add(field)
                fields_by_id[field.id] = field
            await db.flush()

            history_items: list[dict[str, Any]] = []
            mapping = dict(batch.field_mapping or {})
            for record_id in pending_ids:
                task = tasks[record_id]
                record = records[record_id]
                before_data = dict(record.data or {})
                before_meta = record_meta(record)
                before_version = record_version(record)
                after_data = dict(before_data)
                values = {
                    mapping["storyPoints"]: task["storyPoints"],
                    mapping["p50Hours"]: task["p50Hours"],
                    mapping["p90Hours"]: task["p90Hours"],
                    mapping["confidence"]: task["confidenceScore"],
                    mapping["estimateTrace"]: json.dumps(
                        {
                            "batchId": batch.id,
                            "estimateId": task["estimateId"],
                            "traceId": task["traceId"],
                            "recordId": record_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
                after_data.update(values)
                changed = changed_fields(before_data, after_data)
                if not changed:
                    continue
                record.data = after_data
                record.version = before_version + 1
                history_items.append(
                    {
                        "table_id": request.table_id,
                        "entity_type": "record",
                        "entity_id": record_id,
                        "before_data": before_data,
                        "after_data": after_data,
                        "before_meta": before_meta,
                        "after_meta": record_meta(record),
                        "version_before": before_version,
                        "version_after": record.version,
                        "changed_fields": changed,
                    }
                )

            change_set = await append_change_set(
                db,
                table_id=request.table_id,
                actor_id=user_id,
                actor_type="ai",
                operation="workload_estimate_apply",
                source="workload_planning",
                trace_id=batch.id,
                summary=f"Apply workload estimates to {len(pending_ids)} task(s)",
                items=history_items,
            )

            estimate_ids = [
                tasks[record_id]["estimateId"]
                for record_id in pending_ids
                if tasks[record_id].get("estimateId")
            ]
            if estimate_ids:
                estimate_result = await db.execute(
                    select(WorkloadEstimateRun).where(
                        WorkloadEstimateRun.id.in_(estimate_ids),
                        WorkloadEstimateRun.created_by == str(user_id),
                    )
                )
                for estimate_row in estimate_result.scalars().all():
                    estimate_row.status = "applied"

            applied_ids.update(pending_ids)
            change_set_ids = list(payload.get("changeSetIds") or [])
            if change_set.id not in change_set_ids:
                change_set_ids.append(change_set.id)
            payload["appliedRecordIds"] = sorted(applied_ids)
            payload["changeSetIds"] = change_set_ids
            batch.result_payload = payload
            batch.change_set_id = change_set.id
            batch.status = "applied" if applied_ids == set(all_ids) else "partially_applied"
            batch.apply_error = None
            batch.applied_at = datetime.now(timezone.utc)
            await db.commit()
            return {
                "batchId": batch.id,
                "status": batch.status,
                "idempotent": False,
                "appliedRecordIds": sorted(applied_ids),
                "changeSetIds": change_set_ids,
                "fieldMapping": mapping,
            }
        except Exception as exc:
            await db.rollback()
            retry_result = await db.execute(
                select(WorkloadPlanningBatch).where(
                    WorkloadPlanningBatch.id == request.batch_id,
                    WorkloadPlanningBatch.user_id == user_id,
                )
            )
            retry = retry_result.scalars().first()
            if retry:
                already_applied = set(
                    (retry.result_payload or {}).get("appliedRecordIds") or []
                )
                retry.status = (
                    "partially_applied" if already_applied else "failed"
                )
                retry.apply_error = str(exc)[:2000]
                await db.commit()
            raise

    async def submit_feedback(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: WorkloadPlanningFeedbackRequest,
    ) -> dict[str, Any]:
        result = await db.execute(
            select(WorkloadPlanningBatch).where(
                WorkloadPlanningBatch.id == request.batch_id,
                WorkloadPlanningBatch.user_id == user_id,
            )
        )
        batch = result.scalars().first()
        if batch is None:
            raise WorkloadPlanningError("Workload planning batch not found or no access")
        payload = dict(batch.result_payload or {})
        applied = set(payload.get("appliedRecordIds") or [])
        if request.record_id not in applied:
            raise WorkloadPlanningError("Feedback is only accepted for estimates that were applied")
        task = next(
            (
                item
                for item in payload.get("tasks") or []
                if str(item.get("recordId")) == request.record_id
            ),
            None,
        )
        if not task or not task.get("estimateId"):
            raise WorkloadPlanningError("Estimate trace not found for task feedback")

        updated = await estimate_workload_service.submit_feedback(
            db,
            estimate_id=str(task["estimateId"]),
            user_id=user_id,
            feedback=EstimateFeedbackRequest(
                actualStoryPoints=request.actual_story_points,
                actualHours=request.actual_hours,
                outcomeStatus=request.outcome_status,
                accuracyRating=request.accuracy_rating,
                notes=request.notes,
            ),
        )
        if updated is None:
            raise WorkloadPlanningError("Estimate feedback target not found or no access")
        return {
            "batchId": batch.id,
            "recordId": request.record_id,
            "estimateId": updated.id,
            "status": updated.status,
            "actualStoryPoints": updated.actual_story_points,
            "actualHours": updated.actual_hours,
            "outcomeStatus": updated.outcome_status,
            "accuracyRating": updated.accuracy_rating,
            "feedbackAt": updated.feedback_at.isoformat() if updated.feedback_at else None,
        }


workload_planning_service = WorkloadPlanningService()
