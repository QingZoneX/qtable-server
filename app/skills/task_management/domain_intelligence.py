"""
Layer 3: Domain Intelligence Tools - 领域智能

提供面向任务管理领域的高级分析工具:
- get_overdue_tasks: 延期任务检测 (含严重程度分级)
- calculate_project_progress: 项目进度统计
- get_member_workload: 成员工作负载分析
- detect_blocking_tasks: 阻断任务检测
- predict_project_delay: 项目延期预测 (含风险评估和建议)
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.skills.contracts import (
    SkillMetadata,
    SkillPermissionRequirement,
    SkillSideEffect,
)
from app.skills.runtime import (
    SkillDefinition,
    SkillExecutionContext,
)
from app.skills.task_management.postgres_queries import (
    pg_calculate_project_progress,
    pg_detect_blocking_tasks,
    pg_get_member_workload,
    pg_get_overdue_tasks,
    pg_predict_project_delay,
)

# ============ Tool 4: get_overdue_tasks ============


class GetOverdueTasksInput(BaseModel):
    """get_overdue_tasks 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")
    limit: int = Field(default=50, ge=1, le=200)
    exclude_statuses: list[str] = Field(
        default_factory=lambda: ["已完成", "completed", "done", "closed"],
        alias="excludeStatuses",
    )


class GetOverdueTasksOutput(BaseModel):
    """get_overdue_tasks 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    total_overdue: int = Field(alias="totalOverdue")
    by_severity: dict[str, int] = Field(default_factory=dict, alias="bySeverity")
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] = Field(default_factory=dict)


async def handle_get_overdue_tasks(
    context: SkillExecutionContext,
    data: GetOverdueTasksInput,
) -> dict[str, Any]:
    """获取所有延期任务，含严重程度分级和分析"""
    tasks = await pg_get_overdue_tasks(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
        status_not_in=data.exclude_statuses,
        limit=data.limit,
    )

    by_severity: dict[str, int] = {"minor": 0, "moderate": 0, "severe": 0, "critical": 0}
    for task in tasks:
        severity = task.get("severity", "minor")
        by_severity[severity] = by_severity.get(severity, 0) + 1

    total = len(tasks)
    analysis = {
        "averageOverdueDays": round(
            sum(t.get("overdueDays", 0) for t in tasks) / total, 1
        ) if total > 0 else 0,
        "mostOverdue": tasks[0] if total > 0 else None,
        "requiresImmediateAttention": by_severity.get("critical", 0) + by_severity.get("severe", 0) > 0,
        "recommendedActions": _build_overdue_recommendations(by_severity, total),
    }

    return {
        "tableId": data.table_id,
        "totalOverdue": total,
        "bySeverity": by_severity,
        "tasks": tasks,
        "analysis": analysis,
    }


def _build_overdue_recommendations(by_severity: dict[str, int], total: int) -> list[str]:
    recommendations: list[str] = []
    if by_severity.get("critical", 0) > 0:
        recommendations.append(f"立即处理 {by_severity['critical']} 个紧急延期任务（延期超过14天）")
    if by_severity.get("severe", 0) > 0:
        recommendations.append(f"优先跟进 {by_severity['severe']} 个严重延期任务（延期8-14天）")
    if by_severity.get("moderate", 0) > 3:
        recommendations.append("建议召开进度同步会，集中处理中度延期任务")
    if total > 10:
        recommendations.append("延期任务数量较多，建议检查项目排期是否合理")
    if not recommendations:
        recommendations.append("当前延期情况可控，继续保持")
    return recommendations


# ============ Tool 5: calculate_project_progress ============

class CalculateProjectProgressInput(BaseModel):
    """calculate_project_progress 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")


class CalculateProjectProgressOutput(BaseModel):
    """calculate_project_progress 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    progress: dict[str, Any]


async def handle_calculate_project_progress(
    context: SkillExecutionContext,
    data: CalculateProjectProgressInput,
) -> dict[str, Any]:
    """计算项目进度统计"""
    progress = await pg_calculate_project_progress(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )

    return {
        "tableId": data.table_id,
        "progress": progress,
    }


# ============ Tool 6: get_member_workload ============

class GetMemberWorkloadInput(BaseModel):
    """get_member_workload 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")
    team_id: Optional[str] = Field(default=None, alias="teamId")


class GetMemberWorkloadOutput(BaseModel):
    """get_member_workload 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    members: list[dict[str, Any]]
    summary: dict[str, Any] = Field(default_factory=dict)


async def handle_get_member_workload(
    context: SkillExecutionContext,
    data: GetMemberWorkloadInput,
) -> dict[str, Any]:
    """获取成员工作负载"""
    members = await pg_get_member_workload(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
        team_id=data.team_id,
    )

    # 构建摘要
    non_unassigned = [m for m in members if m.get("memberId") != "_unassigned"]
    overloaded = [m for m in non_unassigned if m.get("loadLevel") == "overloaded"]
    highly_loaded = [m for m in non_unassigned if m.get("loadLevel") == "high"]
    has_overdue = [m for m in non_unassigned if m.get("overdueTasks", 0) > 0]

    avg_load = round(
        sum(m.get("loadScore", 0) for m in non_unassigned) / len(non_unassigned), 1
    ) if non_unassigned else 0

    return {
        "tableId": data.table_id,
        "members": members,
        "summary": {
            "totalMembers": len(non_unassigned),
            "averageLoadScore": avg_load,
            "overloadedMembers": len(overloaded),
            "overloadedMemberIds": [m["memberId"] for m in overloaded],
            "highlyLoadedMembers": len(highly_loaded),
            "membersWithOverdue": len(has_overdue),
            "unassignedTasks": next(
                (m.get("totalTasks", 0) for m in members if m.get("memberId") == "_unassigned"), 0
            ),
            "recommendation": (
                "建议重新分配任务以平衡负载" if overloaded or avg_load > 65
                else "当前负载分布合理"
            ),
        },
    }


# ============ Tool 7: detect_blocking_tasks ============

class DetectBlockingTasksInput(BaseModel):
    """detect_blocking_tasks 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")


