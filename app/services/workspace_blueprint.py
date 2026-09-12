from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_config import AiConfig
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, TableGroup, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_generation import WorkspaceGenerationTrace
from app.models.workspace_member import WorkspaceMember
from app.services.ai_service import get_ai_client

MAX_GOAL_LENGTH = 2000
MAX_TABLES = 4
MAX_FIELDS = 32
MAX_VIEWS = 8
MAX_DASHBOARDS = 3
MAX_WIDGETS = 8
FIELD_TYPES = {
    "text", "number", "select", "multiSelect", "member", "date", "progress",
    "rating", "url", "email", "phone", "attachment", "autoNumber", "relation",
}
VIEW_TYPES = {"grid", "board", "gantt", "calendar", "gallery"}
WIDGET_TYPES = {"metric", "bar", "horizontalBar", "line", "area", "pie", "donut"}


class WorkspaceBlueprintError(ValueError):
    pass


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _text(value: Any, fallback: str = "", limit: int = 255) -> str:
    out = " ".join(str(value or "").split()).strip() or fallback
    return out[:limit]


def _slug(value: Any, fallback: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip()).strip("_")
    return (out or fallback)[:64]


def _unique(value: Any, fallback: str, used: set[str]) -> str:
    base = _slug(value, fallback)
    out = base
    n = 2
    while out in used:
        out = f"{base}_{n}"
        n += 1
    used.add(out)
    return out


def _baseline(goal: str, team_size: Optional[int], deadline: Optional[str]) -> Dict[str, Any]:
    project_name = _text(goal, "新项目", 36)
    context = []
    if team_size:
        context.append(f"团队规模 {team_size} 人")
    if deadline:
        context.append(f"目标截止 {deadline}")
    return {
        "version": 1,
        "projectName": project_name,
        "rationale": "以任务执行为中心，默认提供负责人、状态、优先级、时间、工作量、依赖和项目进度。",
        "contextSummary": "；".join(context),
        "folder": {"create": True, "name": project_name},
        "tables": [{
            "key": "tasks",
            "name": "任务",
            "description": "项目任务执行清单",
            "fields": [
                {"key": "title", "name": "任务名称", "type": "text"},
                {"key": "description", "name": "任务描述", "type": "text"},
                {"key": "status", "name": "状态", "type": "select", "options": [
                    {"id": "todo", "label": "未开始", "color": "#E5E7EB"},
                    {"id": "doing", "label": "进行中", "color": "#DBEAFE"},
                    {"id": "blocked", "label": "阻塞", "color": "#FEE2E2"},
                    {"id": "done", "label": "已完成", "color": "#DCFCE7"},
                ]},
                {"key": "assignee", "name": "负责人", "type": "member"},
                {"key": "priority", "name": "优先级", "type": "select", "options": [
                    {"id": "low", "label": "低", "color": "#E5E7EB"},
                    {"id": "medium", "label": "中", "color": "#DBEAFE"},
                    {"id": "high", "label": "高", "color": "#FEF3C7"},
                    {"id": "urgent", "label": "紧急", "color": "#FEE2E2"},
                ]},
                {"key": "startDate", "name": "开始时间", "type": "date", "property": {"format": "YYYY-MM-DD"}},
                {"key": "dueDate", "name": "截止时间", "type": "date", "property": {"format": "YYYY-MM-DD"}},
                {"key": "workload", "name": "预计工作量", "type": "number", "property": {"suffix": "h", "precision": 1}},
                {"key": "progress", "name": "进度", "type": "progress", "property": {"max": 100}},
                {"key": "dependencies", "name": "前置依赖", "type": "relation", "property": {
                    "targetTableKey": "tasks", "displayFieldKey": "title", "multiple": True
                }},
            ],
            "views": [
                {"key": "grid", "name": "任务清单", "type": "grid", "config": {}},
                {"key": "kanban", "name": "任务看板", "type": "board", "config": {"groupFieldKey": "status"}},
                {"key": "gantt", "name": "项目甘特图", "type": "gantt", "config": {
                    "startFieldKey": "startDate", "endFieldKey": "dueDate", "progressFieldKey": "progress"
                }},
                {"key": "calendar", "name": "项目日历", "type": "calendar", "config": {
                    "startFieldKey": "startDate", "endFieldKey": "dueDate"
                }},
            ],
        }],
        "dashboards": [{
            "key": "overview", "name": "项目进度", "description": "项目任务总量与状态分布",
            "widgets": [
                {"key": "count", "type": "metric", "title": "任务总数", "tableKey": "tasks",
                 "metric": {"aggregation": "count"}, "layout": {"x": 0, "y": 0, "w": 4, "h": 6}},
                {"key": "status", "type": "bar", "title": "任务状态分布", "tableKey": "tasks",
                 "dimensionFieldKey": "status", "metric": {"aggregation": "count"},
                 "layout": {"x": 4, "y": 0, "w": 8, "h": 8}},
            ],
        }],
        "nextActions": [
            {"id": "split", "label": "帮我拆第一批任务", "action": "task_split"},
            {"id": "estimate", "label": "估算工作量与工期", "action": "estimate_workload"},
            {"id": "import", "label": "导入已有任务", "action": "import"},
            {"id": "invite", "label": "邀请项目成员", "action": "invite_members"},
        ],
    }


