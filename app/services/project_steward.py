from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.change_history import ChangeItem, ChangeSet
from app.models.project_steward import ProjectStewardDiagnosis
from app.models.smart_table import TableRecord, WorkspaceItem
from app.schemas.project_steward import ProjectStewardRanking, ProjectStewardRequest
from app.services.ai_tool_router import tool_router_service
from app.services.deepseek_helper import DEEPSEEK_BASE_URL, build_deepseek_model
from app.services.encryption import decrypt_api_key
from app.services.row_permissions import filter_store_for_user
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"]
STATUS_NAMES = ["状态", "任务状态", "status", "state"]
DEADLINE_NAMES = ["截止时间", "截止日期", "计划完成时间", "计划完成", "due date", "duedate", "deadline"]
OWNER_NAMES = ["负责人", "执行人", "责任人", "成员", "owner", "assignee", "assigned to"]
DEPENDENCY_NAMES = ["前置依赖", "依赖", "依赖任务", "dependencies", "depends on"]
P50_NAMES = ["p50工时", "p50", "预计工作量", "预计工时", "工作量", "estimated hours"]
ACTUAL_HOURS_NAMES = ["实际工时", "实际工作量", "actual hours", "actualhours"]
PRIORITY_NAMES = ["优先级", "priority"]
DONE_TOKENS = ["已完成", "完成", "已关闭", "关闭", "done", "completed", "closed", "cancelled", "canceled", "已取消"]
BLOCKED_TOKENS = ["阻塞", "已阻塞", "卡住", "blocked", "stuck", "on hold", "暂停"]


class ProjectStewardError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _matches(name: str, candidates: Iterable[str]) -> bool:
    normalized = _norm(name)
    return any(normalized == _norm(item) or _norm(item) in normalized for item in candidates)


def _field(fields: list[dict[str, Any]], names: list[str], types: Optional[set[str]] = None) -> Optional[dict[str, Any]]:
    return next(
        (
            item
            for item in fields
            if (not types or str(item.get("type") or "") in types)
            and _matches(str(item.get("name") or ""), names)
        ),
        None,
    )


def _label(field: Optional[dict[str, Any]], value: Any) -> str:
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
        candidate = (
            value.get("label")
            or value.get("name")
            or value.get("title")
            or value.get("email")
            or value.get("id")
            or value.get("userId")
            or value.get("value")
        )
        return options.get(str(candidate), str(candidate or ""))
    return options.get(str(value), str(value))


def _owners(field: Optional[dict[str, Any]], value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_owners(field, item))
        return list(dict.fromkeys(item for item in values if item))
    return [_label(field, value)] if _label(field, value) else []


