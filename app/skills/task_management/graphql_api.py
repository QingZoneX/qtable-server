"""
GraphQL API - Task Management Tools

提供任务管理工具的 GraphQL 查询接口。
与现有 Strawberry GraphQL + auto_camel_case 集成。
"""
from __future__ import annotations

from typing import Any, Optional

import strawberry
from strawberry.types import Info

from app.api.graphql.helpers import _require_item_permission, _require_user


@strawberry.type
class TaskManagementDatetimeInfo:
    """当前日期时间信息"""
    iso8601: str
    unix_timestamp: float
    human_readable: str
    date: str
    time: str
    day_of_week: str
    day_of_week_en: str
    is_weekend: bool
    week_number: int
    timezone: str
    utc_offset: str


@strawberry.type
class TaskManagementUserInfo:
    """当前用户信息"""
    user_id: int
    username: str
    email: str
    display_name: str
    avatar_url: Optional[str]
    is_authenticated: bool
    workspace_role: Optional[str]


@strawberry.type
class TaskManagementWorkspaceInfo:
    """当前工作空间信息"""
    workspace_id: str
    workspace_name: str
    member_count: int
    is_owner: bool
    role: Optional[str]


@strawberry.type
class OverdueTaskItem:
    """延期任务项"""
    record_id: str
    title: str
    due_date: Optional[str]
    overdue_days: int
    severity: str
    status: str


@strawberry.type
class OverdueTaskResult:
    """延期任务查询结果"""
    table_id: str
    total_overdue: int
    by_severity: strawberry.scalars.JSON
    tasks: list[strawberry.scalars.JSON]
    analysis: strawberry.scalars.JSON


@strawberry.type
class ProjectProgressResult:
    """项目进度统计结果"""
    table_id: str
    progress: strawberry.scalars.JSON


@strawberry.type
class MemberWorkloadResult:
    """成员工作负载结果"""
    table_id: str
    members: list[strawberry.scalars.JSON]
    summary: strawberry.scalars.JSON


@strawberry.type
class BlockingTaskResult:
    """阻断任务检测结果"""
    table_id: str
    blocking_tasks: list[strawberry.scalars.JSON]
    summary: strawberry.scalars.JSON


@strawberry.type
class ProjectDelayPrediction:
    """项目延期预测结果"""
    table_id: str
    prediction: strawberry.scalars.JSON


@strawberry.type
class ExecutionPlanResult:
    """执行计划结果"""
    table_id: str
    plan_id: str
    strategy: str
    total_tasks: int
    phases: list[strawberry.scalars.JSON]
    critical_path: list[str]
    estimated_total_days: float
    estimated_completion: str
    risk_warnings: list[str]
    summary: str


@strawberry.type
class TaskManagementToolInfo:
    """工具元信息"""
    name: str
    title: str
    description: str
    tags: list[str]
    layer: str
    layer_order: int
    side_effect: str
    input_schema: strawberry.scalars.JSON
    output_schema: strawberry.scalars.JSON


@strawberry.type
class TaskManagementContext:
    """注入的任务管理上下文"""
    current_datetime: Optional[TaskManagementDatetimeInfo]
    current_user: Optional[TaskManagementUserInfo]
    current_workspace: Optional[TaskManagementWorkspaceInfo]
    locale: str
    timezone: str


# ============ Query ============