def _normalize_options(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out = []
    used: set[str] = set()
    for i, raw in enumerate(value[:32]):
        if not isinstance(raw, Mapping):
            continue
        oid = _unique(raw.get("id") or raw.get("label"), f"option_{i+1}", used)
        item = {"id": oid, "label": _text(raw.get("label"), oid, 80)}
        if raw.get("color"):
            item["color"] = str(raw.get("color"))[:32]
        out.append(item)
    return out


def normalize_blueprint(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    raw_tables = candidate.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise WorkspaceBlueprintError("Blueprint requires at least one table")
    if len(raw_tables) > MAX_TABLES:
        raise WorkspaceBlueprintError("Too many tables")

    tables = []
    table_used: set[str] = set()
    for ti, raw_table in enumerate(raw_tables):
        if not isinstance(raw_table, Mapping):
            continue
        table_key = _unique(raw_table.get("key"), f"table_{ti+1}", table_used)
        raw_fields = raw_table.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > MAX_FIELDS:
            raise WorkspaceBlueprintError(f"Invalid fields for table {table_key}")
        fields = []
        field_used: set[str] = set()
        for fi, raw_field in enumerate(raw_fields):
            if not isinstance(raw_field, Mapping):
                continue
            ftype = str(raw_field.get("type") or "text")
            if ftype not in FIELD_TYPES:
                raise WorkspaceBlueprintError(f"Unsupported field type: {ftype}")
            fkey = _unique(raw_field.get("key"), f"field_{fi+1}", field_used)
            field = {"key": fkey, "name": _text(raw_field.get("name"), fkey, 120), "type": ftype}
            if ftype in {"select", "multiSelect", "member"}:
                opts = _normalize_options(raw_field.get("options"))
                if opts:
                    field["options"] = opts
            if isinstance(raw_field.get("property"), Mapping):
                field["property"] = _clone(dict(raw_field["property"]))
            fields.append(field)
        raw_views = raw_table.get("views")
        if not isinstance(raw_views, list) or not raw_views:
            raw_views = [{"key": "grid", "name": "表格", "type": "grid", "config": {}}]
        if len(raw_views) > MAX_VIEWS:
            raise WorkspaceBlueprintError("Too many views")
        views = []
        view_used: set[str] = set()
        for vi, raw_view in enumerate(raw_views):
            if not isinstance(raw_view, Mapping):
                continue
            vtype = str(raw_view.get("type") or "grid")
            if vtype not in VIEW_TYPES:
                raise WorkspaceBlueprintError(f"Unsupported view type: {vtype}")
            views.append({
                "key": _unique(raw_view.get("key"), f"view_{vi+1}", view_used),
                "name": _text(raw_view.get("name"), vtype, 120),
                "type": vtype,
                "config": _clone(dict(raw_view.get("config") or {})),
            })
        tables.append({
            "key": table_key,
            "name": _text(raw_table.get("name"), f"数据表 {ti+1}", 120),
            "description": _text(raw_table.get("description"), "", 500),
            "fields": fields,
            "views": views,
        })

    table_keys = {t["key"] for t in tables}
    for table in tables:
        field_keys = {f["key"] for f in table["fields"]}
        for field in table["fields"]:
            if field["type"] == "relation":
                prop = dict(field.get("property") or {})
                target = _slug(prop.get("targetTableKey"), table["key"])
                if target not in table_keys:
                    raise WorkspaceBlueprintError(f"Unknown relation target table: {target}")
                prop["targetTableKey"] = target
                field["property"] = prop
        for view in table["views"]:
            cfg = dict(view.get("config") or {})
            for k in ("groupFieldKey", "startFieldKey", "endFieldKey", "progressFieldKey", "coverFieldKey", "titleFieldKey"):
                if cfg.get(k) and _slug(cfg[k], "") not in field_keys:
                    cfg.pop(k, None)
            view["config"] = cfg

    dashboards = []
    raw_dashboards = candidate.get("dashboards")
    if isinstance(raw_dashboards, list):
        if len(raw_dashboards) > MAX_DASHBOARDS:
            raise WorkspaceBlueprintError("Too many dashboards")
        dash_used: set[str] = set()
        for di, raw_dash in enumerate(raw_dashboards):
            if not isinstance(raw_dash, Mapping):
                continue
            dkey = _unique(raw_dash.get("key"), f"dashboard_{di+1}", dash_used)
            widgets = []
            widget_used: set[str] = set()
            for wi, raw_widget in enumerate((raw_dash.get("widgets") or [])[:MAX_WIDGETS]):
                if not isinstance(raw_widget, Mapping):
                    continue
                wtype = str(raw_widget.get("type") or "metric")
                if wtype not in WIDGET_TYPES:
                    continue
                table_key = _slug(raw_widget.get("tableKey"), "tasks")
                if table_key not in table_keys:
                    continue
                widget = {
                    "key": _unique(raw_widget.get("key"), f"widget_{wi+1}", widget_used),
                    "type": wtype,
                    "title": _text(raw_widget.get("title"), f"组件 {wi+1}", 120),
                    "tableKey": table_key,
                    "metric": _clone(dict(raw_widget.get("metric") or {"aggregation": "count"})),
                    "layout": _clone(dict(raw_widget.get("layout") or {"x": 0, "y": wi * 6, "w": 6, "h": 6})),
                }
                if raw_widget.get("dimensionFieldKey"):
                    widget["dimensionFieldKey"] = _slug(raw_widget.get("dimensionFieldKey"), "status")
                widgets.append(widget)
            dashboards.append({
                "key": dkey,
                "name": _text(raw_dash.get("name"), f"仪表盘 {di+1}", 120),
                "description": _text(raw_dash.get("description"), "", 500),
                "widgets": widgets,
            })

    actions = []
    for i, raw in enumerate((candidate.get("nextActions") or [])[:8]):
        if isinstance(raw, Mapping):
            actions.append({
                "id": _slug(raw.get("id"), f"action_{i+1}"),
                "label": _text(raw.get("label"), "下一步", 80),
                "action": _slug(raw.get("action"), "custom"),
            })
    folder_raw = candidate.get("folder")
    folder = {
        "create": bool(folder_raw.get("create", True)) if isinstance(folder_raw, Mapping) else True,
        "name": _text(
            folder_raw.get("name") if isinstance(folder_raw, Mapping) else candidate.get("projectName"),
            _text(candidate.get("projectName"), "项目", 120),
            120,
        ),
    }
    return {
        "version": 1,
        "projectName": _text(candidate.get("projectName"), "新项目", 120),
        "rationale": _text(candidate.get("rationale"), "", 1000),
        "contextSummary": _text(candidate.get("contextSummary"), "", 500),
        "folder": folder,
        "tables": tables,
        "dashboards": dashboards,
        "nextActions": actions,
    }


def ensure_project_baseline(candidate: Mapping[str, Any], goal: str, team_size: Optional[int], deadline: Optional[str]) -> Dict[str, Any]:
    base = normalize_blueprint(_baseline(goal, team_size, deadline))
    try:
        normalized = normalize_blueprint(candidate)
    except WorkspaceBlueprintError:
        return base
    tasks = next((t for t in normalized["tables"] if t["key"] == "tasks"), normalized["tables"][0])
    base_tasks = base["tables"][0]
    field_keys = {f["key"] for f in tasks["fields"]}
    for field in base_tasks["fields"]:
        if field["key"] not in field_keys:
            tasks["fields"].append(_clone(field))
    view_types = {v["type"] for v in tasks["views"]}
    for view in base_tasks["views"]:
        if view["type"] not in view_types:
            tasks["views"].append(_clone(view))
    dep = next((f for f in tasks["fields"] if f["key"] == "dependencies"), None)
    if dep:
        dep["property"] = {
            **dict(dep.get("property") or {}),
            "targetTableKey": tasks["key"],
            "displayFieldKey": "title",
            "multiple": True,
        }
    if not normalized["dashboards"]:
        dash = _clone(base["dashboards"][0])
        for widget in dash["widgets"]:
            widget["tableKey"] = tasks["key"]
        normalized["dashboards"] = [dash]
    if not normalized["nextActions"]:
        normalized["nextActions"] = _clone(base["nextActions"])
    return normalize_blueprint(normalized)


def _extract_json(content: str) -> Mapping[str, Any]:
    cleaned = (content or "").strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith(fence):
        cleaned = cleaned[:-3]
    payload = json.loads(cleaned.strip())
    if not isinstance(payload, Mapping):
        raise WorkspaceBlueprintError("AI blueprint must be an object")
    return payload


async def _latest_ai_config(db: AsyncSession, user_id: int) -> Optional[AiConfig]:
    result = await db.execute(
        select(AiConfig).where(AiConfig.user_id == str(user_id)).order_by(AiConfig.created_at.desc())
    )
    return result.scalars().first()


async def _request_ai_blueprint(
    config: AiConfig,
    goal: str,
    baseline: Mapping[str, Any],
    team_size: Optional[int],
    deadline: Optional[str],
    current_blueprint: Optional[Mapping[str, Any]],
    instruction: Optional[str],
) -> Mapping[str, Any]:
    client, model = await get_ai_client(config.api_key_encrypted, config.model)
    system = (
        "You design small QTable project workspaces. Return only one JSON object. "
        "Stay in project/task management. Use symbolic keys, never database IDs. "
        "Supported field types: text, number, select, multiSelect, member, date, progress, "
        "rating, url, email, phone, attachment, autoNumber, relation. "
        "Supported views: grid, board, gantt, calendar, gallery. "
        "Relation property uses targetTableKey, displayFieldKey and multiple. "
        "Do not create records or executable content."
    )
    context = {
        "goal": goal,
        "teamSize": team_size,
        "deadline": deadline,
        "instruction": instruction,
        "currentBlueprint": current_blueprint,
        "safeBaseline": baseline,
    }
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        stream=False,
        temperature=0.2,
    )
    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise WorkspaceBlueprintError("AI returned empty blueprint")
    return _extract_json(content)


async def preview_workspace_blueprint(
    db: AsyncSession,
    user_id: int,
    workspace_id: str,
    parent_id: str,
    goal: str,
    team_size: Optional[int] = None,
    deadline: Optional[str] = None,
    current_blueprint: Optional[Mapping[str, Any]] = None,
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    if not str(goal or "").strip():
        raise WorkspaceBlueprintError("Goal is required")
    if len(str(goal)) > MAX_GOAL_LENGTH:
        raise WorkspaceBlueprintError("Goal is too long")
    if team_size is not None and not 1 <= int(team_size) <= 200:
        raise WorkspaceBlueprintError("Team size must be between 1 and 200")
    normalized_goal = _text(goal, "", MAX_GOAL_LENGTH)
    baseline = _baseline(normalized_goal, team_size, deadline)
    warnings: List[str] = []
    mode = "fallback"
    candidate: Mapping[str, Any] = current_blueprint if isinstance(current_blueprint, Mapping) else baseline
    config = await _latest_ai_config(db, user_id)
    if config:
        try:
            candidate = await _request_ai_blueprint(
                config, normalized_goal, baseline, team_size, deadline, current_blueprint, instruction
            )
            mode = "ai"
        except Exception:
            warnings.append("AI 生成暂不可用，已使用经过验证的项目蓝图。")
    else:
        warnings.append("当前未配置 AI Provider，已使用经过验证的项目蓝图；配置 AI 后可继续自然语言细化。")
    blueprint = ensure_project_baseline(candidate, normalized_goal, team_size, deadline)
    trace_id = f"wgt_{uuid.uuid4().hex}"
    db.add(WorkspaceGenerationTrace(
        id=trace_id,
        user_id=user_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        goal=normalized_goal,
        request_context={"teamSize": team_size, "deadline": deadline, "instruction": _text(instruction, "", 1000) or None},
        blueprint=_clone(blueprint),
        status="previewed",
    ))
    await db.commit()
    return {"traceId": trace_id, "generationMode": mode, "warnings": warnings, "blueprint": blueprint}


def _toolbar(view_type: str) -> List[str]:
    if view_type == "board":
        return ["group", "filter", "sort", "share"]
    if view_type in {"gantt", "calendar", "gallery"}:
        return ["insertRow", "viewSettings", "fields", "filter", "group", "sort", "automations", "share"]
    return ["insertRow", "fields", "filter", "group", "sort", "automations", "share"]


def _view_config(view: Mapping[str, Any], field_ids: Mapping[str, str]) -> Dict[str, Any]:
    vtype = str(view["type"])
    raw = dict(view.get("config") or {})
    cfg: Dict[str, Any] = {
        "toolbar": {"items": _toolbar(vtype)},
        "filters": [],
        "sorts": [],
        "groupConfig": {
            "fieldId": field_ids.get(str(raw.get("groupFieldKey") or "")) if vtype == "board" else None,
            "order": "asc",
        },
        "hiddenFieldIds": [],
    }
    if vtype == "gantt":
        cfg["ganttConfig"] = {
            "startFieldId": field_ids.get(str(raw.get("startFieldKey") or "")),
            "endFieldId": field_ids.get(str(raw.get("endFieldKey") or "")),
            "progressFieldId": field_ids.get(str(raw.get("progressFieldKey") or "")),
        }
    if vtype == "calendar":
        cfg["calendarConfig"] = {
            "startFieldId": field_ids.get(str(raw.get("startFieldKey") or "")),
            "endFieldId": field_ids.get(str(raw.get("endFieldKey") or "")),
            "weekStartsOn": 1,
        }
    if vtype == "gallery":
        cfg["galleryConfig"] = {
            "coverFieldId": field_ids.get(str(raw.get("coverFieldKey") or "")),
            "titleFieldId": field_ids.get(str(raw.get("titleFieldKey") or "")),
            "cardSize": "medium",
            "imageFit": "cover",
            "showFieldNames": True,
        }
    return cfg


async def _next_order(db: AsyncSession, workspace_id: str, parent_id: str) -> int:
    result = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.workspace_id == workspace_id,
            WorkspaceItem.parent_id == parent_id,
        )
    )
    return int(result.scalar() or 0) + 1


