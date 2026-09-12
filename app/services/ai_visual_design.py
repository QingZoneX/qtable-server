from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModelSettings
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    ALLOWED_VIEW_TYPES,
    DEFAULT_TOOLBAR_ITEMS,
    _build_default_view_config,
    _next_view_name,
)
from app.models.ai_visual_design import AiVisualDesignPlan
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.schemas.ai_visual_design import (
    AiVisualDesignApplyRequest,
    AiVisualDesignPreviewRequest,
    AiVisualDesignProposal,
    DashboardDesign,
    DashboardWidgetDesign,
    ViewDesign,
)
from app.services.dashboard_cache import get_dashboard_cache
from app.services.dashboard_runtime import (
    DashboardValidationError,
    NUMERIC_FIELD_TYPES,
    validate_widget_candidate,
)
from app.services.project_steward import project_steward_service
from app.services.workspace import get_effective_permission_for_item, permission_allows


MAX_CONTEXT_TABLES = 8
MAX_CONTEXT_FIELDS_PER_TABLE = 64
MAX_VIEW_NAME = 120
MAX_DASHBOARD_NAME = 120
MAX_DESCRIPTION = 500
MAX_WIDGETS = 12

TITLE_NAMES = ["任务名称", "任务标题", "标题", "名称", "title", "name"]
STATUS_NAMES = ["状态", "任务状态", "status", "state"]
OWNER_NAMES = ["负责人", "执行人", "责任人", "成员", "owner", "assignee", "assigned to"]
PRIORITY_NAMES = ["优先级", "priority"]
DEADLINE_NAMES = ["截止时间", "截止日期", "计划完成时间", "计划完成", "due date", "duedate", "deadline"]
START_NAMES = ["开始时间", "开始日期", "计划开始时间", "start date", "start"]
PROGRESS_NAMES = ["进度", "完成度", "progress"]
TAG_NAMES = ["标签", "任务标签", "tags", "tag"]