class DetectBlockingTasksOutput(BaseModel):
    """detect_blocking_tasks 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    blocking_tasks: list[dict[str, Any]] = Field(alias="blockingTasks")
    summary: dict[str, Any] = Field(default_factory=dict)


async def handle_detect_blocking_tasks(
    context: SkillExecutionContext,
    data: DetectBlockingTasksInput,
) -> dict[str, Any]:
    """检测阻断任务"""
    blocking = await pg_detect_blocking_tasks(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )

    by_severity: dict[str, int] = {}
    for t in blocking:
        sev = t.get("blockingSeverity", "low")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    critical_blockers = by_severity.get("critical", 0)
    high_blockers = by_severity.get("high", 0)

    return {
        "tableId": data.table_id,
        "blockingTasks": blocking,
        "summary": {
            "total": len(blocking),
            "bySeverity": by_severity,
            "criticalBlockers": critical_blockers,
            "highBlockers": high_blockers,
            "needsImmediateAction": critical_blockers + high_blockers > 0,
            "topBlockers": blocking[:3] if blocking else [],
            "recommendations": _build_blocker_recommendations(blocking),
        },
    }


def _build_blocker_recommendations(blocking: list[dict[str, Any]]) -> list[str]:
    recs: list[str] = []
    for task in blocking[:5]:
        reasons = task.get("blockingReasons", [])
        title = task.get("title", "未知任务")
        for reason in reasons:
            recs.append(f"任务「{title}」: {reason}")
    if not recs:
        recs.append("当前无阻断任务")
    return recs


# ============ Tool 8: predict_project_delay ============

class PredictProjectDelayInput(BaseModel):
    """predict_project_delay 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")
    target_date: Optional[str] = Field(default=None, alias="targetDate")


class PredictProjectDelayOutput(BaseModel):
    """predict_project_delay 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    prediction: dict[str, Any]


async def handle_predict_project_delay(
    context: SkillExecutionContext,
    data: PredictProjectDelayInput,
) -> dict[str, Any]:
    """预测项目延期风险"""
    prediction = await pg_predict_project_delay(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )
    return {
        "tableId": data.table_id,
        "prediction": prediction,
    }


# ============ Skill Definitions ============

def build_get_overdue_tasks_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.task.overdue.get",
            version="1.0.0",
            title="Get Overdue Tasks",
            description=(
                "查询当前项目中的所有延期任务。自动计算每个任务的延期天数，"
                "按严重程度（轻微/中度/严重/紧急）分级，并提供处理建议。"
                "需要先通过 get_current_datetime 获取当前日期作为判断基准。"
            ),
            tags=["task", "overdue", "deadline", "analytics"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=GetOverdueTasksInput,
        output_model=GetOverdueTasksOutput,
        handler=handle_get_overdue_tasks,
    )


def build_calculate_project_progress_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.project.progress.calculate",
            version="1.0.0",
            title="Calculate Project Progress",
            description=(
                "统计项目整体进度。计算总任务数、完成率、延期率、各状态分布、"
                "即将到期任务数，并给出项目健康度评估（良好/一般/值得关注/紧急）。"
            ),
            tags=["project", "progress", "analytics", "health"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=CalculateProjectProgressInput,
        output_model=CalculateProjectProgressOutput,
        handler=handle_calculate_project_progress,
    )


def build_get_member_workload_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.member.workload.get",
            version="1.0.0",
            title="Get Member Workload",
            description=(
                "统计每个成员的工作负载。包含总任务数、已完成数、进行中数、"
                "延期数、即将到期数、负载评分、负载等级和完成率。"
                "用于 AI 进行资源分配分析和负载均衡建议。"
            ),
            tags=["member", "workload", "resource", "analytics"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=GetMemberWorkloadInput,
        output_model=GetMemberWorkloadOutput,
        handler=handle_get_member_workload,
    )


def build_detect_blocking_tasks_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.task.blocking.detect",
            version="1.0.0",
            title="Detect Blocking Tasks",
            description=(
                "检测当前项目中的阻断任务。自动识别以下阻断模式："
                "1) 被显式标记为阻断状态的任务；2) 依赖未完成前置任务的任务；"
                "3) 延期超过7天且无进度的任务；4) 缺少关键信息（负责人、截止日期）的任务。"
                "按阻断严重程度排序，输出处理建议。"
            ),
            tags=["task", "blocking", "dependency", "analytics", "risk"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=DetectBlockingTasksInput,
        output_model=DetectBlockingTasksOutput,
        handler=handle_detect_blocking_tasks,
    )


def build_predict_project_delay_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.project.delay.predict",
            version="1.0.0",
            title="Predict Project Delay",
            description=(
                "预测项目是否会延期。基于完成速度、剩余工作量、延期趋势、"
                "依赖链条分析，给出延期风险等级（极低/低/中/高/紧急）、"
                "风险评估分数、预计完成日期和具体建议行动。"
            ),
            tags=["project", "prediction", "risk", "analytics", "forecast"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=PredictProjectDelayInput,
        output_model=PredictProjectDelayOutput,
        handler=handle_predict_project_delay,
    )