async def _create_rows(
    db: AsyncSession,
    blueprint: Mapping[str, Any],
    workspace_id: str,
    parent_id: str,
) -> Dict[str, Any]:
    parent_result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    parent = parent_result.scalars().first()
    if not parent or parent.type in {"table", "dashboard"}:
        raise WorkspaceBlueprintError("Invalid target parent")

    project_parent = parent_id
    folder_out = None
    if blueprint.get("folder", {}).get("create", True):
        folder_id = f"fld{uuid.uuid4().hex[:20]}"
        folder_name = _text(blueprint.get("folder", {}).get("name"), blueprint.get("projectName") or "项目", 120)
        db.add(WorkspaceItem(
            id=folder_id, workspace_id=workspace_id, type="folder", name=folder_name,
            parent_id=parent_id, order_index=await _next_order(db, workspace_id, parent_id),
            default_view_id=None,
        ))
        project_parent = folder_id
        folder_out = {"id": folder_id, "name": folder_name}

    member_result = await db.execute(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(User.name.asc(), User.email.asc(), User.id.asc())
    )
    workspace_member_options = [
        {
            "id": str(member.id),
            "label": member.name or member.email or str(member.id),
            "color": "#F3F4F6",
        }
        for member in member_result.scalars().all()
    ]

    tables = list(blueprint["tables"])
    table_ids = {str(t["key"]): f"dst{uuid.uuid4().hex[:20]}" for t in tables}
    field_ids: Dict[str, Dict[str, str]] = {}
    for table in tables:
        field_ids[str(table["key"])] = {
            str(field["key"]): f"f{i+1}" for i, field in enumerate(table["fields"])
        }

    created_tables = []
    order_index = 1
    for table in tables:
        tkey = str(table["key"])
        tid = table_ids[tkey]
        views = list(table["views"])
        view_ids = {str(v["key"]): f"v{i+1}" for i, v in enumerate(views)}
        default_view = next(
            (view_ids[str(v["key"])] for v in views if v["type"] == "grid"),
            next(iter(view_ids.values())),
        )
        db.add(WorkspaceItem(
            id=tid, workspace_id=workspace_id, type="table", name=str(table["name"]),
            parent_id=project_parent, order_index=order_index, default_view_id=default_view,
        ))
        order_index += 1
        for i, field in enumerate(table["fields"]):
            prop = _clone(field.get("property"))
            if field["type"] == "relation":
                prop = dict(prop or {})
                target_key = str(prop.pop("targetTableKey", tkey))
                display_key = str(prop.pop("displayFieldKey", "title"))
                if target_key not in table_ids:
                    raise WorkspaceBlueprintError(f"Unknown relation target table: {target_key}")
                prop["targetTableId"] = table_ids[target_key]
                display_id = field_ids.get(target_key, {}).get(display_key)
                if display_id:
                    prop["displayFieldId"] = display_id
                prop["multiple"] = bool(prop.get("multiple", True))
            field_options = _clone(field.get("options"))
            if field["type"] == "member" and not field_options:
                field_options = _clone(workspace_member_options)
            db.add(TableField(
                id=field_ids[tkey][str(field["key"])],
                table_id=tid,
                name=str(field["name"]),
                type=str(field["type"]),
                options=field_options,
                property=prop,
                order_index=i,
            ))
        for view in views:
            db.add(TableView(
                id=view_ids[str(view["key"])],
                table_id=tid,
                name=str(view["name"]),
                type=str(view["type"]),
                config=_view_config(view, field_ids[tkey]),
            ))
        db.add(TableGroup(id=f"grp_{tid}", table_id=tid, field_id=None, order="asc"))
        created_tables.append({
            "key": tkey, "id": tid, "name": table["name"], "defaultViewId": default_view
        })

    created_dashboards = []
    for dash in blueprint.get("dashboards") or []:
        did = f"dsb{uuid.uuid4().hex[:20]}"
        db.add(WorkspaceItem(
            id=did, workspace_id=workspace_id, type="dashboard", name=str(dash["name"]),
            parent_id=project_parent, order_index=order_index, default_view_id=None,
        ))
        order_index += 1
        db.add(Dashboard(
            id=did, workspace_id=workspace_id,
            description=str(dash.get("description") or ""), is_public=False,
        ))
        for wi, widget in enumerate(dash.get("widgets") or []):
            tkey = str(widget.get("tableKey") or "")
            tid = table_ids.get(tkey)
            if not tid:
                continue
            fmap = field_ids[tkey]
            metric = dict(widget.get("metric") or {"aggregation": "count"})
            metric_field_key = metric.get("fieldKey")
            dimension_key = widget.get("dimensionFieldKey")
            db.add(DashboardWidget(
                id=f"wdg{uuid.uuid4().hex[:20]}",
                dashboard_id=did,
                type=str(widget["type"]),
                title=str(widget.get("title") or ""),
                color_scheme=None,
                layout=_clone(widget.get("layout") or {"x": 0, "y": wi * 6, "w": 6, "h": 6}),
                config={
                    "tableId": tid,
                    "dimensionFieldId": fmap.get(str(dimension_key)) if dimension_key else None,
                    "metric": {
                        "aggregation": str(metric.get("aggregation") or "count"),
                        "fieldId": fmap.get(str(metric_field_key)) if metric_field_key else None,
                    },
                    "filters": [],
                    "sort": {"by": "value", "order": "desc"},
                    "limit": 50,
                },
                order_index=wi + 1,
            ))
        created_dashboards.append({"key": dash["key"], "id": did, "name": dash["name"]})

    await db.flush()
    return {
        "folder": folder_out,
        "tables": created_tables,
        "dashboards": created_dashboards,
        "nextActions": _clone(blueprint.get("nextActions") or []),
    }


