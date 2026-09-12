from __future__ import annotations

import copy
import math
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.member_assignment import MemberAssignmentBatch
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workload_planning import WorkloadPlanningBatch
from app.models.workspace_member import WorkspaceMember
from app.schemas.member_assignment import (
    MemberAssignmentApplyRequest,
    MemberAssignmentPreviewRequest,
    MemberAssignmentWhatIfRequest,
)
from app.services.change_history import (
    append_change_set,
    changed_fields,
    record_meta,
    record_version,
)
from app.services.row_permissions import filter_store_for_user, require_record_access
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "task title", "title", "name"]
STATUS_NAMES = ["状态", "任务状态", "status", "state"]
OWNER_NAMES = ["负责人", "执行人", "责任人", "成员", "owner", "assignee", "assigned to"]
DEADLINE_NAMES = ["截止时间", "截止日期", "计划完成时间", "计划完成", "due date", "duedate", "deadline"]
P50_NAMES = ["p50工时", "p50", "预计工作量", "预计工时", "工作量", "estimated hours"]
DEPENDENCY_NAMES = ["前置依赖", "依赖", "依赖任务", "dependencies", "depends on"]
TASK_TAG_NAMES = ["任务类型", "类型", "标签", "技能", "技术栈", "category", "type", "tags", "skills"]
ROLE_NAMES = ["建议角色", "所需角色", "角色", "岗位", "suggested role", "role"]
DONE_TOKENS = ["已完成", "完成", "已关闭", "关闭", "done", "completed", "closed", "cancelled", "canceled", "已取消"]
MAX_TASKS = 100
MAX_MEMBERS = 50


class MemberAssignmentError(ValueError):
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