VIEW_FILTER_OPERATORS = {
    "text": {"contains", "equals", "is_empty", "is_not_empty"},
    "url": {"contains", "equals", "is_empty", "is_not_empty"},
    "email": {"contains", "equals", "is_empty", "is_not_empty"},
    "phone": {"contains", "equals", "is_empty", "is_not_empty"},
    "number": {"equals", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"},
    "progress": {"equals", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"},
    "rating": {"equals", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"},
    "formula": {"contains", "equals", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"},
    "select": {"is", "is_not", "is_empty", "is_not_empty"},
    "multiSelect": {"is", "is_not", "is_empty", "is_not_empty"},
    "member": {"is", "is_not", "is_empty", "is_not_empty"},
    "date": {"is", "before", "after", "is_empty", "is_not_empty"},
}
DASHBOARD_FILTER_OPERATORS = {
    "text": {"eq", "neq", "contains", "in"},
    "url": {"eq", "neq", "contains", "in"},
    "email": {"eq", "neq", "contains", "in"},
    "phone": {"eq", "neq", "contains", "in"},
    "number": {"eq", "neq", "gt", "gte", "lt", "lte", "in"},
    "progress": {"eq", "neq", "gt", "gte", "lt", "lte", "in"},
    "rating": {"eq", "neq", "gt", "gte", "lt", "lte", "in"},
    "autoNumber": {"eq", "neq", "gt", "gte", "lt", "lte", "in"},
    "select": {"eq", "neq", "in"},
    "multiSelect": {"eq", "neq", "contains", "in"},
    "member": {"eq", "neq", "contains", "in"},
    "date": {"eq", "neq", "before", "after"},
}
CATEGORICAL_TYPES = {"text", "select", "multiSelect", "member", "date", "autoNumber"}
TREND_DIMENSION_TYPES = {"date", "number", "progress", "rating", "autoNumber"}

AI_SYSTEM_PROMPT = """你是 QTable 的“AI 视图与仪表盘设计器”。
你只能输出结构化 Proposal，不能执行任何写操作。

必须遵守：
1. 只能引用输入 schemaContext 中真实存在的 tableId / fieldId / option。
2. View 只能使用 grid / board / gantt / calendar / gallery。
3. Dashboard 只能使用 bar / line / pie / horizontalBar / table / metric / progress。
4. sum/avg/max/min 只能使用数值字段；count 不需要 metric field。
5. line 图必须使用日期或数值型维度；不要给字符串字段做 sum。
6. 不要使用 relation 字段生成筛选、分组、图表维度或指标，以避免大表退化为内存聚合。
7. Dashboard 至少 3 个、最多 8 个有业务意义的组件；组件标题和 purpose 要说明价值。
8. filters / sorts / groups 必须使用提供的字段；View filter operator 要符合 field.allowedViewOperators。
9. Dashboard filter operator 要符合 field.allowedDashboardOperators。
10. 只生成当前用户有权限读取的数据源；权限边界由服务端再次校验。
11. 结果必须可继续人工编辑；不要创建第二套数据模型。
12. 如果 currentProposal 存在，优先按 instruction 做最小修改，不要无故重建全部内容。
"""


class AiVisualDesignError(ValueError):
    pass


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _field_matches(field: TableField, candidates: Iterable[str]) -> bool:
    name = _norm(field.name)
    return any(name == _norm(candidate) or _norm(candidate) in name for candidate in candidates)


def _find_field(
    fields: list[TableField],
    names: Iterable[str],
    types: Optional[set[str]] = None,
) -> Optional[TableField]:
    return next(
        (
            field
            for field in fields
            if (not types or field.type in types) and _field_matches(field, names)
        ),
        None,
    )


def _field_payload(field: TableField) -> dict[str, Any]:
    return {
        "id": str(field.id),
        "name": str(field.name),
        "type": str(field.type),
        "options": _clone(field.options) or [],
        "property": _clone(field.property) or {},
        "allowedViewOperators": sorted(VIEW_FILTER_OPERATORS.get(str(field.type), set())),
        "allowedDashboardOperators": sorted(
            DASHBOARD_FILTER_OPERATORS.get(str(field.type), set())
        ),
    }


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _schema_fingerprint(
    user_id: int,
    workspace_id: str,
    contexts: Mapping[str, dict[str, Any]],
) -> str:
    return _hash(
        {
            "userId": user_id,
            "workspaceId": workspace_id,
            "tables": [
                {
                    "tableId": table_id,
                    "permission": context["permission"],
                    "fields": [
                        _field_payload(field)
                        for field in context["fields"]
                    ],
                }
                for table_id, context in sorted(contexts.items())
            ],
        }
    )


def _dashboard_fingerprint(dashboard: Dashboard, widgets: list[DashboardWidget]) -> str:
    return _hash(
        {
            "dashboardId": dashboard.id,
            "description": dashboard.description,
            "isPublic": bool(dashboard.is_public),
            "updatedAt": dashboard.updated_at,
            "widgets": [
                {
                    "id": widget.id,
                    "type": widget.type,
                    "title": widget.title,
                    "layout": widget.layout,
                    "config": widget.config,
                    "colorScheme": widget.color_scheme,
                    "orderIndex": widget.order_index,
                    "updatedAt": widget.updated_at,
                }
                for widget in sorted(widgets, key=lambda item: (item.order_index, item.id))
            ],
        }
    )


def _option_id_and_label(field: TableField, value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    folded = _norm(raw)
    for item in list(field.options or []):
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        option_id = str(item["id"])
        label = str(item.get("label") or item.get("name") or option_id)
        if folded in {_norm(option_id), _norm(label)}:
            return option_id, label
    raise AiVisualDesignError(
        f"Unknown option '{raw}' for field {field.name}"
    )


def _parse_date(value: Any) -> str:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise AiVisualDesignError("Date filters must use YYYY-MM-DD") from exc


def _numeric(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AiVisualDesignError("Numeric filter value is invalid") from exc
    if not (-1e15 < number < 1e15):
        raise AiVisualDesignError("Numeric filter value is out of range")
    return number


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _layout(index: int, *, metric: bool = False) -> dict[str, int]:
    if metric:
        width, height = 4, 6
    else:
        width, height = 6, 12
    columns = 12 // width
    return {
        "x": (index % columns) * width,
        "y": (index // columns) * height,
        "w": width,
        "h": height,
    }


def _name(value: Any, fallback: str, max_length: int) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    return (cleaned or fallback)[:max_length]


class AiVisualDesignService:
    async def _require_workspace_member(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
    ) -> WorkspaceMember:
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

    async def _table_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        table_id: str,
        required: str = "read",
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
            raise AiVisualDesignError("Source table not found or no access")
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, required):
            raise PermissionError("Source table not found or no access")
        field_result = await db.execute(
            select(TableField)
            .where(TableField.table_id == table_id)
            .order_by(TableField.order_index)
            .limit(MAX_CONTEXT_FIELDS_PER_TABLE + 1)
        )
        fields = list(field_result.scalars().all())
        if len(fields) > MAX_CONTEXT_FIELDS_PER_TABLE:
            fields = fields[:MAX_CONTEXT_FIELDS_PER_TABLE]
        if not fields:
            raise AiVisualDesignError("Source table has no fields")
        return {
            "item": item,
            "permission": permission,
            "fields": fields,
            "fieldById": {str(field.id): field for field in fields},
        }

    async def _readable_contexts(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        preferred_table_id: Optional[str],
        dashboard_id: Optional[str],
    ) -> dict[str, dict[str, Any]]:
        contexts: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        if preferred_table_id:
            ordered_ids.append(preferred_table_id)

        if dashboard_id:
            widget_result = await db.execute(
                select(DashboardWidget).where(
                    DashboardWidget.dashboard_id == dashboard_id
                )
            )
            for widget in widget_result.scalars().all():
                config = widget.config if isinstance(widget.config, dict) else {}
                table_id = config.get("tableId")
                if isinstance(table_id, str) and table_id and table_id not in ordered_ids:
                    ordered_ids.append(table_id)

        all_result = await db.execute(
            select(WorkspaceItem.id).where(
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "table",
            ).order_by(WorkspaceItem.order_index, WorkspaceItem.id)
        )
        for table_id in all_result.scalars().all():
            table_id = str(table_id)
            if table_id not in ordered_ids:
                ordered_ids.append(table_id)

        for table_id in ordered_ids:
            if len(contexts) >= MAX_CONTEXT_TABLES:
                break
            try:
                context = await self._table_context(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    table_id=table_id,
                    required="read",
                )
            except (PermissionError, AiVisualDesignError):
                if table_id == preferred_table_id:
                    raise
                continue
            contexts[table_id] = context
        if not contexts:
            raise AiVisualDesignError("No readable tables are available for visual design")
        return contexts

    async def _workspace_member_names(
        self,
        db: AsyncSession,
        workspace_id: str,
    ) -> set[str]:
        result = await db.execute(
            select(User.name)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == workspace_id)
        )
        return {
            str(name).strip()
            for name in result.scalars().all()
            if isinstance(name, str) and name.strip()
        }

    async def _existing_dashboard(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        dashboard_id: str,
    ) -> tuple[Dashboard, WorkspaceItem, list[DashboardWidget], str]:
        item_result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == dashboard_id,
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "dashboard",
            )
        )
        item = item_result.scalars().first()
        if item is None:
            raise AiVisualDesignError("Dashboard not found or no access")
        permission = await get_effective_permission_for_item(db, user_id, dashboard_id)
        if not permission_allows(permission, "read"):
            raise PermissionError("Dashboard not found or no access")
        dashboard_result = await db.execute(
            select(Dashboard).where(Dashboard.id == dashboard_id)
        )
        dashboard = dashboard_result.scalars().first()
        if dashboard is None:
            raise AiVisualDesignError("Dashboard metadata is missing")
        widget_result = await db.execute(
            select(DashboardWidget)
            .where(DashboardWidget.dashboard_id == dashboard_id)
            .order_by(DashboardWidget.order_index, DashboardWidget.id)
        )
        widgets = list(widget_result.scalars().all())
        return dashboard, item, widgets, permission

    def _existing_dashboard_proposal(
        self,
        dashboard: Dashboard,
        item: WorkspaceItem,
        widgets: list[DashboardWidget],
    ) -> Optional[AiVisualDesignProposal]:
        payload = {
            "kind": "dashboard",
            "rationale": "基于当前仪表盘进行自然语言调整。",
            "dashboard": {
                "name": item.name,
                "description": dashboard.description or "",
                "widgets": [
                    {
                        "key": f"existing_{widget.id}",
                        "type": widget.type,
                        "title": widget.title or "",
                        "tableId": (widget.config or {}).get("tableId"),
                        "dimensionFieldId": (widget.config or {}).get("dimensionFieldId"),
                        "metric": (widget.config or {}).get("metric") or {"aggregation": "count"},
                        "filters": (widget.config or {}).get("filters") or [],
                        "sort": (widget.config or {}).get("sort") or {"by": "value", "order": "desc"},
                        "limit": (widget.config or {}).get("limit") or 50,
                        "layout": widget.layout or {},
                        "targetValue": (widget.config or {}).get("targetValue"),
                        "purpose": "保留当前组件并按自然语言指令调整。",
                    }
                    for widget in widgets
                    if isinstance(widget.config, dict)
                    and isinstance(widget.config.get("tableId"), str)
                ],
            },
        }
        # The typed DashboardDesign requires at least three widgets. An older
        # or still-being-configured dashboard may legitimately contain fewer;
        # in that case pass only its metadata to the AI and let the safe
        # dashboard baseline supply the minimum meaningful component set.
        if len(payload["dashboard"]["widgets"]) < 3:
            return None
        return AiVisualDesignProposal.model_validate(payload)

    def _semantic(self, fields: list[TableField]) -> dict[str, Optional[TableField]]:
        return {
            "title": _find_field(fields, TITLE_NAMES, {"text"}),
            "status": _find_field(fields, STATUS_NAMES, {"select", "text"}),
            "owner": _find_field(fields, OWNER_NAMES, {"member"}),
            "priority": _find_field(fields, PRIORITY_NAMES, {"select", "text"}),
            "deadline": _find_field(fields, DEADLINE_NAMES, {"date"}),
            "start": _find_field(fields, START_NAMES, {"date"}),
            "progress": _find_field(fields, PROGRESS_NAMES, {"progress", "number"}),
            "tags": _find_field(fields, TAG_NAMES, {"text", "multiSelect"}),
        }

    def _high_option(self, field: TableField) -> Optional[tuple[str, str]]:
        preferred = {
            _norm("高"),
            _norm("高优先级"),
            _norm("high"),
            _norm("urgent"),
            _norm("紧急"),
            _norm("p0"),
        }
        first: Optional[tuple[str, str]] = None
        for item in list(field.options or []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            option_id = str(item["id"])
            label = str(item.get("label") or item.get("name") or option_id)
            first = first or (option_id, label)
            if _norm(option_id) in preferred or _norm(label) in preferred:
                return option_id, label
        return first

    def _done_option(self, field: TableField) -> Optional[tuple[str, str]]:
        done = {_norm(x) for x in ["已完成", "完成", "done", "completed", "closed"]}
        for item in list(field.options or []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            option_id = str(item["id"])
            label = str(item.get("label") or item.get("name") or option_id)
            if _norm(option_id) in done or _norm(label) in done:
                return option_id, label
        return None

    def _fallback_view(
        self,
        prompt: str,
        table_id: str,
        context: dict[str, Any],
        user_name: str,
    ) -> AiVisualDesignProposal:
        fields: list[TableField] = context["fields"]
        semantic = self._semantic(fields)
        combined = _norm(prompt)

        view_type = "grid"
        if any(token in combined for token in [_norm("甘特"), _norm("gantt")]):
            view_type = "gantt"
        elif any(token in combined for token in [_norm("日历"), _norm("calendar")]):
            view_type = "calendar"
        elif any(token in combined for token in [_norm("画廊"), _norm("gallery")]):
            view_type = "gallery"
        elif any(token in combined for token in [_norm("看板"), _norm("kanban")]):
            view_type = "board"

        filters: list[dict[str, Any]] = []
        sorts: list[dict[str, Any]] = []
        group = {"fieldId": None, "order": "asc"}

        if any(token in combined for token in [_norm("高优先级"), _norm("high priority"), _norm("紧急任务")]):
            field = semantic["priority"]
            if field is not None:
                if field.type == "select":
                    option = self._high_option(field)
                    if option:
                        filters.append(
                            {
                                "fieldId": field.id,
                                "operator": "is",
                                "value": option[1],
                                "logic": "where",
                            }
                        )
                else:
                    filters.append(
                        {
                            "fieldId": field.id,
                            "operator": "contains",
                            "value": "高",
                            "logic": "where",
                        }
                    )

        if any(token in combined for token in [_norm("逾期"), _norm("overdue")]):
            deadline = semantic["deadline"]
            status = semantic["status"]
            if deadline:
                filters.append(
                    {
                        "fieldId": deadline.id,
                        "operator": "before",
                        "value": _today().isoformat(),
                        "logic": "and" if filters else "where",
                    }
                )
                sorts.append({"fieldId": deadline.id, "order": "asc"})
            if status and status.type == "select":
                done = self._done_option(status)
                if done:
                    filters.append(
                        {
                            "fieldId": status.id,
                            "operator": "is_not",
                            "value": done[1],
                            "logic": "and",
                        }
                    )

        if any(token in combined for token in [_norm("我的"), _norm("my tasks")]):
            owner = semantic["owner"]
            if owner and user_name:
                filters.append(
                    {
                        "fieldId": owner.id,
                        "operator": "is",
                        "value": user_name,
                        "logic": "and" if filters else "where",
                    }
                )

        if any(token in combined for token in [_norm("负责人分组"), _norm("按负责人"), _norm("by owner")]):
            if semantic["owner"]:
                group = {"fieldId": semantic["owner"].id, "order": "asc"}
        elif any(token in combined for token in [_norm("状态分组"), _norm("按状态"), _norm("by status")]):
            if semantic["status"]:
                group = {"fieldId": semantic["status"].id, "order": "asc"}

        if view_type == "board" and not group["fieldId"]:
            candidate = semantic["status"] or semantic["owner"]
            if candidate:
                group = {"fieldId": candidate.id, "order": "asc"}

        if not sorts and semantic["deadline"]:
            sorts.append({"fieldId": semantic["deadline"].id, "order": "asc"})

        config: dict[str, Any] = {}
        if view_type == "gantt":
            config["ganttConfig"] = {
                "startFieldId": (semantic["start"] or semantic["deadline"]).id
                if (semantic["start"] or semantic["deadline"])
                else None,
                "endFieldId": semantic["deadline"].id if semantic["deadline"] else None,
                "progressFieldId": semantic["progress"].id if semantic["progress"] else None,
            }
        elif view_type == "calendar":
            config["calendarConfig"] = {
                "startFieldId": (semantic["start"] or semantic["deadline"]).id
                if (semantic["start"] or semantic["deadline"])
                else None,
                "endFieldId": semantic["deadline"].id if semantic["deadline"] else None,
                "weekStartsOn": 1,
            }
        elif view_type == "gallery":
            config["galleryConfig"] = {
                "coverFieldId": None,
                "titleFieldId": semantic["title"].id if semantic["title"] else None,
                "cardSize": "medium",
                "imageFit": "cover",
                "showFieldNames": True,
            }

        return AiVisualDesignProposal.model_validate(
            {
                "kind": "view",
                "rationale": "根据自然语言意图生成命名视图；确认前不会修改表格。",
                "view": {
                    "tableId": table_id,
                    "name": _name(prompt, "AI 视图", MAX_VIEW_NAME),
                    "type": view_type,
                    "filters": filters,
                    "sorts": sorts,
                    "groupConfig": group,
                    "visibleFieldIds": [],
                    **config,
                },
            }
        )

    def _dashboard_baseline(
        self,
        prompt: str,
        preferred_table_id: str,
        context: dict[str, Any],
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> AiVisualDesignProposal:
        fields: list[TableField] = context["fields"]
        semantic = self._semantic(fields)
        widgets: list[dict[str, Any]] = []

        widgets.append(
            {
                "key": "total_tasks",
                "type": "metric",
                "title": "任务总数",
                "tableId": preferred_table_id,
                "metric": {"aggregation": "count"},
                "layout": _layout(0, metric=True),
                "purpose": "快速了解当前项目任务规模。",
            }
        )

        if semantic["status"]:
            widgets.append(
                {
                    "key": "status_distribution",
                    "type": "pie",
                    "title": "任务状态分布",
                    "tableId": preferred_table_id,
                    "dimensionFieldId": semantic["status"].id,
                    "metric": {"aggregation": "count"},
                    "layout": _layout(len(widgets)),
                    "purpose": "识别任务在待处理、进行中、已完成等状态上的分布。",
                }
            )

        if semantic["owner"]:
            widgets.append(
                {
                    "key": "owner_load",
                    "type": "horizontalBar",
                    "title": "负责人任务负载",
                    "tableId": preferred_table_id,
                    "dimensionFieldId": semantic["owner"].id,
                    "metric": {"aggregation": "count"},
                    "layout": _layout(len(widgets)),
                    "purpose": "按负责人统计当前任务数量，辅助识别负载不均衡。",
                }
            )

        if semantic["deadline"]:
            overdue_filters = [
                {
                    "fieldId": semantic["deadline"].id,
                    "operator": "before",
                    "value": _today().isoformat(),
                }
            ]
            if semantic["status"] and semantic["status"].type == "select":
                done = self._done_option(semantic["status"])
                if done:
                    overdue_filters.append(
                        {
                            "fieldId": semantic["status"].id,
                            "operator": "neq",
                            "value": done[0],
                        }
                    )
            widgets.append(
                {
                    "key": "overdue_tasks",
                    "type": "metric",
                    "title": "逾期未完成任务",
                    "tableId": preferred_table_id,
                    "metric": {"aggregation": "count"},
                    "filters": overdue_filters,
                    "layout": _layout(len(widgets), metric=True),
                    "purpose": "直接暴露已经超过截止日期且尚未完成的任务数量。",
                }
            )

        if semantic["priority"]:
            widgets.append(
                {
                    "key": "priority_distribution",
                    "type": "bar",
                    "title": "任务优先级分布",
                    "tableId": preferred_table_id,
                    "dimensionFieldId": semantic["priority"].id,
                    "metric": {"aggregation": "count"},
                    "layout": _layout(len(widgets)),
                    "purpose": "查看任务优先级结构，辅助识别高风险工作占比。",
                }
            )

        # Guarantee at least three meaningful components even on simple tables.
        fallback_dimensions = [
            field
            for field in fields
            if field.type in CATEGORICAL_TYPES and field.type != "date"
        ]
        while len(widgets) < 3 and fallback_dimensions:
            field = fallback_dimensions.pop(0)
            widgets.append(
                {
                    "key": f"distribution_{field.id}",
                    "type": "bar",
                    "title": f"{field.name}分布",
                    "tableId": preferred_table_id,
                    "dimensionFieldId": field.id,
                    "metric": {"aggregation": "count"},
                    "layout": _layout(len(widgets)),
                    "purpose": f"按{field.name}统计记录分布。",
                }
            )

        if len(widgets) < 3:
            # Repeated metrics with distinct purposes are preferable to an
            # invalid chart over arbitrary string fields.
            widgets.append(
                {
                    "key": "records_metric_secondary",
                    "type": "metric",
                    "title": "当前记录数",
                    "tableId": preferred_table_id,
                    "metric": {"aggregation": "count"},
                    "layout": _layout(len(widgets), metric=True),
                    "purpose": "作为项目规模的基础指标。",
                }
            )
        if len(widgets) < 3:
            widgets.append(
                {
                    "key": "records_progress",
                    "type": "progress",
                    "title": "任务基准进度",
                    "tableId": preferred_table_id,
                    "metric": {"aggregation": "count"},
                    "targetValue": 100,
                    "layout": _layout(len(widgets), metric=True),
                    "purpose": "以 100 条任务为可编辑目标基准，用户可继续手工调整。",
                }
            )

        return AiVisualDesignProposal.model_validate(
            {
                "kind": "dashboard",
                "rationale": "使用现有 Dashboard 模型生成项目数据视角；组件数据继续由服务端聚合与缓存执行。",
                "dashboard": {
                    "name": _name(name or prompt, "AI 项目仪表盘", MAX_DASHBOARD_NAME),
                    "description": _name(
                        description or "根据自然语言生成的项目分析仪表盘",
                        "",
                        MAX_DESCRIPTION,
                    ),
                    "widgets": widgets[:8],
                },
            }
        )

    def _refine_fallback(
        self,
        proposal: AiVisualDesignProposal,
        instruction: str,
        contexts: Mapping[str, dict[str, Any]],
        user_name: str,
    ) -> AiVisualDesignProposal:
        data = proposal.model_dump(mode="json", by_alias=True)
        combined = _norm(instruction)

        if proposal.kind == "view" and proposal.view:
            table_id = proposal.view.table_id
            context = contexts.get(table_id)
            if not context:
                return proposal
            semantic = self._semantic(context["fields"])
            view = data["view"]
            if any(token in combined for token in [_norm("只看高优先级"), _norm("high priority")]):
                field = semantic["priority"]
                if field:
                    filters = [
                        item
                        for item in view.get("filters", [])
                        if item.get("fieldId") != field.id
                    ]
                    if field.type == "select":
                        option = self._high_option(field)
                        if option:
                            filters.append(
                                {
                                    "fieldId": field.id,
                                    "operator": "is",
                                    "value": option[1],
                                    "logic": "and" if filters else "where",
                                }
                            )
                    view["filters"] = filters
            if any(token in combined for token in [_norm("按负责人分组"), _norm("by owner")]) and semantic["owner"]:
                view["groupConfig"] = {
                    "fieldId": semantic["owner"].id,
                    "order": "asc",
                }
            if any(token in combined for token in [_norm("看板"), _norm("kanban")]):
                view["type"] = "board"
                if not view.get("groupConfig", {}).get("fieldId"):
                    candidate = semantic["status"] or semantic["owner"]
                    if candidate:
                        view["groupConfig"] = {"fieldId": candidate.id, "order": "asc"}
            return AiVisualDesignProposal.model_validate(data)

        if proposal.kind == "dashboard" and proposal.dashboard:
            widgets = data["dashboard"]["widgets"]
            requested_type: Optional[str] = None
            if any(token in combined for token in [_norm("折线"), _norm("line chart")]):
                requested_type = "line"
            elif any(token in combined for token in [_norm("条形"), _norm("horizontal bar")]):
                requested_type = "horizontalBar"
            elif any(token in combined for token in [_norm("饼图"), _norm("pie")]):
                requested_type = "pie"
            elif any(token in combined for token in [_norm("柱状"), _norm("bar chart")]):
                requested_type = "bar"

            if requested_type:
                for widget in widgets:
                    if widget.get("type") in {"bar", "line", "pie", "horizontalBar", "table"}:
                        table_id = widget.get("tableId")
                        context = contexts.get(str(table_id))
                        if not context:
                            continue
                        if requested_type == "line":
                            trend = _find_field(
                                context["fields"],
                                [*DEADLINE_NAMES, *START_NAMES],
                                {"date"},
                            ) or next(
                                (
                                    field
                                    for field in context["fields"]
                                    if field.type in TREND_DIMENSION_TYPES
                                ),
                                None,
                            )
                            if trend:
                                widget["dimensionFieldId"] = trend.id
                                widget["type"] = "line"
                                widget["title"] = f"{trend.name}趋势"
                                break
                        else:
                            widget["type"] = requested_type
                            break

            match = re.search(r"(\d{1,4})\s*天", instruction)
            if match and any(token in combined for token in [_norm("最近"), _norm("近")]):
                days = max(1, min(3650, int(match.group(1))))
                start = (_today() - timedelta(days=days - 1)).isoformat()
                for widget in widgets:
                    context = contexts.get(str(widget.get("tableId")))
                    if not context:
                        continue
                    date_field = _find_field(
                        context["fields"],
                        [*DEADLINE_NAMES, *START_NAMES],
                        {"date"},
                    ) or next((field for field in context["fields"] if field.type == "date"), None)
                    if not date_field:
                        continue
                    existing = [
                        item
                        for item in widget.get("filters", [])
                        if item.get("fieldId") != date_field.id
                    ]
                    existing.append(
                        {
                            "fieldId": date_field.id,
                            "operator": "after",
                            "value": start,
                        }
                    )
                    widget["filters"] = existing
            return AiVisualDesignProposal.model_validate(data)

        return proposal

    async def _ai_proposal(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        prompt: str,
        target_type: str,
        contexts: Mapping[str, dict[str, Any]],
        current_proposal: Optional[AiVisualDesignProposal],
        instruction: Optional[str],
        model_override: Optional[str],
        existing_dashboard: Optional[dict[str, Any]],
    ) -> tuple[AiVisualDesignProposal, str, Optional[str]]:
        model, provider, model_name = await project_steward_service._provider(
            db,
            user_id=user_id,
            model_override=model_override,
        )
        context_payload = {
            "targetType": target_type,
            "prompt": prompt,
            "instruction": instruction,
            "currentProposal": (
                current_proposal.model_dump(mode="json", by_alias=True)
                if current_proposal
                else None
            ),
            "existingDashboard": existing_dashboard,
            "schemaContext": [
                {
                    "tableId": table_id,
                    "tableName": context["item"].name,
                    "fields": [_field_payload(field) for field in context["fields"]],
                }
                for table_id, context in contexts.items()
            ],
        }
        agent = Agent(
            model=model,
            deps_type=dict,
            output_type=AiVisualDesignProposal,
            system_prompt=AI_SYSTEM_PROMPT,
            model_settings=OpenAIChatModelSettings(temperature=0.1),
        )
        result = await agent.run(
            json.dumps(context_payload, ensure_ascii=False, default=str),
            deps={},
        )
        return result.output, provider, model_name

    def _normalize_view_filter(
        self,
        *,
        field: TableField,
        raw: Mapping[str, Any],
        member_names: set[str],
        index: int,
    ) -> dict[str, Any]:
        operator = str(raw.get("operator") or "").strip()
        allowed = VIEW_FILTER_OPERATORS.get(field.type, set())
        if operator not in allowed:
            raise AiVisualDesignError(
                f"Unsupported filter operator '{operator}' for field {field.name}"
            )
        logic = str(raw.get("logic") or ("where" if index == 0 else "and")).lower()
        if logic not in {"where", "and", "or"}:
            raise AiVisualDesignError("View filter logic must be where/and/or")
        if index == 0:
            logic = "where"
        value = raw.get("value")
        if operator not in {"is_empty", "is_not_empty"}:
            if field.type in {"select", "multiSelect"}:
                _, label = _option_id_and_label(field, value)
                value = label
            elif field.type == "date":
                value = _parse_date(value)
            elif field.type in {"number", "progress", "rating"} and operator in {"gt", "lt", "gte", "lte"}:
                value = _numeric(value)
            elif field.type == "member":
                name = str(value or "").strip()
                if name not in member_names:
                    raise AiVisualDesignError(
                        f"Member filter references unavailable member: {name}"
                    )
                value = name
        return {
            "id": f"aif_{uuid.uuid4().hex[:12]}",
            "fieldId": str(field.id),
            "operator": operator,
            "value": value,
            "logic": logic,
        }

    async def _normalize_view(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        design: ViewDesign,
        contexts: Mapping[str, dict[str, Any]],
    ) -> dict[str, Any]:
        context = contexts.get(design.table_id)
        if context is None:
            raise AiVisualDesignError("View references a table outside the preview scope")
        if not permission_allows(context["permission"], "edit"):
            raise PermissionError("No edit permission for the target table")
        fields: list[TableField] = context["fields"]
        field_by_id = context["fieldById"]
        member_names = await self._workspace_member_names(db, workspace_id)

        filters: list[dict[str, Any]] = []
        for index, spec in enumerate(design.filters):
            field = field_by_id.get(spec.field_id)
            if field is None:
                raise AiVisualDesignError(
                    f"View filter references missing field: {spec.field_id}"
                )
            if field.type == "relation":
                raise AiVisualDesignError(
                    "AI-generated views do not filter relation fields to protect large-table performance"
                )
            filters.append(
                self._normalize_view_filter(
                    field=field,
                    raw=spec.model_dump(mode="json", by_alias=True),
                    member_names=member_names,
                    index=index,
                )
            )

        sorts: list[dict[str, Any]] = []
        seen_sorts: set[str] = set()
        for spec in design.sorts:
            field = field_by_id.get(spec.field_id)
            if field is None:
                raise AiVisualDesignError(
                    f"View sort references missing field: {spec.field_id}"
                )
            if field.type == "relation":
                raise AiVisualDesignError(
                    "AI-generated views do not sort relation fields to protect large-table performance"
                )
            if field.id in seen_sorts:
                continue
            seen_sorts.add(field.id)
            sorts.append({"fieldId": field.id, "order": spec.order})

        group_field_id = design.group_config.field_id
        if group_field_id:
            group_field = field_by_id.get(group_field_id)
            if group_field is None:
                raise AiVisualDesignError("View group references a missing field")
            if group_field.type == "relation":
                raise AiVisualDesignError(
                    "AI-generated views do not group relation fields to protect large-table performance"
                )
        if design.type == "board":
            if not group_field_id:
                raise AiVisualDesignError("Kanban views require a group field")
            group_field = field_by_id[group_field_id]
            if group_field.type != "select":
                raise AiVisualDesignError(
                    "Kanban grouping must use a select field to match the existing board editor"
                )

        visible_ids = list(dict.fromkeys(design.visible_field_ids))
        for field_id in visible_ids:
            if field_id not in field_by_id:
                raise AiVisualDesignError(
                    f"Visible fields reference missing field: {field_id}"
                )
        hidden = (
            [field.id for field in fields if field.id not in set(visible_ids)]
            if visible_ids
            else []
        )

        config = _build_default_view_config(design.type)
        config["filters"] = filters
        config["sorts"] = sorts
        config["groupConfig"] = {
            "fieldId": group_field_id,
            "order": design.group_config.order,
        }
        config["hiddenFieldIds"] = hidden

        if design.type == "gantt":
            raw = dict(design.gantt_config or {})
            start_id = str(raw.get("startFieldId") or "")
            end_id = str(raw.get("endFieldId") or "")
            progress_id = str(raw.get("progressFieldId") or "")
            if not start_id or field_by_id.get(start_id, None) is None or field_by_id[start_id].type != "date":
                raise AiVisualDesignError("Gantt requires a valid date startFieldId")
            if not end_id or field_by_id.get(end_id, None) is None or field_by_id[end_id].type != "date":
                raise AiVisualDesignError("Gantt requires a valid date endFieldId")
            if progress_id:
                progress_field = field_by_id.get(progress_id)
                if progress_field is None or progress_field.type not in {"progress", "number"}:
                    raise AiVisualDesignError("Gantt progressFieldId must be numeric/progress")
            config["ganttConfig"] = {
                "startFieldId": start_id,
                "endFieldId": end_id,
                "progressFieldId": progress_id or None,
            }
        elif design.type == "calendar":
            raw = dict(design.calendar_config or {})
            start_id = str(raw.get("startFieldId") or "")
            end_id = str(raw.get("endFieldId") or "")
            if not start_id or field_by_id.get(start_id, None) is None or field_by_id[start_id].type != "date":
                raise AiVisualDesignError("Calendar requires a valid date startFieldId")
            if end_id:
                end_field = field_by_id.get(end_id)
                if end_field is None or end_field.type != "date":
                    raise AiVisualDesignError("Calendar endFieldId must be a date field")
            config["calendarConfig"] = {
                "startFieldId": start_id,
                "endFieldId": end_id or None,
                "weekStartsOn": 1,
            }
        elif design.type == "gallery":
            raw = dict(design.gallery_config or {})
            title_id = str(raw.get("titleFieldId") or "")
            cover_id = str(raw.get("coverFieldId") or "")
            if title_id and title_id not in field_by_id:
                raise AiVisualDesignError("Gallery titleFieldId is invalid")
            if cover_id and cover_id not in field_by_id:
                raise AiVisualDesignError("Gallery coverFieldId is invalid")
            config["galleryConfig"] = {
                "coverFieldId": cover_id or None,
                "titleFieldId": title_id or None,
                "cardSize": str(raw.get("cardSize") or "medium"),
                "imageFit": str(raw.get("imageFit") or "cover"),
                "showFieldNames": bool(raw.get("showFieldNames", True)),
            }

        return {
            "tableId": design.table_id,
            "name": _name(design.name, "AI 视图", MAX_VIEW_NAME),
            "type": design.type,
            "config": config,
            "explanation": {
                "filters": [
                    {
                        "fieldName": field_by_id[item["fieldId"]].name,
                        "operator": item["operator"],
                        "value": item["value"],
                    }
                    for item in filters
                ],
                "sorts": [
                    {
                        "fieldName": field_by_id[item["fieldId"]].name,
                        "order": item["order"],
                    }
                    for item in sorts
                ],
                "groupFieldName": (
                    field_by_id[group_field_id].name if group_field_id else None
                ),
                "visibleFieldCount": len(fields) - len(hidden),
            },
        }

    def _normalize_dashboard_filter(
        self,
        field: TableField,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        if field.type == "relation":
            raise AiVisualDesignError(
                "AI-generated dashboards do not use relation filters to preserve SQL aggregation performance"
            )
        operator = str(raw.get("operator") or "").lower()
        allowed = DASHBOARD_FILTER_OPERATORS.get(field.type, set())
        if operator not in allowed:
            raise AiVisualDesignError(
                f"Unsupported dashboard filter operator '{operator}' for {field.name}"
            )
        value = raw.get("value")
        if operator in {"before", "after"}:
            value = _parse_date(value)
        elif field.type == "date" and operator in {"eq", "neq"}:
            value = _parse_date(value)
        elif field.type == "select":
            if operator == "in":
                if not isinstance(value, list):
                    raise AiVisualDesignError("Select in-filter requires a list")
                value = [_option_id_and_label(field, item)[0] for item in value]
            else:
                value = _option_id_and_label(field, value)[0]
        elif field.type in NUMERIC_FIELD_TYPES and operator in {"gt", "gte", "lt", "lte"}:
            value = _numeric(value)
        elif operator == "in" and not isinstance(value, list):
            raise AiVisualDesignError("Dashboard in-filter requires a list")
        return {
            "fieldId": str(field.id),
            "operator": operator,
            "value": value,
        }

    def _normalize_widget(
        self,
        design: DashboardWidgetDesign,
        contexts: Mapping[str, dict[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        context = contexts.get(design.table_id)
        if context is None:
            raise AiVisualDesignError("Dashboard widget references an unavailable table")
        field_by_id = context["fieldById"]

        dimension_id = design.dimension_field_id
        dimension: Optional[TableField] = None
        if dimension_id:
            dimension = field_by_id.get(dimension_id)
            if dimension is None:
                raise AiVisualDesignError(
                    f"Dashboard dimension references missing field: {dimension_id}"
                )
            if dimension.type == "relation":
                raise AiVisualDesignError(
                    "AI-generated dashboards avoid relation dimensions to preserve SQL aggregation performance"
                )

        if design.type in {"bar", "line", "pie", "horizontalBar", "table"} and dimension is None:
            raise AiVisualDesignError(f"{design.type} widget requires a dimension")
        if design.type == "line" and dimension and dimension.type not in TREND_DIMENSION_TYPES:
            raise AiVisualDesignError(
                f"Line chart requires a date/numeric dimension, not {dimension.type}"
            )
        if design.type == "pie" and dimension and dimension.type not in CATEGORICAL_TYPES:
            raise AiVisualDesignError(
                "Pie chart requires a categorical dimension"
            )

        metric = design.metric
        metric_field: Optional[TableField] = None
        if metric.aggregation != "count":
            if not metric.field_id:
                raise AiVisualDesignError(
                    f"{metric.aggregation} requires a numeric metric field"
                )
            metric_field = field_by_id.get(metric.field_id)
            if metric_field is None:
                raise AiVisualDesignError("Dashboard metric references a missing field")
            if metric_field.type not in NUMERIC_FIELD_TYPES:
                raise AiVisualDesignError(
                    f"{metric.aggregation} cannot be used with {metric_field.type}"
                )

        filters: list[dict[str, Any]] = []
        for raw in design.filters:
            if not isinstance(raw, dict):
                raise AiVisualDesignError("Dashboard filter must be an object")
            field_id = str(raw.get("fieldId") or "")
            field = field_by_id.get(field_id)
            if field is None:
                raise AiVisualDesignError(
                    f"Dashboard filter references missing field: {field_id}"
                )
            filters.append(self._normalize_dashboard_filter(field, raw))

        sort_by = str((design.sort or {}).get("by") or "value").lower()
        sort_order = str((design.sort or {}).get("order") or "desc").lower()
        if sort_by not in {"value", "dimension"}:
            raise AiVisualDesignError("Dashboard widget sort must use value or dimension")
        if sort_order not in {"asc", "desc"}:
            raise AiVisualDesignError("Dashboard widget sort order must be asc or desc")
        if not dimension and sort_by == "dimension":
            sort_by = "value"

        config: dict[str, Any] = {
            "tableId": design.table_id,
            "dimensionFieldId": dimension.id if dimension else None,
            "metric": {
                "aggregation": metric.aggregation,
                "fieldId": metric_field.id if metric_field else None,
            },
            "filters": filters,
            "sort": {"by": sort_by, "order": sort_order},
            "limit": int(design.limit),
        }
        if design.type == "progress":
            if design.target_value is None or float(design.target_value) <= 0:
                raise AiVisualDesignError("Progress widget requires targetValue > 0")
            config["targetValue"] = float(design.target_value)

        metric_like = design.type in {"metric", "progress"}
        layout = dict(design.layout or {})
        if not layout:
            layout = _layout(index, metric=metric_like)
        return {
            "key": _name(design.key, f"widget_{index+1}", 80),
            "type": design.type,
            "title": _name(design.title, f"组件 {index+1}", 255),
            "colorScheme": {"paletteId": "default"},
            "layout": layout,
            "config": config,
            "purpose": _name(
                design.purpose,
                "根据当前数据源提供项目分析视角。",
                300,
            ),
            "source": {
                "tableId": design.table_id,
                "tableName": context["item"].name,
                "dimensionFieldName": dimension.name if dimension else None,
                "metricFieldName": metric_field.name if metric_field else None,
                "aggregation": metric.aggregation,
            },
        }

    async def _normalize_proposal(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        proposal: AiVisualDesignProposal,
        contexts: Mapping[str, dict[str, Any]],
        dashboard_id: Optional[str],
    ) -> dict[str, Any]:
        if proposal.kind == "view":
            if proposal.view is None:
                raise AiVisualDesignError("View proposal is missing")
            normalized_view = await self._normalize_view(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
                design=proposal.view,
                contexts=contexts,
            )
            return {
                "kind": "view",
                "rationale": _name(
                    proposal.rationale,
                    "根据自然语言生成命名视图。",
                    1000,
                ),
                "view": normalized_view,
                "dashboard": None,
            }

        if proposal.dashboard is None:
            raise AiVisualDesignError("Dashboard proposal is missing")
        if not 3 <= len(proposal.dashboard.widgets) <= MAX_WIDGETS:
            raise AiVisualDesignError(
                "Dashboard proposals require between 3 and 12 widgets"
            )

        widgets = [
            self._normalize_widget(widget, contexts, index)
            for index, widget in enumerate(proposal.dashboard.widgets)
        ]
        keys = [widget["key"] for widget in widgets]
        if len(keys) != len(set(keys)):
            raise AiVisualDesignError("Dashboard widget keys must be unique")

        return {
            "kind": "dashboard",
            "rationale": _name(
                proposal.rationale,
                "根据自然语言生成项目仪表盘。",
                1000,
            ),
            "view": None,
            "dashboard": {
                "name": _name(
                    proposal.dashboard.name,
                    "AI 项目仪表盘",
                    MAX_DASHBOARD_NAME,
                ),
                "description": _name(
                    proposal.dashboard.description,
                    "",
                    MAX_DESCRIPTION,
                ),
                "widgets": widgets,
                "mode": "replace" if dashboard_id else "create",
            },
        }

    def _normalized_to_model(
        self,
        normalized: dict[str, Any],
        contexts: Optional[Mapping[str, dict[str, Any]]] = None,
    ) -> AiVisualDesignProposal:
        if normalized["kind"] == "view":
            view = normalized["view"]
            config = view["config"]
            context = (contexts or {}).get(str(view["tableId"]))
            hidden = set(str(item) for item in list(config.get("hiddenFieldIds") or []))
            visible = (
                [
                    str(field.id)
                    for field in context["fields"]
                    if str(field.id) not in hidden
                ]
                if context
                else []
            )
            # Store normalized runtime config in a Pydantic proposal shape so
            # the client can edit/refine it without learning server internals.
            return AiVisualDesignProposal.model_validate(
                {
                    "kind": "view",
                    "rationale": normalized["rationale"],
                    "view": {
                        "tableId": view["tableId"],
                        "name": view["name"],
                        "type": view["type"],
                        "filters": [
                            {
                                "fieldId": item["fieldId"],
                                "operator": item["operator"],
                                "value": item.get("value"),
                                "logic": item.get("logic", "and"),
                            }
                            for item in config.get("filters", [])
                        ],
                        "sorts": config.get("sorts", []),
                        "groupConfig": config.get("groupConfig") or {},
                        "visibleFieldIds": visible,
                        "ganttConfig": config.get("ganttConfig"),
                        "calendarConfig": config.get("calendarConfig"),
                        "galleryConfig": config.get("galleryConfig"),
                    },
                }
            )
        dashboard = normalized["dashboard"]
        return AiVisualDesignProposal.model_validate(
            {
                "kind": "dashboard",
                "rationale": normalized["rationale"],
                "dashboard": {
                    "name": dashboard["name"],
                    "description": dashboard["description"],
                    "widgets": [
                        {
                            "key": item["key"],
                            "type": item["type"],
                            "title": item["title"],
                            "tableId": item["config"]["tableId"],
                            "dimensionFieldId": item["config"].get("dimensionFieldId"),
                            "metric": item["config"].get("metric") or {"aggregation": "count"},
                            "filters": item["config"].get("filters") or [],
                            "sort": item["config"].get("sort") or {"by": "value", "order": "desc"},
                            "limit": item["config"].get("limit") or 50,
                            "layout": item["layout"],
                            "targetValue": item["config"].get("targetValue"),
                            "purpose": item["purpose"],
                        }
                        for item in dashboard["widgets"]
                    ],
                },
            }
        )

    def _choose_target(self, request: AiVisualDesignPreviewRequest) -> str:
        if request.target_type in {"view", "dashboard"}:
            return request.target_type
        combined = _norm(
            " ".join(
                part
                for part in [request.prompt, request.instruction or ""]
                if part
            )
        )
        return (
            "dashboard"
            if any(
                token in combined
                for token in [_norm("仪表盘"), _norm("dashboard"), _norm("驾驶舱")]
            )
            else "view"
        )

    async def _resolve_parent(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: str,
        requested_parent_id: Optional[str],
        table_id: Optional[str],
    ) -> str:
        parent_id = requested_parent_id
        if not parent_id and table_id:
            result = await db.execute(
                select(WorkspaceItem.parent_id).where(
                    WorkspaceItem.id == table_id,
                    WorkspaceItem.workspace_id == workspace_id,
                    WorkspaceItem.type == "table",
                )
            )
            parent_id = result.scalar()
        if not parent_id:
            result = await db.execute(
                select(WorkspaceItem.id)
                .where(
                    WorkspaceItem.workspace_id == workspace_id,
                    WorkspaceItem.type == "folder",
                    WorkspaceItem.parent_id.is_(None),
                )
                .order_by(WorkspaceItem.order_index, WorkspaceItem.id)
                .limit(1)
            )
            parent_id = result.scalar()
        if not parent_id:
            raise AiVisualDesignError("A parent folder is required to create a dashboard")
        item_result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == parent_id,
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.type == "folder",
            )
        )
        if item_result.scalars().first() is None:
            raise AiVisualDesignError("Dashboard parent folder not found")
        permission = await get_effective_permission_for_item(db, user_id, parent_id)
        if not permission_allows(permission, "edit"):
            raise PermissionError("No edit permission for dashboard parent folder")
        return str(parent_id)

    async def preview(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: AiVisualDesignPreviewRequest,
    ) -> dict[str, Any]:
        await self._require_workspace_member(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
        )
        target_type = self._choose_target(request)
        if target_type == "view" and not request.table_id:
            raise AiVisualDesignError("tableId is required to generate a view")

        existing_dashboard: Optional[dict[str, Any]] = None
        dashboard_target_fingerprint: Optional[str] = None
        dashboard_start: Optional[AiVisualDesignProposal] = None
        if request.dashboard_id:
            dashboard, dashboard_item, widgets, _ = await self._existing_dashboard(
                db,
                user_id=user_id,
                workspace_id=request.workspace_id,
                dashboard_id=request.dashboard_id,
            )
            dashboard_target_fingerprint = _dashboard_fingerprint(dashboard, widgets)
            dashboard_start = self._existing_dashboard_proposal(
                dashboard,
                dashboard_item,
                widgets,
            )
            existing_dashboard = {
                "dashboardId": dashboard.id,
                "name": dashboard_item.name,
                "description": dashboard.description or "",
                "widgetCount": len(widgets),
            }

        contexts = await self._readable_contexts(
            db,
            user_id=user_id,
            workspace_id=request.workspace_id,
            preferred_table_id=request.table_id,
            dashboard_id=request.dashboard_id,
        )
        if target_type == "view":
            contexts = {request.table_id: contexts[request.table_id]}

        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalars().first()
        user_name = str(user.name or "") if user else ""

        current_proposal = request.current_proposal or dashboard_start
        generation_mode = "deterministic-fallback"
        provider = generation_mode
        model_name: Optional[str] = None
        warnings: list[str] = []

        try:
            candidate, provider, model_name = await self._ai_proposal(
                db,
                user_id=user_id,
                prompt=request.prompt,
                target_type=target_type,
                contexts=contexts,
                current_proposal=current_proposal,
                instruction=request.instruction,
                model_override=request.model,
                existing_dashboard=existing_dashboard,
            )
            generation_mode = "ai"
            if candidate.kind != target_type:
                raise AiVisualDesignError("AI returned the wrong visual target type")
            normalized = await self._normalize_proposal(
                db,
                user_id=user_id,
                workspace_id=request.workspace_id,
                proposal=candidate,
                contexts=contexts,
                dashboard_id=request.dashboard_id,
            )
        except Exception as exc:
            warnings.append(
                "AI 结果不可用或未配置模型，已使用经过后端校验的安全生成规则。"
            )
            if current_proposal and request.instruction:
                fallback = self._refine_fallback(
                    current_proposal,
                    request.instruction,
                    contexts,
                    user_name,
                )
            elif target_type == "view":
                fallback = self._fallback_view(
                    request.prompt,
                    str(request.table_id),
                    contexts[str(request.table_id)],
                    user_name,
                )
            else:
                preferred = (
                    request.table_id
                    if request.table_id in contexts
                    else next(iter(contexts))
                )
                fallback = self._dashboard_baseline(
                    request.prompt,
                    preferred,
                    contexts[preferred],
                    name=existing_dashboard.get("name") if existing_dashboard else None,
                    description=existing_dashboard.get("description") if existing_dashboard else None,
                )
                if current_proposal and request.instruction:
                    fallback = self._refine_fallback(
                        fallback,
                        request.instruction,
                        contexts,
                        user_name,
                    )
            normalized = await self._normalize_proposal(
                db,
                user_id=user_id,
                workspace_id=request.workspace_id,
                proposal=fallback,
                contexts=contexts,
                dashboard_id=request.dashboard_id,
            )
            provider = "deterministic-fallback"
            model_name = None

        allowed_source_table_ids = sorted(contexts)
        schema_fingerprint = _schema_fingerprint(
            user_id,
            request.workspace_id,
            contexts,
        )
        parent_id = None
        if target_type == "dashboard" and not request.dashboard_id:
            parent_id = await self._resolve_parent(
                db,
                user_id=user_id,
                workspace_id=request.workspace_id,
                requested_parent_id=request.parent_id,
                table_id=request.table_id,
            )

        plan_id = str(uuid.uuid4())
        trace_id = f"aiv_{uuid.uuid4().hex}"
        request_payload = request.model_dump(mode="json", by_alias=True)
        request_payload["allowedSourceTableIds"] = allowed_source_table_ids
        request_payload["resolvedTargetType"] = target_type
        request_payload["resolvedParentId"] = parent_id
        request_payload["generationMode"] = generation_mode

        row = AiVisualDesignPlan(
            id=plan_id,
            trace_id=trace_id,
            user_id=user_id,
            workspace_id=request.workspace_id,
            target_type=target_type,
            table_id=request.table_id,
            parent_id=parent_id,
            dashboard_id=request.dashboard_id,
            prompt=request.prompt,
            request_payload=request_payload,
            proposal_payload=_clone(normalized),
            schema_fingerprint=schema_fingerprint,
            target_fingerprint=dashboard_target_fingerprint,
            provider=provider,
            model=model_name,
            status="previewed",
        )
        db.add(row)
        await db.commit()

        return {
            "planId": plan_id,
            "traceId": trace_id,
            "generationMode": generation_mode,
            "provider": provider,
            "model": model_name,
            "warnings": warnings,
            "proposal": normalized,
            "editableProposal": self._normalized_to_model(
                normalized,
                contexts,
            ).model_dump(mode="json", by_alias=True),
            "performance": {
                "schemaOnly": True,
                "recordRowsScanned": 0,
                "sourceTableCount": len(contexts),
                "dashboardWidgetsUseServerAggregation": target_type == "dashboard",
                "relationFieldsAvoided": True,
            },
            "target": {
                "type": target_type,
                "tableId": request.table_id,
                "dashboardId": request.dashboard_id,
                "parentId": parent_id,
            },
        }

    async def get_plan(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        plan_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(AiVisualDesignPlan).where(
                AiVisualDesignPlan.id == plan_id,
                AiVisualDesignPlan.user_id == user_id,
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "planId": row.id,
            "traceId": row.trace_id,
            "workspaceId": row.workspace_id,
            "targetType": row.target_type,
            "tableId": row.table_id,
            "parentId": row.parent_id,
            "dashboardId": row.dashboard_id,
            "prompt": row.prompt,
            "request": row.request_payload,
            "proposal": row.proposal_payload,
            "provider": row.provider,
            "model": row.model,
            "status": row.status,
            "result": row.result_payload,
            "error": row.error,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        }

    async def _apply_contexts(
        self,
        db: AsyncSession,
        *,
        row: AiVisualDesignPlan,
    ) -> dict[str, dict[str, Any]]:
        ids = [
            str(item)
            for item in list((row.request_payload or {}).get("allowedSourceTableIds") or [])
        ]
        contexts: dict[str, dict[str, Any]] = {}
        for table_id in ids:
            contexts[table_id] = await self._table_context(
                db,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                table_id=table_id,
                required="read",
            )
        if not contexts:
            raise AiVisualDesignError("Visual design has no valid source tables")
        current_fingerprint = _schema_fingerprint(
            row.user_id,
            row.workspace_id,
            contexts,
        )
        if current_fingerprint != row.schema_fingerprint:
            raise AiVisualDesignError(
                "Table schema or source permissions changed after preview; regenerate the visual design"
            )
        return contexts

    async def _unique_dashboard_name(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        parent_id: str,
        base_name: str,
        exclude_id: Optional[str] = None,
    ) -> str:
        result = await db.execute(
            select(WorkspaceItem.name, WorkspaceItem.id).where(
                WorkspaceItem.workspace_id == workspace_id,
                WorkspaceItem.parent_id == parent_id,
            )
        )
        names = {
            str(name).strip()
            for name, item_id in result.all()
            if str(item_id) != str(exclude_id or "")
        }
        if base_name not in names:
            return base_name
        index = 2
        while f"{base_name} {index}" in names:
            index += 1
        return f"{base_name} {index}"

    async def _source_read_check(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
    ) -> None:
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, "read"):
            raise PermissionError("Dashboard source table is no longer readable")

    async def apply(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: AiVisualDesignApplyRequest,
    ) -> dict[str, Any]:
        row_result = await db.execute(
            select(AiVisualDesignPlan)
            .where(
                AiVisualDesignPlan.id == request.plan_id,
                AiVisualDesignPlan.user_id == user_id,
            )
            .with_for_update()
        )
        row = row_result.scalars().first()
        if row is None:
            raise AiVisualDesignError("Visual design plan not found or no access")
        if row.status == "applied" and row.result_payload:
            return {
                **dict(row.result_payload),
                "idempotent": True,
            }

        try:
            contexts = await self._apply_contexts(db, row=row)
            if request.proposal is None:
                # The stored payload is already normalized against the exact
                # schema fingerprint checked above. Reusing it preserves
                # hiddenFieldIds and all runtime-normalized values exactly,
                # instead of lossy round-tripping through an editable model.
                normalized = _clone(row.proposal_payload)
                if normalized.get("kind") != row.target_type:
                    raise AiVisualDesignError(
                        "Stored proposal target type does not match the preview"
                    )
            else:
                if request.proposal.kind != row.target_type:
                    raise AiVisualDesignError(
                        "Proposal target type does not match the preview"
                    )
                normalized = await self._normalize_proposal(
                    db,
                    user_id=user_id,
                    workspace_id=row.workspace_id,
                    proposal=request.proposal,
                    contexts=contexts,
                    dashboard_id=row.dashboard_id,
                )

            if row.target_type == "view":
                view = normalized["view"]
                table_id = str(view["tableId"])
                if row.table_id and table_id != row.table_id:
                    raise AiVisualDesignError("View target table cannot change after preview")
                context = contexts.get(table_id)
                if context is None or not permission_allows(context["permission"], "edit"):
                    raise PermissionError("No edit permission for the target table")
                existing_result = await db.execute(
                    select(TableView.name).where(TableView.table_id == table_id)
                )
                unique_name = _next_view_name(
                    view["name"],
                    [str(item) for item in existing_result.scalars().all()],
                )
                view_id = f"v{uuid.uuid4().hex[:12]}"
                db.add(
                    TableView(
                        id=view_id,
                        table_id=table_id,
                        name=unique_name,
                        type=view["type"],
                        config=_clone(view["config"]),
                    )
                )
                await db.flush()
                result_payload = {
                    "planId": row.id,
                    "traceId": row.trace_id,
                    "status": "applied",
                    "targetType": "view",
                    "view": {
                        "id": view_id,
                        "tableId": table_id,
                        "name": unique_name,
                        "type": view["type"],
                        "config": view["config"],
                    },
                    "dashboard": None,
                    "idempotent": False,
                }
            else:
                dashboard_payload = normalized["dashboard"]
                source_table_ids = sorted(
                    {
                        str(widget["config"]["tableId"])
                        for widget in dashboard_payload["widgets"]
                    }
                )
                for table_id in source_table_ids:
                    await self._source_read_check(
                        db,
                        user_id=user_id,
                        table_id=table_id,
                    )

                if row.dashboard_id:
                    dashboard, item, existing_widgets, permission = await self._existing_dashboard(
                        db,
                        user_id=user_id,
                        workspace_id=row.workspace_id,
                        dashboard_id=row.dashboard_id,
                    )
                    required = "manage" if dashboard.is_public else "edit"
                    if not permission_allows(permission, required):
                        raise PermissionError("No permission to modify this dashboard")
                    current_target_fingerprint = _dashboard_fingerprint(
                        dashboard,
                        existing_widgets,
                    )
                    if current_target_fingerprint != row.target_fingerprint:
                        raise AiVisualDesignError(
                            "Dashboard changed after preview; regenerate the design before applying"
                        )
                    dashboard_id = dashboard.id
                    dashboard.description = dashboard_payload["description"]
                    item.name = await self._unique_dashboard_name(
                        db,
                        workspace_id=row.workspace_id,
                        parent_id=str(item.parent_id or ""),
                        base_name=dashboard_payload["name"],
                        exclude_id=dashboard_id,
                    )
                    await db.execute(
                        delete(DashboardWidget).where(
                            DashboardWidget.dashboard_id == dashboard_id
                        )
                    )
                else:
                    parent_id = str(row.parent_id or "")
                    if not parent_id:
                        raise AiVisualDesignError("Dashboard parent folder is missing")
                    parent_permission = await get_effective_permission_for_item(
                        db,
                        user_id,
                        parent_id,
                    )
                    if not permission_allows(parent_permission, "edit"):
                        raise PermissionError("No edit permission for dashboard parent folder")
                    dashboard_id = f"dsb{uuid.uuid4().hex[:12]}"
                    dashboard_name = await self._unique_dashboard_name(
                        db,
                        workspace_id=row.workspace_id,
                        parent_id=parent_id,
                        base_name=dashboard_payload["name"],
                    )
                    max_result = await db.execute(
                        select(WorkspaceItem.order_index).where(
                            WorkspaceItem.workspace_id == row.workspace_id,
                            WorkspaceItem.parent_id == parent_id,
                        ).order_by(WorkspaceItem.order_index.desc()).limit(1)
                    )
                    max_order = int(max_result.scalar() or 0)
                    item = WorkspaceItem(
                        id=dashboard_id,
                        workspace_id=row.workspace_id,
                        type="dashboard",
                        name=dashboard_name,
                        parent_id=parent_id,
                        order_index=max_order + 1,
                        default_view_id=None,
                    )
                    dashboard = Dashboard(
                        id=dashboard_id,
                        workspace_id=row.workspace_id,
                        description=dashboard_payload["description"],
                        is_public=False,
                    )
                    db.add(item)
                    db.add(dashboard)
                    await db.flush()

                created_widgets: list[dict[str, Any]] = []
                for index, widget in enumerate(dashboard_payload["widgets"]):
                    candidate = await validate_widget_candidate(
                        db,
                        dashboard_id,
                        {
                            "type": widget["type"],
                            "title": widget["title"],
                            "colorScheme": widget.get("colorScheme"),
                            "layout": widget["layout"],
                            "config": widget["config"],
                        },
                        require_complete=True,
                    )
                    widget_id = f"wdg_{uuid.uuid4().hex[:12]}"
                    db.add(
                        DashboardWidget(
                            id=widget_id,
                            dashboard_id=dashboard_id,
                            type=candidate["type"],
                            title=candidate["title"],
                            color_scheme=candidate.get("colorScheme"),
                            layout=candidate["layout"],
                            config=candidate["config"],
                            order_index=index,
                        )
                    )
                    created_widgets.append(
                        {
                            "id": widget_id,
                            "type": candidate["type"],
                            "title": candidate["title"],
                            "layout": candidate["layout"],
                            "config": candidate["config"],
                            "purpose": widget["purpose"],
                        }
                    )
                result_payload = {
                    "planId": row.id,
                    "traceId": row.trace_id,
                    "status": "applied",
                    "targetType": "dashboard",
                    "view": None,
                    "dashboard": {
                        "id": dashboard_id,
                        "name": item.name,
                        "description": dashboard.description or "",
                        "mode": "replaced" if row.dashboard_id else "created",
                        "widgets": created_widgets,
                    },
                    "idempotent": False,
                }

            row.status = "applied"
            row.proposal_payload = _clone(normalized)
            row.result_payload = _clone(result_payload)
            row.error = None
            row.applied_at = datetime.now(timezone.utc)
            await db.commit()

            if row.target_type == "dashboard":
                cache = get_dashboard_cache()
                for table_id in {
                    str(widget["config"]["tableId"])
                    for widget in result_payload["dashboard"]["widgets"]
                }:
                    await cache.invalidate_table(table_id)
            return result_payload
        except Exception as exc:
            await db.rollback()
            retry_result = await db.execute(
                select(AiVisualDesignPlan).where(
                    AiVisualDesignPlan.id == request.plan_id,
                    AiVisualDesignPlan.user_id == user_id,
                )
            )
            retry = retry_result.scalars().first()
            if retry is not None:
                retry.status = "apply_failed"
                retry.error = str(exc)[:4000]
                await db.commit()
            raise


ai_visual_design_service = AiVisualDesignService()