async def apply_workspace_blueprint(
    db: AsyncSession,
    trace_id: str,
    user_id: int,
    workspace_id: str,
    parent_id: str,
    blueprint: Mapping[str, Any],
) -> Dict[str, Any]:
    result = await db.execute(
        select(WorkspaceGenerationTrace).where(
            WorkspaceGenerationTrace.id == trace_id,
            WorkspaceGenerationTrace.user_id == user_id,
            WorkspaceGenerationTrace.workspace_id == workspace_id,
        )
    )
    trace = result.scalars().first()
    if not trace:
        raise WorkspaceBlueprintError("Generation trace not found or no access")
    if trace.parent_id != parent_id:
        raise WorkspaceBlueprintError("Generation trace parent mismatch")
    if trace.status == "applied" and trace.created_items:
        return {
            "traceId": trace.id, "status": "applied",
            "created": _clone(trace.created_items), "idempotent": True,
        }

    normalized = normalize_blueprint(blueprint)
    try:
        created = await _create_rows(db, normalized, workspace_id, parent_id)
        trace.blueprint = _clone(normalized)
        trace.status = "applied"
        trace.created_items = _clone(created)
        trace.error_message = None
        trace.applied_at = datetime.now(timezone.utc)
        await db.commit()
        return {
            "traceId": trace.id, "status": "applied",
            "created": created, "idempotent": False,
        }
    except Exception as exc:
        await db.rollback()
        retry = await db.execute(
            select(WorkspaceGenerationTrace).where(
                WorkspaceGenerationTrace.id == trace_id,
                WorkspaceGenerationTrace.user_id == user_id,
            )
        )
        failed_trace = retry.scalars().first()
        if failed_trace:
            failed_trace.status = "failed"
            failed_trace.error_message = _text(str(exc), exc.__class__.__name__, 1000)
            await db.commit()
        raise