def _label(field: Optional[TableField], value: Any) -> str:
    if value in (None, "", []):
        return ""
    options = {
        str(item.get("id")): str(item.get("label") or item.get("name") or item.get("id"))
        for item in list((field.options if field else None) or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    if isinstance(value, list):
        return ", ".join(part for part in (_label(field, item) for item in value) if part)
    if isinstance(value, dict):
        candidate = (
            value.get("label")
            or value.get("name")
            or value.get("title")
            or value.get("id")
            or value.get("userId")
            or value.get("value")
        )
        return options.get(str(candidate), str(candidate or ""))
    return options.get(str(value), str(value))


def _tokens(field: Optional[TableField], value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_tokens(field, item))
        return list(dict.fromkeys(item for item in output if item))
    text = _label(field, value)
    parts = re.split(r"[,，/、;；|\s]+", text)
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _member_ids(value: Any) -> list[int]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        output: list[int] = []
        for item in value:
            output.extend(_member_ids(item))
        return list(dict.fromkeys(output))
    if isinstance(value, dict):
        value = value.get("id") or value.get("userId") or value.get("user_id") or value.get("value")
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
        value = value.get("id") or value.get("recordId") or value.get("value")
    return [str(value)] if value not in (None, "") else []


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
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _is_done(status: str) -> bool:
    value = _norm(status)
    return any(value == _norm(token) or _norm(token) in value for token in DONE_TOKENS)


def _tag_match(required: list[str], offered: list[str]) -> tuple[float, list[str]]:
    if not required:
        return 0.5, []
    req = {_norm(item): item for item in required if _norm(item)}
    offered_norm = {_norm(item) for item in offered if _norm(item)}
    matched = [raw for key, raw in req.items() if key in offered_norm]
    return len(matched) / max(len(req), 1), matched


def _role_match(role_hint: str, role_tags: list[str], skill_tags: list[str], workspace_role: str) -> bool:
    hint = _norm(role_hint)
    if not hint:
        return False
    candidates = role_tags + skill_tags + [workspace_role]
    return any(hint == _norm(item) or hint in _norm(item) or _norm(item) in hint for item in candidates)


def _confidence(top_score: float, second_score: Optional[float], has_conflict: bool) -> float:
    gap = max(0.0, top_score - (second_score if second_score is not None else 0.0))
    value = 0.38 + 0.42 * max(0.0, min(top_score, 1.0)) + 0.2 * min(gap, 0.5)
    if has_conflict:
        value -= 0.15
    return round(max(0.2, min(value, 0.95)), 2)


class MemberAssignmentService:
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
            raise MemberAssignmentError("Task table not found or no access")
        effective = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(effective, permission):
            raise PermissionError("Task table not found or no access")
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

    async def _workload_map(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        table_id: str,
        batch_id: Optional[str],
    ) -> dict[str, float]:
        if not batch_id:
            return {}
        result = await db.execute(
            select(WorkloadPlanningBatch).where(
                WorkloadPlanningBatch.id == batch_id,
                WorkloadPlanningBatch.user_id == user_id,
                WorkloadPlanningBatch.workspace_id == workspace_id,
                WorkloadPlanningBatch.table_id == table_id,
            )
        )
        batch = result.scalars().first()
        if batch is None:
            raise MemberAssignmentError("Workload planning batch not found or no access")
        output: dict[str, float] = {}
        for item in list((batch.result_payload or {}).get("tasks") or []):
            rid = str(item.get("recordId") or "")
            hours = _number(item.get("p50Hours"))
            if rid and hours is not None:
                output[rid] = max(0.0, hours)
        return output

    async def _eligible_members(
        self,
        db: AsyncSession,
        *,
        request: MemberAssignmentPreviewRequest,
    ) -> list[dict[str, Any]]:
        result = await db.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == request.workspace_id)
            .order_by(User.name.asc(), User.id.asc())
        )
        profile_by_id = {profile.user_id: profile for profile in request.member_profiles}
        members: list[dict[str, Any]] = []
        for workspace_member, user in result.all():
            if len(members) >= MAX_MEMBERS:
                break
            permission = await get_effective_permission_for_item(db, user.id, request.table_id)
            if not permission_allows(permission, "read"):
                continue
            profile = profile_by_id.get(int(user.id))
            capacity = (
                request.capacity_overrides.get(int(user.id))
                or (profile.capacity_hours if profile else 40.0)
            )
            members.append(
                {
                    "userId": int(user.id),
                    "name": str(user.name or f"User {user.id}"),
                    "workspaceRole": getattr(workspace_member.role, "value", str(workspace_member.role)),
                    "skillTags": list(profile.skill_tags if profile else []),
                    "roleTags": list(profile.role_tags if profile else []),
                    "capacityHours": round(float(capacity), 2),
                }
            )
        eligible_ids = {item["userId"] for item in members}
        if any(user_id not in eligible_ids for user_id in profile_by_id):
            raise MemberAssignmentError("Member profile contains unavailable project member")
        if any(user_id not in eligible_ids for user_id in request.capacity_overrides):
            raise MemberAssignmentError("Capacity override contains unavailable project member")
        return members

    async def _context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: MemberAssignmentPreviewRequest,
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
        title_field = _find_field(fields, TITLE_NAMES, {"text"})
        if title_field is None:
            title_field = next((field for field in fields if field.type == "text"), None)
        owner_field = _find_field(fields, OWNER_NAMES, {"member"})
        if owner_field is None:
            raise MemberAssignmentError("Task table requires a member field for assignee writeback")
        status_field = _find_field(fields, STATUS_NAMES)
        deadline_field = _find_field(fields, DEADLINE_NAMES, {"date", "text"})
        p50_field = _find_field(fields, P50_NAMES, {"number", "formula"})
        dependency_field = _find_field(fields, DEPENDENCY_NAMES, {"relation"})
        tag_fields = [
            field for field in fields
            if field.type in {"text", "select", "multiSelect", "multipleSelect"}
            and _matches(field.name, TASK_TAG_NAMES)
        ][:3]
        role_field = _find_field(fields, ROLE_NAMES, {"text", "select"})

        visible_records = {
            str(record.get("id")): dict(record)
            for record in store.get("records", [])
            if isinstance(record, dict) and record.get("id") is not None
        }
        selected_ids = request.record_ids or list(visible_records)
        selected_ids = list(dict.fromkeys(selected_ids))
        if not selected_ids:
            raise MemberAssignmentError("No visible task records available for assignment")
        if len(selected_ids) > MAX_TASKS:
            raise MemberAssignmentError(f"At most {MAX_TASKS} tasks can be planned in one batch")
        if any(record_id not in visible_records for record_id in selected_ids):
            raise PermissionError("Selected task records not found or no access")

        workloads = await self._workload_map(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            table_id=request.table_id,
            batch_id=request.workload_batch_id,
        )
        members = await self._eligible_members(db, request=request)
        if not members:
            raise MemberAssignmentError("No project member with task-table access is available")
        member_ids = {item["userId"] for item in members}
        if any(uid not in member_ids for uid in request.locked_assignments.values()):
            raise MemberAssignmentError("Locked assignment contains unavailable project member")
        if any(rid not in selected_ids for rid in request.locked_assignments):
            raise MemberAssignmentError("Locked assignment must target a selected visible task")

        version_result = await db.execute(
            select(TableRecord.id, TableRecord.version).where(
                TableRecord.table_id == request.table_id,
                TableRecord.id.in_(selected_ids),
            )
        )
        record_versions = {
            str(record_id): int(version or 1) for record_id, version in version_result.all()
        }
        if len(record_versions) != len(selected_ids):
            raise MemberAssignmentError("One or more selected tasks no longer exist")

        def task_from_record(record_id: str, record: dict[str, Any]) -> dict[str, Any]:
            tags: list[str] = []
            for field in tag_fields:
                tags.extend(_tokens(field, record.get(field.id)))
            status = _label(status_field, record.get(status_field.id)) if status_field else ""
            hours = _number(record.get(p50_field.id)) if p50_field else None
            if hours is None:
                hours = workloads.get(record_id)
            if hours is None:
                hours = request.default_task_hours
            owners = _member_ids(record.get(owner_field.id))
            role_hint = _label(role_field, record.get(role_field.id)) if role_field else ""
            return {
                "recordId": record_id,
                "title": (
                    _label(title_field, record.get(title_field.id))
                    if title_field
                    else record_id
                ) or record_id,
                "status": status,
                "done": _is_done(status),
                "deadline": _date(record.get(deadline_field.id)) if deadline_field else None,
                "p50Hours": round(max(0.0, float(hours)), 2),
                "ownerUserIds": owners,
                "taskTags": list(dict.fromkeys(tags)),
                "suggestedRole": role_hint,
                "dependencyRecordIds": (
                    _relation_ids(record.get(dependency_field.id))
                    if dependency_field
                    else []
                ),
            }

        all_tasks = {
            record_id: task_from_record(record_id, record)
            for record_id, record in visible_records.items()
        }
        selected_tasks = [all_tasks[record_id] for record_id in selected_ids]

        current_load: dict[int, float] = defaultdict(float)
        completed_visible: dict[int, int] = defaultdict(int)
        completed_tags: dict[int, set[str]] = defaultdict(set)
        for task in all_tasks.values():
            for owner_id in task["ownerUserIds"]:
                if owner_id not in member_ids:
                    continue
                if task["done"]:
                    completed_visible[owner_id] += 1
                    completed_tags[owner_id].update(_norm(tag) for tag in task["taskTags"])
                else:
                    current_load[owner_id] += float(task["p50Hours"])

        owner_multiple = bool((owner_field.property or {}).get("multiple", True))
        return {
            "permission": permission,
            "ownerFieldId": owner_field.id,
            "ownerMultiple": owner_multiple,
            "selectedTasks": selected_tasks,
            "allTasks": all_tasks,
            "members": members,
            "recordVersions": record_versions,
            "currentLoad": {uid: round(hours, 2) for uid, hours in current_load.items()},
            "completedVisible": dict(completed_visible),
            "completedTags": {uid: sorted(tags) for uid, tags in completed_tags.items()},
            "visibleRecordCount": len(visible_records),
        }

    def _recommend(
        self,
        *,
        context: dict[str, Any],
        request: MemberAssignmentPreviewRequest,
    ) -> dict[str, Any]:
        members = [dict(item) for item in context["members"]]
        member_by_id = {item["userId"]: item for item in members}
        tasks = list(context["selectedTasks"])
        all_tasks = dict(context["allTasks"])
        current_load = {
            member["userId"]: float(context["currentLoad"].get(member["userId"], 0.0))
            for member in members
        }
        projected_load = dict(current_load)
        completed_visible = context["completedVisible"]
        completed_tags = {
            int(uid): set(tags) for uid, tags in context["completedTags"].items()
        }
        today = datetime.now(timezone.utc).date()

        def deadline_sort(task: dict[str, Any]) -> tuple[date, str]:
            return (task["deadline"] or date.max, task["title"])

        outputs: list[dict[str, Any]] = []
        for task in sorted(tasks, key=deadline_sort):
            existing_all = list(task["ownerUserIds"])
            existing = [uid for uid in existing_all if uid in member_by_id]
            inaccessible_existing = [uid for uid in existing_all if uid not in member_by_id]
            locked_user_id = request.locked_assignments.get(task["recordId"])
            if (
                request.preserve_existing_assignees
                and existing_all
                and locked_user_id is not None
                and locked_user_id not in existing_all
            ):
                raise MemberAssignmentError(
                    f"Task {task['recordId']} already has an assignee; locked assignment cannot overwrite it"
                )

            public_task = {
                **task,
                "deadline": task["deadline"].isoformat() if task["deadline"] else None,
            }

            if task["done"]:
                outputs.append(
                    {
                        **public_task,
                        "fixed": True,
                        "fixedReason": "completed_task",
                        "recommendations": [],
                        "notRecommended": [],
                        "confidence": 1.0,
                        "warnings": ["任务已完成，不建议重新分配。"],
                    }
                )
                continue

            if request.preserve_existing_assignees and existing_all:
                warnings = []
                if inaccessible_existing:
                    warnings.append("任务引用了已无当前项目访问权限的负责人；保持人工分配，不由 AI 覆盖，请人工核查。")
                outputs.append(
                    {
                        **public_task,
                        "fixed": True,
                        "fixedReason": "existing_assignee",
                        "recommendations": [
                            {
                                "userId": uid,
                                "name": member_by_id[uid]["name"],
                                "score": 1.0,
                                "confidence": 1.0,
                                "currentLoadHours": round(projected_load.get(uid, 0.0), 2),
                                "capacityHours": member_by_id[uid]["capacityHours"],
                                "projectedLoadHours": round(projected_load.get(uid, 0.0), 2),
                                "projectedUtilization": round(
                                    projected_load.get(uid, 0.0)
                                    / max(float(member_by_id[uid]["capacityHours"]), 0.1),
                                    3,
                                ),
                                "reasons": ["已有人为负责人，默认锁定且不会被 AI 覆盖。"],
                                "conflicts": [],
                                "locked": True,
                            }
                            for uid in existing[: request.top_k]
                        ],
                        "notRecommended": [],
                        "confidence": 1.0,
                        "warnings": warnings,
                    }
                )
                continue

            if existing_all and not request.preserve_existing_assignees:
                for previous_owner in existing:
                    projected_load[previous_owner] = max(
                        0.0,
                        projected_load.get(previous_owner, 0.0) - float(task["p50Hours"]),
                    )

            if locked_user_id is not None:
                member = member_by_id[locked_user_id]
                before = projected_load.get(locked_user_id, 0.0)
                after = before + float(task["p50Hours"])
                projected_load[locked_user_id] = after
                conflicts = []
                if after > float(member["capacityHours"]):
                    conflicts.append(
                        f"预计分配后负载 {after:g}h 超过容量 {member['capacityHours']:g}h。"
                    )
                outputs.append(
                    {
                        **public_task,
                        "fixed": True,
                        "fixedReason": "manual_lock",
                        "recommendations": [
                            {
                                "userId": locked_user_id,
                                "name": member["name"],
                                "score": 1.0,
                                "confidence": 1.0 if not conflicts else 0.75,
                                "currentLoadHours": round(before, 2),
                                "capacityHours": member["capacityHours"],
                                "projectedLoadHours": round(after, 2),
                                "projectedUtilization": round(
                                    after / max(float(member["capacityHours"]), 0.1), 3
                                ),
                                "reasons": ["用户在预览请求中锁定该负责人，AI 不覆盖。"],
                                "conflicts": conflicts,
                                "locked": True,
                            }
                        ],
                        "notRecommended": [],
                        "confidence": 1.0 if not conflicts else 0.75,
                        "warnings": conflicts,
                    }
                )
                continue

            dependency_owners: set[int] = set()
            for dep_id in task["dependencyRecordIds"]:
                dep = all_tasks.get(dep_id)
                if dep:
                    dependency_owners.update(
                        uid for uid in dep["ownerUserIds"] if uid in member_by_id
                    )

            scored: list[dict[str, Any]] = []
            for member in members:
                uid = int(member["userId"])
                before = projected_load.get(uid, 0.0)
                capacity = max(float(member["capacityHours"]), 0.1)
                after = before + float(task["p50Hours"])
                skill_score, matched_skills = _tag_match(
                    task["taskTags"], member["skillTags"] + member["roleTags"]
                )
                role_match = _role_match(
                    task["suggestedRole"],
                    member["roleTags"],
                    member["skillTags"],
                    member["workspaceRole"],
                )
                headroom = max(0.0, min(1.0, 1.0 - before / capacity))
                history = min(float(completed_visible.get(uid, 0)) / 5.0, 1.0)
                if task["taskTags"]:
                    normalized_task_tags = {_norm(x) for x in task["taskTags"] if _norm(x)}
                    history_overlap = (
                        len(normalized_task_tags & completed_tags.get(uid, set()))
                        / max(len(normalized_task_tags), 1)
                    )
                else:
                    history_overlap = 0.0
                dependency_continuity = 1.0 if uid in dependency_owners else 0.5
                role_score = 1.0 if role_match else (0.45 if not task["suggestedRole"] else 0.15)
                score = (
                    0.36 * skill_score
                    + 0.18 * role_score
                    + 0.28 * headroom
                    + 0.10 * max(history, history_overlap)
                    + 0.08 * dependency_continuity
                )
                days_left = (
                    (task["deadline"] - today).days
                    if task["deadline"] is not None
                    else None
                )
                conflicts: list[str] = []
                if after > capacity:
                    over = round(after - capacity, 2)
                    conflicts.append(
                        f"预计分配后超出容量 {over:g}h（{after:g}/{capacity:g}h）。"
                    )
                    score -= min(0.35, 0.15 + over / capacity * 0.2)
                if days_left is not None and days_left <= 3 and before / capacity >= 0.8:
                    conflicts.append("截止时间临近且当前负载已较高。")
                    score -= 0.08
                if task["taskTags"] and not matched_skills:
                    conflicts.append("成员技能标签与任务标签无直接匹配。")
                reasons = []
                if matched_skills:
                    reasons.append("技能匹配：" + "、".join(matched_skills[:4]))
                if role_match:
                    reasons.append(f"角色匹配：{task['suggestedRole']}")
                reasons.append(f"当前可见负载 {before:g}/{capacity:g}h")
                if uid in dependency_owners:
                    reasons.append("与可见前置任务负责人一致，可减少交接")
                if completed_visible.get(uid, 0):
                    reasons.append(f"当前可见范围内已有 {completed_visible[uid]} 个已完成任务")
                scored.append(
                    {
                        "userId": uid,
                        "name": member["name"],
                        "score": round(max(0.0, min(score, 1.0)), 3),
                        "currentLoadHours": round(before, 2),
                        "capacityHours": round(capacity, 2),
                        "projectedLoadHours": round(after, 2),
                        "projectedUtilization": round(after / capacity, 3),
                        "reasons": reasons,
                        "conflicts": conflicts,
                        "locked": False,
                    }
                )

            scored.sort(key=lambda item: (-item["score"], item["projectedUtilization"], item["name"]))
            top = scored[: request.top_k]
            second_score = top[1]["score"] if len(top) > 1 else None
            task_confidence = _confidence(
                top[0]["score"],
                second_score,
                bool(top[0]["conflicts"]),
            ) if top else 0.0
            for index, item in enumerate(top):
                item["confidence"] = (
                    task_confidence
                    if index == 0
                    else round(max(0.2, min(0.9, 0.3 + 0.5 * item["score"])), 2)
                )

            bottom = list(reversed(scored[-2:]))
            not_recommended = []
            top_ids = {item["userId"] for item in top}
            for item in bottom:
                if item["userId"] in top_ids:
                    continue
                reasons = list(item["conflicts"])
                if not reasons:
                    reasons.append("综合匹配分低于当前 Top 推荐。")
                not_recommended.append(
                    {
                        "userId": item["userId"],
                        "name": item["name"],
                        "reasons": reasons[:3],
                    }
                )

            if top:
                projected_load[top[0]["userId"]] = float(top[0]["projectedLoadHours"])
            warnings = list(top[0]["conflicts"]) if top else ["没有可用候选成员。"]
            outputs.append(
                {
                    **public_task,
                    "fixed": False,
                    "fixedReason": None,
                    "recommendations": top,
                    "notRecommended": not_recommended,
                    "confidence": task_confidence,
                    "warnings": warnings,
                }
            )

        projected_members = []
        overload_ids: list[int] = []
        for member in members:
            uid = member["userId"]
            current = round(float(current_load.get(uid, 0.0)), 2)
            projected = round(float(projected_load.get(uid, current)), 2)
            capacity = round(float(member["capacityHours"]), 2)
            if projected > capacity:
                overload_ids.append(uid)
            projected_members.append(
                {
                    "userId": uid,
                    "name": member["name"],
                    "currentLoadHours": current,
                    "projectedLoadHours": projected,
                    "capacityHours": capacity,
                    "currentUtilization": round(current / max(capacity, 0.1), 3),
                    "projectedUtilization": round(projected / max(capacity, 0.1), 3),
                    "overloaded": projected > capacity,
                }
            )
        return {
            "tasks": outputs,
            "summary": {
                "taskCount": len(outputs),
                "actionableTaskCount": sum(
                    1 for item in outputs
                    if not item["done"] and item.get("fixedReason") != "existing_assignee"
                ),
                "fixedTaskCount": sum(1 for item in outputs if item["fixed"]),
                "projectedMembers": projected_members,
                "overloadedMemberIds": overload_ids,
                "capacityWarningCount": len(overload_ids),
                "visibleRecordCount": context["visibleRecordCount"],
                "calculationScope": (
                    "Only task rows visible to the current user and workspace members "
                    "with current read access to the task table are used."
                ),
            },
        }

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: MemberAssignmentPreviewRequest,
    ) -> dict[str, Any]:
        context = await self._context(db, user_id=user_id, request=request)
        recommendation = self._recommend(context=context, request=request)
        batch_id = str(uuid.uuid4())
        payload = {
            **_clone(recommendation),
            "ownerFieldId": context["ownerFieldId"],
            "ownerMultiple": context["ownerMultiple"],
            "appliedRecordIds": [],
            "appliedAssignments": {},
            "changeSetIds": [],
        }
        batch = MemberAssignmentBatch(
            id=batch_id,
            user_id=user_id,
            workspace_id=request.workspace_id,
            table_id=request.table_id,
            record_ids=[item["recordId"] for item in context["selectedTasks"]],
            request_payload=request.model_dump(mode="json", by_alias=True),
            result_payload=payload,
            record_versions=_clone(context["recordVersions"]),
            member_snapshot=_clone(context["members"]),
            status="previewed",
            change_set_ids=[],
        )
        db.add(batch)
        await db.commit()
        return {
            "batchId": batch_id,
            **recommendation,
            "ownerFieldId": context["ownerFieldId"],
            "ownerMultiple": context["ownerMultiple"],
            "recordIds": list(batch.record_ids),
        }

    async def get_batch(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        batch_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(MemberAssignmentBatch).where(
                MemberAssignmentBatch.id == batch_id,
                MemberAssignmentBatch.user_id == user_id,
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
            "memberSnapshot": row.member_snapshot,
            "status": row.status,
            "changeSetIds": row.change_set_ids,
            "applyError": row.apply_error,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        }

    async def what_if(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: MemberAssignmentWhatIfRequest,
    ) -> dict[str, Any]:
        batch = await self.get_batch(db, user_id=user_id, batch_id=request.batch_id)
        if batch is None:
            raise MemberAssignmentError("Member assignment batch not found or no access")
        preview_payload = dict(batch["request"] or {})
        merged_overrides = dict(preview_payload.get("capacityOverrides") or {})
        merged_overrides.update({str(k): v for k, v in request.capacity_overrides.items()})
        preview_payload["capacityOverrides"] = merged_overrides
        preview_request = MemberAssignmentPreviewRequest.model_validate(preview_payload)
        context = await self._context(db, user_id=user_id, request=preview_request)
        scenario = self._recommend(context=context, request=preview_request)
        base_summary = dict((batch["result"] or {}).get("summary") or {})
        base_top = {
            item["recordId"]: (
                (item.get("recommendations") or [{}])[0].get("userId")
                if item.get("recommendations")
                else None
            )
            for item in list((batch["result"] or {}).get("tasks") or [])
        }
        scenario_top = {
            item["recordId"]: (
                (item.get("recommendations") or [{}])[0].get("userId")
                if item.get("recommendations")
                else None
            )
            for item in scenario["tasks"]
        }
        changed = [
            record_id for record_id, user_id_value in scenario_top.items()
            if base_top.get(record_id) != user_id_value
        ]
        return {
            "batchId": request.batch_id,
            "baseSummary": base_summary,
            "scenario": scenario,
            "changedTopRecommendationRecordIds": changed,
        }

    async def _load_batch_for_apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        batch_id: str,
    ) -> MemberAssignmentBatch:
        result = await db.execute(
            select(MemberAssignmentBatch)
            .where(
                MemberAssignmentBatch.id == batch_id,
                MemberAssignmentBatch.user_id == user_id,
            )
            .with_for_update()
        )
        row = result.scalars().first()
        if row is None:
            raise MemberAssignmentError("Member assignment batch not found or no access")
        return row

    async def apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: MemberAssignmentApplyRequest,
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
            raise MemberAssignmentError("Member assignment batch scope mismatch")

        payload = dict(batch.result_payload or {})
        task_by_id = {
            str(item.get("recordId")): item
            for item in list(payload.get("tasks") or [])
            if item.get("recordId") is not None
        }
        applied_assignments = {
            str(key): int(value)
            for key, value in dict(payload.get("appliedAssignments") or {}).items()
        }

        selections = []
        for selection in request.assignments:
            rid = selection.record_id
            uid = int(selection.user_id)
            task = task_by_id.get(rid)
            if task is None:
                raise MemberAssignmentError("Apply selection contains task outside preview batch")
            if task.get("done"):
                raise MemberAssignmentError(f"Task {rid} is completed and cannot be assigned")
            if task.get("fixedReason") == "existing_assignee":
                raise MemberAssignmentError(
                    f"Task {rid} has an existing human assignee and is locked by default"
                )
            allowed = {
                int(item["userId"])
                for item in list(task.get("recommendations") or [])
                if item.get("userId") is not None
            }
            if uid not in allowed:
                raise MemberAssignmentError(
                    f"Selected member for task {rid} is outside the preview recommendation set"
                )
            if rid in applied_assignments:
                if applied_assignments[rid] == uid:
                    continue
                raise MemberAssignmentError(
                    f"Task {rid} was already applied with a different assignee"
                )
            selections.append((rid, uid, task))

        if not selections:
            return {
                "batchId": batch.id,
                "status": batch.status,
                "idempotent": True,
                "appliedRecordIds": sorted(applied_assignments),
                "appliedAssignments": applied_assignments,
                "changeSetIds": list(batch.change_set_ids or []),
            }

        try:
            member_result = await db.execute(
                select(WorkspaceMember, User)
                .join(User, User.id == WorkspaceMember.user_id)
                .where(
                    WorkspaceMember.workspace_id == request.workspace_id,
                    WorkspaceMember.user_id.in_([uid for _, uid, _ in selections]),
                )
            )
            member_rows = {int(user.id): user for _, user in member_result.all()}
            if len(member_rows) != len({uid for _, uid, _ in selections}):
                raise MemberAssignmentError("Selected member no longer belongs to the workspace")
            for uid in member_rows:
                current_permission = await get_effective_permission_for_item(
                    db, uid, request.table_id
                )
                if not permission_allows(current_permission, "read"):
                    raise MemberAssignmentError(
                        "Selected member no longer has project task access"
                    )

            for rid, _, _ in selections:
                await require_record_access(
                    db,
                    request.table_id,
                    rid,
                    user_id=user_id,
                    table_permission=table_permission,
                )

            records_result = await db.execute(
                select(TableRecord)
                .where(
                    TableRecord.table_id == request.table_id,
                    TableRecord.id.in_([rid for rid, _, _ in selections]),
                )
                .with_for_update()
            )
            records = {str(row.id): row for row in records_result.scalars().all()}
            if len(records) != len(selections):
                raise MemberAssignmentError("One or more previewed tasks no longer exist")

            owner_field_id = str(payload.get("ownerFieldId") or "")
            field_result = await db.execute(
                select(TableField).where(
                    TableField.table_id == request.table_id,
                    TableField.id == owner_field_id,
                    TableField.type == "member",
                )
            )
            owner_field = field_result.scalars().first()
            if owner_field is None:
                raise MemberAssignmentError("Assignee member field changed after preview")

            history_items: list[dict[str, Any]] = []
            for rid, uid, task in selections:
                record = records[rid]
                expected = int((batch.record_versions or {}).get(rid) or 1)
                before_version = record_version(record)
                if before_version != expected:
                    raise MemberAssignmentError(
                        f"Task {rid} changed after preview; regenerate assignment suggestions"
                    )
                before_data = dict(record.data or {})
                current_owners = _member_ids(before_data.get(owner_field_id))
                preview_owners = [int(value) for value in list(task.get("ownerUserIds") or [])]
                if current_owners != preview_owners:
                    raise MemberAssignmentError(
                        f"Task {rid} assignee changed after preview; regenerate assignment suggestions"
                    )
                after_data = dict(before_data)
                after_data[owner_field_id] = (
                    [str(uid)] if bool(payload.get("ownerMultiple", True)) else str(uid)
                )
                changed = changed_fields(before_data, after_data)
                if not changed:
                    continue
                before_meta = record_meta(record)
                record.data = after_data
                record.version = before_version + 1
                history_items.append(
                    {
                        "table_id": request.table_id,
                        "entity_type": "record",
                        "entity_id": rid,
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
                operation="member_assignment_apply",
                source="member_assignment",
                trace_id=batch.id,
                summary=f"Apply AI member assignment to {len(history_items)} task(s)",
                items=history_items,
            )
            for rid, uid, _ in selections:
                applied_assignments[rid] = uid
            change_set_ids = list(batch.change_set_ids or [])
            if change_set.id not in change_set_ids:
                change_set_ids.append(change_set.id)
            payload["appliedAssignments"] = {
                rid: uid for rid, uid in sorted(applied_assignments.items())
            }
            payload["appliedRecordIds"] = sorted(applied_assignments)
            payload["changeSetIds"] = change_set_ids
            batch.result_payload = payload
            batch.change_set_ids = change_set_ids
            actionable = {
                str(item["recordId"])
                for item in task_by_id.values()
                if not item.get("done") and item.get("fixedReason") != "existing_assignee"
            }
            batch.status = (
                "applied"
                if actionable and actionable <= set(applied_assignments)
                else "partially_applied"
            )
            batch.apply_error = None
            batch.applied_at = datetime.now(timezone.utc)
            await db.commit()
            return {
                "batchId": batch.id,
                "status": batch.status,
                "idempotent": False,
                "appliedRecordIds": sorted(applied_assignments),
                "appliedAssignments": payload["appliedAssignments"],
                "changeSetIds": change_set_ids,
                "changeSetId": change_set.id,
            }
        except Exception as exc:
            await db.rollback()
            retry_result = await db.execute(
                select(MemberAssignmentBatch).where(
                    MemberAssignmentBatch.id == request.batch_id,
                    MemberAssignmentBatch.user_id == user_id,
                )
            )
            retry = retry_result.scalars().first()
            if retry is not None:
                retry.apply_error = str(exc)[:4000]
                if retry.status == "previewed":
                    retry.status = "apply_failed"
                await db.commit()
            raise


member_assignment_service = MemberAssignmentService()