@strawberry.type
class TaskManagementQuery:
    """任务管理工具 GraphQL Query"""

    @strawberry.field(description="获取当前日期时间")
    async def task_management_get_datetime(
        self,
        info: Info,
        timezone: Optional[str] = None,
    ) -> TaskManagementDatetimeInfo:
        from datetime import datetime
        tz_str = timezone or "Asia/Shanghai"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = None

        now = datetime.now(tz or __import__('datetime').timezone.utc)
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekdays_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        dow = now.weekday()
        offset_hours = (now.utcoffset().total_seconds() / 3600) if now.utcoffset() else 0

        return TaskManagementDatetimeInfo(
            iso8601=now.isoformat(),
            unix_timestamp=now.timestamp(),
            human_readable=now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M:%S"),
            day_of_week=weekdays_cn[dow],
            day_of_week_en=weekdays_en[dow],
            is_weekend=dow >= 5,
            week_number=now.isocalendar()[1],
            timezone=tz_str,
            utc_offset=f"UTC{offset_hours:+g}",
        )

    @strawberry.field(description="获取任务管理工具清单")
    async def task_management_tools(
        self,
        info: Info,
    ) -> list[TaskManagementToolInfo]:
        from app.skills.task_management.registry import get_task_management_manifest
        manifests = get_task_management_manifest()
        return [
            TaskManagementToolInfo(
                name=m["name"],
                title=m["title"],
                description=m["description"],
                tags=m["tags"],
                layer=m["layer"],
                layer_order=m["layerOrder"],
                side_effect=m["sideEffect"],
                input_schema=m["inputSchema"],
                output_schema=m["outputSchema"],
            )
            for m in manifests
        ]

    @strawberry.field(description="获取延期任务列表")
    async def task_management_overdue_tasks(
        self,
        info: Info,
        table_id: str,
        limit: int = 50,
        workspace_id: Optional[str] = None,
    ) -> OverdueTaskResult:
        from app.skills.task_management.postgres_queries import pg_get_overdue_tasks
        db = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, table_id, "read")

        tasks = await pg_get_overdue_tasks(
            db=db,
            table_id=table_id,
            workspace_id=workspace_id,
            user_id=user.id,
            status_not_in=["已完成", "completed", "done", "closed"],
            limit=limit,
        )

        by_severity = {"minor": 0, "moderate": 0, "severe": 0, "critical": 0}
        for task in tasks:
            sev = task.get("severity", "minor")
            by_severity[sev] = by_severity.get(sev, 0) + 1

        total = len(tasks)
        analysis = {
            "averageOverdueDays": round(
                sum(t.get("overdueDays", 0) for t in tasks) / total, 1
            ) if total > 0 else 0,
        }

        return OverdueTaskResult(
            table_id=table_id,
            total_overdue=total,
            by_severity=by_severity,
            tasks=tasks,
            analysis=analysis,
        )

    @strawberry.field(description="计算项目进度")
    async def task_management_project_progress(
        self,
        info: Info,
        table_id: str,
        workspace_id: Optional[str] = None,
    ) -> ProjectProgressResult:
        from app.skills.task_management.postgres_queries import pg_calculate_project_progress
        db = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, table_id, "read")

        progress = await pg_calculate_project_progress(
            db=db,
            table_id=table_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        return ProjectProgressResult(table_id=table_id, progress=progress)

    @strawberry.field(description="获取成员工作负载")
    async def task_management_member_workload(
        self,
        info: Info,
        table_id: str,
        workspace_id: Optional[str] = None,
    ) -> MemberWorkloadResult:
        from app.skills.task_management.postgres_queries import pg_get_member_workload
        db = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, table_id, "read")

        members = await pg_get_member_workload(
            db=db,
            table_id=table_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )

        non_unassigned = [m for m in members if m.get("memberId") != "_unassigned"]
        overloaded = [m for m in non_unassigned if m.get("loadLevel") == "overloaded"]

        summary = {
            "totalMembers": len(non_unassigned),
            "overloadedMembers": len(overloaded),
            "recommendation": "建议重新分配任务以平衡负载" if overloaded else "当前负载分布合理",
        }

        return MemberWorkloadResult(table_id=table_id, members=members, summary=summary)

    @strawberry.field(description="检测阻断任务")
    async def task_management_blocking_tasks(
        self,
        info: Info,
        table_id: str,
        workspace_id: Optional[str] = None,
    ) -> BlockingTaskResult:
        from app.skills.task_management.postgres_queries import pg_detect_blocking_tasks
        db = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, table_id, "read")

        blocking = await pg_detect_blocking_tasks(
            db=db,
            table_id=table_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )

        by_severity = {}
        for t in blocking:
            sev = t.get("blockingSeverity", "low")
            by_severity[sev] = by_severity.get(sev, 0) + 1

        return BlockingTaskResult(
            table_id=table_id,
            blocking_tasks=blocking,
            summary={
                "total": len(blocking),
                "bySeverity": by_severity,
                "criticalBlockers": by_severity.get("critical", 0),
            },
        )

    @strawberry.field(description="预测项目延期风险")
    async def task_management_delay_prediction(
        self,
        info: Info,
        table_id: str,
        workspace_id: Optional[str] = None,
    ) -> ProjectDelayPrediction:
        from app.skills.task_management.postgres_queries import pg_predict_project_delay
        db = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, table_id, "read")

        prediction = await pg_predict_project_delay(
            db=db,
            table_id=table_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        return ProjectDelayPrediction(table_id=table_id, prediction=prediction)


def register_task_management_queries():
    """
    在 Strawberry Query 中注册任务管理查询字段。

    用法 (在 api/graphql/queries/__init__.py 中):
        from app.skills.task_management.graphql_api import TaskManagementQuery
        from app.api.graphql.queries.task_management import register_task_management_queries

        # 在 Query 类中添加:
        task_management = strawberry.field(resolver=lambda: TaskManagementQuery())
    """
    pass