def _relations(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return list(dict.fromkeys(str(item) for item in value if item not in (None, "")))
    if isinstance(value, dict):
        candidate = value.get("id") or value.get("recordId") or value.get("value")
        return [str(candidate)] if candidate not in (None, "") else []
    return [str(value)]


def _number(value: Any) -> Optional[float]:
    if value in (None, "", []):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> Optional[date]:
    if value in (None, "", []):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_done(status: str) -> bool:
    value = _norm(status)
    return any(value == _norm(token) or _norm(token) in value for token in DONE_TOKENS)


def _is_blocked(status: str) -> bool:
    value = _norm(status)
    return any(value == _norm(token) or _norm(token) in value for token in BLOCKED_TOKENS)


def _rank_severity(value: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(value, 5)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ProjectStewardError(f"Unknown timezone: {name}") from exc


def _evidence(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "tableId": task["tableId"],
        "recordId": task["recordId"],
        "title": task["title"],
        "deepLink": task["deepLink"],
    }


def _conclusion(
    conclusion_id: str,
    kind: str,
    category: str,
    severity: str,
    title: str,
    detail: str,
    tasks: list[dict[str, Any]],
    metric: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "id": conclusion_id,
        "kind": kind,
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "evidence": [_evidence(task) for task in tasks[:8]],
        "metric": metric or {},
    }


def _topology(keys: list[str], dependencies: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    key_set = set(keys)
    indegree = {key: 0 for key in keys}
    outgoing = {key: [] for key in keys}
    for key in keys:
        for predecessor in dependencies.get(key, []):
            if predecessor not in key_set or predecessor == key:
                continue
            indegree[key] += 1
            outgoing[predecessor].append(key)
    queue = sorted(key for key, count in indegree.items() if count == 0)
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for successor in sorted(outgoing[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
                queue.sort()
    return order, sorted(key for key, count in indegree.items() if count > 0)


def _critical_path(tasks: list[dict[str, Any]], dependencies: dict[str, list[str]]) -> tuple[list[str], float, list[str]]:
    keys = [task["taskKey"] for task in tasks]
    order, cycle = _topology(keys, dependencies)
    if cycle:
        return [], 0.0, cycle
    durations = {task["taskKey"]: max(float(task.get("p50Hours") or 0), 1.0) for task in tasks}
    longest: dict[str, float] = {}
    previous: dict[str, Optional[str]] = {}
    for key in order:
        predecessors = [item for item in dependencies.get(key, []) if item in longest]
        if not predecessors:
            longest[key] = durations[key]
            previous[key] = None
        else:
            best = max(predecessors, key=lambda item: longest[item])
            longest[key] = longest[best] + durations[key]
            previous[key] = best
    if not longest:
        return [], 0.0, []
    cursor: Optional[str] = max(longest, key=longest.get)
    path: list[str] = []
    while cursor:
        path.append(cursor)
        cursor = previous.get(cursor)
    path.reverse()
    return path, round(longest[path[-1]], 2), []


class ProjectStewardService:
    async def _provider(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        model_override: Optional[str],
    ) -> tuple[OpenAIChatModel, str, str]:
        config = await tool_router_service.load_user_ai_config(db, user_id)
        provider = (config.provider or "deepseek").strip().lower()
        api_key = decrypt_api_key(config.api_key_encrypted)
        if provider == "openai":
            base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model = model_override or config.model or settings.OPENAI_MODEL or "gpt-4o-mini"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            return OpenAIChatModel(model, provider=OpenAIProvider(openai_client=client)), "openai", model
        if provider in {"deepseek", "deepseek-chat", "deepseek-reasoner"}:
            model = model_override or config.model or settings.DEEPSEEK_MODEL or "deepseek-chat"
            base_url = (settings.DEEPSEEK_BASE_URL or DEEPSEEK_BASE_URL).rstrip("/")
            return build_deepseek_model(api_key, model, base_url), "deepseek", model
        base_url = (settings.OPENAI_BASE_URL or settings.DEEPSEEK_BASE_URL or "https://api.openai.com/v1").rstrip("/")
        model = model_override or config.model or settings.OPENAI_MODEL or settings.DEEPSEEK_MODEL or "gpt-4o-mini"
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        return OpenAIChatModel(model, provider=OpenAIProvider(openai_client=client)), "openai-compatible", model

    async def _scope(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: ProjectStewardRequest,
    ) -> dict[str, Any]:
        tables: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        remaining = request.max_records
        truncated = False

        for index, table_id in enumerate(request.table_ids):
            item_result = await db.execute(
                select(WorkspaceItem).where(
                    WorkspaceItem.id == table_id,
                    WorkspaceItem.workspace_id == request.workspace_id,
                    WorkspaceItem.type == "table",
                )
            )
            item = item_result.scalars().first()
            if item is None:
                raise ProjectStewardError("Project table not found or no access")
            permission = await get_effective_permission_for_item(db, user_id, table_id)
            if not permission_allows(permission, "read"):
                raise PermissionError("Project table not found or no access")

            store = await get_full_store(db, table_id)
            visible, _ = await filter_store_for_user(
                db,
                table_id,
                store,
                user_id=user_id,
                table_permission=permission,
            )
            fields = [dict(field) for field in visible.get("fields", []) if isinstance(field, dict)]
            source_records = [dict(record) for record in visible.get("records", []) if isinstance(record, dict)]
            records = source_records[:remaining]
            if len(records) < len(source_records):
                truncated = True
            remaining -= len(records)

            title_field = _field(fields, TITLE_NAMES, {"text"}) or next(
                (field for field in fields if field.get("type") == "text"),
                None,
            )
            status_field = _field(fields, STATUS_NAMES)
            deadline_field = _field(fields, DEADLINE_NAMES, {"date", "text"})
            owner_field = _field(fields, OWNER_NAMES)
            dependency_field = _field(fields, DEPENDENCY_NAMES, {"relation"})
            target_table_id = str(
                ((dependency_field or {}).get("property") or {}).get("targetTableId")
                or table_id
            )
            p50_field = _field(fields, P50_NAMES, {"number", "formula"})
            actual_field = _field(fields, ACTUAL_HOURS_NAMES, {"number", "formula"})
            priority_field = _field(fields, PRIORITY_NAMES)

            table_tasks: list[dict[str, Any]] = []
            for record in records:
                record_id = str(record.get("id") or "")
                if not record_id:
                    continue
                title_id = str((title_field or {}).get("id") or "")
                status_id = str((status_field or {}).get("id") or "")
                deadline_id = str((deadline_field or {}).get("id") or "")
                owner_id = str((owner_field or {}).get("id") or "")
                dependency_id = str((dependency_field or {}).get("id") or "")
                p50_id = str((p50_field or {}).get("id") or "")
                actual_id = str((actual_field or {}).get("id") or "")
                priority_id = str((priority_field or {}).get("id") or "")
                relation_ids = _relations(record.get(dependency_id))
                task = {
                    "taskKey": f"{table_id}:{record_id}",
                    "tableId": table_id,
                    "recordId": record_id,
                    "title": _label(title_field, record.get(title_id)) or record_id,
                    "status": _label(status_field, record.get(status_id)),
                    "deadline": (_date(record.get(deadline_id)) or None),
                    "owners": _owners(owner_field, record.get(owner_id)),
                    "hasOwnerField": owner_field is not None,
                    "dependencyTaskKeys": [f"{target_table_id}:{dep}" for dep in relation_ids],
                    "p50Hours": _number(record.get(p50_id)),
                    "actualHours": _number(record.get(actual_id)),
                    "priority": _label(priority_field, record.get(priority_id)),
                    "statusFieldId": status_id or None,
                    "deepLink": (
                        f"/workbench/{table_id}/{item.default_view_id}?recordId={record_id}"
                        if item.default_view_id
                        else f"/workbench/{table_id}?recordId={record_id}"
                    ),
                }
                task["done"] = _is_done(task["status"])
                task["blockedStatus"] = _is_blocked(task["status"])
                table_tasks.append(task)
                tasks.append(task)

            tables.append(
                {
                    "tableId": table_id,
                    "name": item.name,
                    "defaultViewId": item.default_view_id,
                    "fields": fields,
                    "tasks": table_tasks,
                }
            )
            if remaining <= 0:
                if index < len(request.table_ids) - 1:
                    truncated = True
                break

        if not tasks:
            raise ProjectStewardError("No visible project records are available for diagnosis")

        ids_by_table: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            ids_by_table[task["tableId"]].append(task["recordId"])
        versions: dict[tuple[str, str], int] = {}
        for table_id, record_ids in ids_by_table.items():
            result = await db.execute(
                select(TableRecord.id, TableRecord.version).where(
                    TableRecord.table_id == table_id,
                    TableRecord.id.in_(record_ids),
                )
            )
            for record_id, version in result.all():
                versions[(table_id, str(record_id))] = int(version or 1)
        for task in tasks:
            task["recordVersion"] = versions.get(
                (task["tableId"], task["recordId"]),
                1,
            )

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "workspaceId": request.workspace_id,
                    "userId": user_id,
                    "truncated": truncated,
                    "tables": [
                        {
                            "tableId": table["tableId"],
                            "fields": [
                                {
                                    "id": field.get("id"),
                                    "name": field.get("name"),
                                    "type": field.get("type"),
                                    "options": field.get("options"),
                                    "property": field.get("property"),
                                }
                                for field in table["fields"]
                            ],
                            "records": sorted(
                                [
                                    {
                                        "id": task["recordId"],
                                        "version": task["recordVersion"],
                                    }
                                    for task in table["tasks"]
                                ],
                                key=lambda item: item["id"],
                            ),
                        }
                        for table in tables
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "tables": tables,
            "tasks": tasks,
            "visibleRecordCount": len(tasks),
            "truncated": truncated,
            "fingerprint": fingerprint,
        }

    async def _status_activity(
        self,
        db: AsyncSession,
        tasks: list[dict[str, Any]],
    ) -> dict[tuple[str, str], datetime]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task in tasks:
            if task.get("statusFieldId"):
                grouped[task["tableId"]].append(task)
        output: dict[tuple[str, str], datetime] = {}
        for table_id, table_tasks in grouped.items():
            field_by_record = {
                task["recordId"]: task["statusFieldId"]
                for task in table_tasks
            }
            result = await db.execute(
                select(ChangeItem, ChangeSet.created_at)
                .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
                .where(
                    ChangeItem.table_id == table_id,
                    ChangeItem.entity_type == "record",
                    ChangeItem.entity_id.in_(list(field_by_record)),
                )
                .order_by(ChangeSet.created_at.desc())
            )
            for item, changed_at in result.all():
                field_id = field_by_record.get(str(item.entity_id))
                if not field_id or field_id not in list(item.changed_fields or []):
                    continue
                key = (table_id, str(item.entity_id))
                if key in output or changed_at is None:
                    continue
                if changed_at.tzinfo is None:
                    changed_at = changed_at.replace(tzinfo=timezone.utc)
                output[key] = changed_at
        return output

    def _diagnose(
        self,
        *,
        scope: dict[str, Any],
        request: ProjectStewardRequest,
        generated_at: datetime,
        activity: dict[tuple[str, str], datetime],
    ) -> dict[str, Any]:
        today = generated_at.astimezone(_timezone(request.timezone)).date()
        due_soon_cutoff = today + timedelta(days=request.due_soon_days)
        overload_cutoff = today + timedelta(days=request.overload_window_days)
        stale_cutoff = generated_at - timedelta(days=request.stale_task_days)
        tasks = list(scope["tasks"])
        by_key = {task["taskKey"]: task for task in tasks}

        overdue = sorted(
            [
                task for task in tasks
                if not task["done"]
                and task["deadline"]
                and task["deadline"] < today
            ],
            key=lambda task: (task["deadline"], task["title"]),
        )
        due_soon = sorted(
            [
                task for task in tasks
                if not task["done"]
                and task["deadline"]
                and today <= task["deadline"] <= due_soon_cutoff
            ],
            key=lambda task: (task["deadline"], task["title"]),
        )
        unassigned = [
            task
            for task in tasks
            if not task["done"] and task["hasOwnerField"] and not task["owners"]
        ]
        variance = sorted(
            [
                task for task in tasks
                if task["actualHours"] is not None
                and task["p50Hours"] is not None
                and task["actualHours"] > task["p50Hours"] * 1.2
            ],
            key=lambda task: task["actualHours"] / max(task["p50Hours"], 0.1),
            reverse=True,
        )
        stale = [
            task for task in tasks
            if not task["done"]
            and activity.get((task["tableId"], task["recordId"])) is not None
            and activity[(task["tableId"], task["recordId"])] < stale_cutoff
        ]

        facts: list[dict[str, Any]] = []
        if overdue:
            facts.append(
                _conclusion(
                    "fact-overdue",
                    "fact",
                    "overdue",
                    "critical" if len(overdue) >= 3 else "high",
                    f"有 {len(overdue)} 个可见任务已逾期",
                    f"最早逾期任务为“{overdue[0]['title']}”，截止日期 {overdue[0]['deadline'].isoformat()}。统计仅基于当前用户可见记录。",
                    overdue,
                    {"count": len(overdue)},
                )
            )
        if due_soon:
            facts.append(
                _conclusion(
                    "fact-due-soon",
                    "fact",
                    "due_soon",
                    "medium",
                    f"未来 {request.due_soon_days} 天内有 {len(due_soon)} 个任务到期",
                    "这些任务尚未完成，需要优先确认交付条件和阻塞项。",
                    due_soon,
                    {"count": len(due_soon), "windowDays": request.due_soon_days},
                )
            )
        if unassigned:
            facts.append(
                _conclusion(
                    "fact-unassigned",
                    "fact",
                    "unassigned",
                    "medium",
                    f"有 {len(unassigned)} 个未完成任务没有负责人",
                    "未分配负责人会增加无人推进或责任边界不清的风险。",
                    unassigned,
                    {"count": len(unassigned)},
                )
            )
        if variance:
            worst = variance[0]
            ratio = round(worst["actualHours"] / max(worst["p50Hours"], 0.1), 2)
            facts.append(
                _conclusion(
                    "fact-estimate-variance",
                    "fact",
                    "estimate_variance",
                    "high" if ratio >= 1.5 else "medium",
                    f"有 {len(variance)} 个任务实际工时明显超过 P50",
                    f"“{worst['title']}”实际工时约为 P50 的 {ratio} 倍，正在消耗项目缓冲。",
                    variance,
                    {"count": len(variance), "worstRatio": ratio},
                )
            )
        if stale:
            facts.append(
                _conclusion(
                    "fact-stale-status",
                    "fact",
                    "stale_status",
                    "medium",
                    f"有 {len(stale)} 个任务超过 {request.stale_task_days} 天未发生状态变化",
                    "该结论只在存在真实状态变更历史时生成，不使用创建时间冒充状态变化时间。",
                    stale,
                    {"count": len(stale), "staleDays": request.stale_task_days},
                )
            )

        dependencies: dict[str, list[str]] = {}
        incomplete: dict[str, list[str]] = {}
        for task in tasks:
            visible_deps = [
                dep for dep in task["dependencyTaskKeys"]
                if dep in by_key
            ]
            dependencies[task["taskKey"]] = visible_deps
            pending = [dep for dep in visible_deps if not by_key[dep]["done"]]
            if pending:
                incomplete[task["taskKey"]] = pending

        blocked = sorted(
            [
                task for task in tasks
                if not task["done"]
                and (task["blockedStatus"] or task["taskKey"] in incomplete)
            ],
            key=lambda task: (
                not task["blockedStatus"],
                task["deadline"] or date.max,
                task["title"],
            ),
        )
        inferences: list[dict[str, Any]] = []
        if blocked:
            evidence_tasks: list[dict[str, Any]] = []
            for task in blocked:
                evidence_tasks.append(task)
                evidence_tasks.extend(
                    by_key[dep]
                    for dep in incomplete.get(task["taskKey"], [])
                    if dep in by_key
                )
            inferences.append(
                _conclusion(
                    "inference-blocked",
                    "inference",
                    "blocked",
                    "high",
                    f"当前可见范围内有 {len(blocked)} 个任务存在阻塞信号",
                    "阻塞信号来自明确的阻塞状态或尚未完成的可见前置依赖；隐藏记录不会被用于推断或计数。",
                    list({task["taskKey"]: task for task in evidence_tasks}.values()),
                    {"count": len(blocked)},
                )
            )

        path, path_hours, cycle = _critical_path(tasks, dependencies)
        if cycle:
            inferences.append(
                _conclusion(
                    "inference-dependency-cycle",
                    "inference",
                    "dependency_cycle",
                    "critical",
                    "可见任务依赖中检测到循环",
                    "循环依赖会让关键路径和完成顺序无法可靠计算，应先解除循环关系。",
                    [by_key[key] for key in cycle if key in by_key],
                    {"taskKeys": cycle},
                )
            )
        elif path:
            path_tasks = [by_key[key] for key in path if key in by_key]
            risky = [
                task for task in path_tasks
                if not task["done"]
                and (
                    task["blockedStatus"]
                    or task["taskKey"] in incomplete
                    or (task["deadline"] and task["deadline"] <= due_soon_cutoff)
                )
            ]
            if risky:
                inferences.append(
                    _conclusion(
                        "inference-critical-path",
                        "inference",
                        "critical_path",
                        "high",
                        f"关键路径上有 {len(risky)} 个高风险任务",
                        f"按可见依赖关系和 P50 工时估算，当前关键路径约 {path_hours:g} 小时；这是排期推断，不是事实工期承诺。",
                        path_tasks,
                        {"p50Hours": path_hours, "taskKeys": path},
                    )
                )

        load_by_owner: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if task["done"] or not task["owners"] or not task["deadline"] or task["deadline"] > overload_cutoff:
                continue
            for owner in task["owners"]:
                bucket = load_by_owner.setdefault(owner, {"hours": 0.0, "tasks": []})
                bucket["tasks"].append(task)
                bucket["hours"] += float(task["p50Hours"] or 0.0)
        overloaded = [
            (owner, data)
            for owner, data in load_by_owner.items()
            if data["hours"] > request.overload_hours
            or (data["hours"] <= 0 and len(data["tasks"]) >= 5)
        ]
        overloaded.sort(key=lambda item: (-item[1]["hours"], -len(item[1]["tasks"]), item[0]))
        for index, (owner, data) in enumerate(overloaded[:5], start=1):
            hours = round(float(data["hours"]), 2)
            inferences.append(
                _conclusion(
                    f"inference-owner-overload-{index}",
                    "inference",
                    "owner_overload",
                    "high" if hours >= request.overload_hours * 1.25 else "medium",
                    f"{owner} 可能存在短期负载过高",
                    (
                        f"未来 {request.overload_window_days} 天内，{owner} 的可见未完成任务 P50 合计约 {hours:g} 小时，"
                        f"启发式阈值为 {request.overload_hours:g} 小时。这是基于当前可见任务的推断，不等同于真实可用产能。"
                    ),
                    data["tasks"],
                    {"owner": owner, "p50Hours": hours, "taskCount": len(data["tasks"])},
                )
            )

        candidates: list[tuple[int, dict[str, Any], list[str]]] = []
        for task in tasks:
            if task["done"]:
                continue
            score = 0
            reasons: list[str] = []
            if task in overdue:
                score += 100
                reasons.append("已逾期")
            if task["blockedStatus"] or task["taskKey"] in incomplete:
                score += 80
                reasons.append("存在阻塞")
            if task in due_soon:
                score += 60
                reasons.append("即将到期")
            if task["taskKey"] in path:
                score += 40
                reasons.append("位于关键路径")
            if not task["owners"]:
                score += 20
                reasons.append("尚无负责人")
            if any(token in _norm(task["priority"]) for token in ["高", "high", "urgent", "紧急", "p0", "p1"]):
                score += 15
                reasons.append("高优先级")
            candidates.append((score, task, reasons))
        candidates.sort(
            key=lambda item: (
                -item[0],
                item[1]["deadline"] or date.max,
                item[1]["title"],
            )
        )
        suggestions = [
            _conclusion(
                f"suggestion-focus-{index}",
                "suggestion",
                "next_action",
                "high" if score >= 100 else "medium",
                f"优先推进“{task['title']}”",
                f"建议优先核实并推进该任务，当前排序依据：{'、'.join(reasons) or '未完成'}。不会自动修改 QTable 数据。",
                [task],
                {"priorityScore": score},
            )
            for index, (score, task, reasons) in enumerate(candidates[:3], start=1)
        ]

        facts.sort(key=lambda item: (_rank_severity(item["severity"]), item["title"]))
        inferences.sort(key=lambda item: (_rank_severity(item["severity"]), item["title"]))
        return {
            "facts": facts,
            "inferences": inferences,
            "suggestions": suggestions,
            "diagnostics": {
                "overdueCount": len(overdue),
                "dueSoonCount": len(due_soon),
                "blockedCount": len(blocked),
                "unassignedCount": len(unassigned),
                "staleStatusCount": len(stale),
                "estimateVarianceCount": len(variance),
                "overloadedOwnerCount": len(overloaded),
                "criticalPathTaskKeys": path,
                "criticalPathP50Hours": path_hours,
                "dependencyCycleTaskKeys": cycle,
            },
        }

    def _fallback_order(
        self,
        question: str,
        conclusions: list[dict[str, Any]],
    ) -> list[str]:
        q = _norm(question)
        bonus: dict[str, int] = defaultdict(int)
        if any(token in q for token in [_norm("延期"), _norm("逾期"), _norm("delay")]):
            for category in ["overdue", "estimate_variance", "blocked", "critical_path"]:
                bonus[category] += 50
        if any(token in q for token in [_norm("阻塞"), _norm("依赖"), _norm("block")]):
            for category in ["blocked", "dependency_cycle", "critical_path"]:
                bonus[category] += 60
        if any(token in q for token in [_norm("下一步"), _norm("最应该"), _norm("今天"), _norm("优先")]):
            bonus["next_action"] += 80
        ranked = sorted(
            conclusions,
            key=lambda item: (
                -bonus[item["category"]],
                _rank_severity(item["severity"]),
                0 if item["kind"] == "fact" else 1 if item["kind"] == "inference" else 2,
                item["title"],
            ),
        )
        return [item["id"] for item in ranked]

    async def _rank(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: ProjectStewardRequest,
        conclusions: list[dict[str, Any]],
    ) -> tuple[list[str], str, Optional[str]]:
        fallback = self._fallback_order(request.question, conclusions)
        if not conclusions:
            return fallback, "deterministic-fallback", None
        try:
            model, provider, model_name = await self._provider(
                db,
                user_id=user_id,
                model_override=request.model,
            )
            if not tool_router_service.model_supports_tool_calling(model_name):
                model, provider, model_name = await self._provider(
                    db,
                    user_id=user_id,
                    model_override=settings.DEEPSEEK_MODEL or "deepseek-chat",
                )
            agent = Agent(
                model=model,
                output_type=ProjectStewardRanking,
                system_prompt=(
                    "你是 QTable AI 项目管家的结论排序器。只能对给定 conclusion id 排序，"
                    "禁止新增、改写或补充事实。优先选择最能回答问题、风险最高且证据最直接的结论。"
                ),
                model_settings=OpenAIChatModelSettings(temperature=0.0),
                retries={"output": 2},
            )
            catalog = [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "category": item["category"],
                    "severity": item["severity"],
                    "title": item["title"],
                    "detail": item["detail"],
                }
                for item in conclusions
            ]
            run = await agent.run(
                f"用户问题：\n{request.question}\n\n"
                "只返回下列结论 id 的优先级顺序：\n"
                f"{json.dumps(catalog, ensure_ascii=False)}"
            )
            allowed = {item["id"] for item in conclusions}
            ordered = [
                item
                for item in run.output.ordered_conclusion_ids
                if item in allowed
            ]
            ordered.extend(item for item in fallback if item not in ordered)
            return ordered, provider, model_name
        except Exception:
            return fallback, "deterministic-fallback", None

    def _answer(
        self,
        conclusions: list[dict[str, Any]],
        ordered_ids: list[str],
        truncated: bool,
    ) -> str:
        by_id = {item["id"]: item for item in conclusions}
        ordered = [by_id[item] for item in ordered_ids if item in by_id]
        parts: list[str] = []
        for kind, label in [
            ("fact", "事实"),
            ("inference", "推断"),
            ("suggestion", "建议"),
        ]:
            selected = [item for item in ordered if item["kind"] == kind][:2]
            if selected:
                parts.append(f"{label}：" + "；".join(item["title"] for item in selected))
        if not parts:
            parts.append("当前可见数据中没有发现明确的逾期、阻塞或高风险信号。")
        if truncated:
            parts.append("本次诊断已达到记录上限，结论被明确标记为截断，不能视为完整项目结论。")
        return "\n".join(parts)

    async def ask(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: ProjectStewardRequest,
    ) -> dict[str, Any]:
        generated_at = _now()
        scope = await self._scope(db, user_id=user_id, request=request)
        activity = await self._status_activity(db, scope["tasks"])
        diagnosis = self._diagnose(
            scope=scope,
            request=request,
            generated_at=generated_at,
            activity=activity,
        )
        conclusions = (
            diagnosis["facts"]
            + diagnosis["inferences"]
            + diagnosis["suggestions"]
        )
        ordering, provider, model_name = await self._rank(
            db,
            user_id=user_id,
            request=request,
            conclusions=conclusions,
        )
        diagnosis_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        payload = {
            "diagnosisId": diagnosis_id,
            "traceId": trace_id,
            "question": request.question,
            "answer": self._answer(conclusions, ordering, scope["truncated"]),
            "generatedAt": generated_at.isoformat(),
            "snapshot": {
                "capturedAt": generated_at.isoformat(),
                "fingerprint": scope["fingerprint"],
                "visibleRecordCount": scope["visibleRecordCount"],
                "tableIds": [table["tableId"] for table in scope["tables"]],
                "truncated": scope["truncated"],
                "isStale": False,
            },
            "facts": diagnosis["facts"],
            "inferences": diagnosis["inferences"],
            "suggestions": diagnosis["suggestions"],
            "diagnostics": diagnosis["diagnostics"],
            "ranking": ordering,
            "provider": provider,
            "model": model_name,
            "readOnly": True,
        }
        db.add(
            ProjectStewardDiagnosis(
                id=diagnosis_id,
                trace_id=trace_id,
                user_id=user_id,
                workspace_id=request.workspace_id,
                project_id=request.project_id,
                table_ids=[table["tableId"] for table in scope["tables"]],
                question=request.question,
                request_payload=request.model_dump(mode="json", by_alias=True),
                snapshot_fingerprint=scope["fingerprint"],
                result_payload=payload,
                provider=provider,
                model=model_name,
            )
        )
        await db.commit()
        return payload

    async def get_diagnosis(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        diagnosis_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(ProjectStewardDiagnosis).where(
                ProjectStewardDiagnosis.id == diagnosis_id,
                ProjectStewardDiagnosis.user_id == user_id,
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        try:
            request = ProjectStewardRequest.model_validate(row.request_payload)
            scope = await self._scope(db, user_id=user_id, request=request)
            stale = scope["fingerprint"] != row.snapshot_fingerprint
            current_snapshot = {
                "capturedAt": _now().isoformat(),
                "fingerprint": scope["fingerprint"],
                "visibleRecordCount": scope["visibleRecordCount"],
                "tableIds": [table["tableId"] for table in scope["tables"]],
                "truncated": scope["truncated"],
            }
            if stale:
                return {
                    "diagnosisId": row.id,
                    "createdAt": row.created_at.isoformat() if row.created_at else None,
                    "isStale": True,
                    "staleReason": (
                        "Visible project data, schema, or permission scope changed after this diagnosis. "
                        "Rerun the steward before relying on previous conclusions."
                    ),
                    "currentSnapshot": current_snapshot,
                    "result": None,
                }
            payload = dict(row.result_payload or {})
            payload["snapshot"] = {
                **dict(payload.get("snapshot") or {}),
                "isStale": False,
            }
            return {
                "diagnosisId": row.id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "isStale": False,
                "staleReason": None,
                "currentSnapshot": current_snapshot,
                "result": payload,
            }
        except (PermissionError, ProjectStewardError, ValueError):
            return {
                "diagnosisId": row.id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "isStale": True,
                "staleReason": (
                    "Current permission scope no longer allows this diagnosis to be reconstructed. "
                    "Previous details are redacted."
                ),
                "currentSnapshot": None,
                "result": None,
            }


project_steward_service = ProjectStewardService()
