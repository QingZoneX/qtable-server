"""
Layer 4: Workflow Tools - 工作流编排

create_execution_plan: 基于项目状态分析结果，自动生成可执行的行动计划。
支持优先级排序、资源分配建议、依赖关系解析、并行执行组识别。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.skills.contracts import (
    SkillMetadata,
    SkillSideEffect,
)
from app.skills.runtime import (
    SkillDefinition,
    SkillExecutionContext,
)


class CreateExecutionPlanInput(BaseModel):
    """create_execution_plan 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId")
    strategy: str = Field(
        default="balanced",
        description="执行策略: urgent_first (紧急优先) / deadline_first (截止日期优先) / balanced (平衡) / workload_balance (负载均衡)",
    )
    max_parallel_tasks: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="maxParallelTasks",
    )
    target_date: Optional[str] = Field(
        default=None,
        alias="targetDate",
        description="目标完成日期（ISO 8601 格式），不指定则根据任务自动计算",
    )


class ExecutionPlanTask(BaseModel):
    """执行计划中的单个任务"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: str = Field(alias="taskId")
    title: str
    priority: int = 0
    suggested_start: str = Field(alias="suggestedStart")
    suggested_end: str = Field(alias="suggestedEnd")
    assigned_to: Optional[str] = Field(default=None, alias="assignedTo")
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    block_by: list[str] = Field(default_factory=list, alias="blockBy")
    parallel_group: int = Field(default=0, alias="parallelGroup")
    estimated_hours: float = Field(default=0.0, alias="estimatedHours")
    reason: str = ""


class ExecutionPlanPhase(BaseModel):
    """执行阶段"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    phase_id: str = Field(alias="phaseId")
    phase_name: str = Field(alias="phaseName")
    phase_order: int = Field(alias="phaseOrder")
    tasks: list[ExecutionPlanTask]
    parallel_groups: int = Field(alias="parallelGroups")
    estimated_days: float = Field(alias="estimatedDays")


class CreateExecutionPlanOutput(BaseModel):
    """create_execution_plan 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    plan_id: str = Field(alias="planId")
    strategy: str
    total_tasks: int = Field(alias="totalTasks")
    phases: list[dict[str, Any]]
    critical_path: list[str] = Field(default_factory=list, alias="criticalPath")
    estimated_total_days: float = Field(alias="estimatedTotalDays")
    estimated_completion: str = Field(alias="estimatedCompletion")
    risk_warnings: list[str] = Field(default_factory=list, alias="riskWarnings")
    summary: str


async def handle_create_execution_plan(
    context: SkillExecutionContext,
    data: CreateExecutionPlanInput,
) -> dict[str, Any]:
    """
    基于项目状态生成执行计划。

    执行流程:
    1. 收集所有未完成任务
    2. 按策略排序（紧急优先/截止日期优先/平衡/负载均衡）
    3. 解析依赖关系，识别可并行执行的任务组
    4. 分配资源，避免超载
    5. 构建阶段化执行计划
    6. 识别关键路径
    7. 评估风险
    """
    from app.skills.task_management.postgres_queries import (
        pg_get_overdue_tasks,
        pg_calculate_project_progress,
        pg_get_member_workload,
        pg_detect_blocking_tasks,
    )

    # 1. 收集数据
    progress = await pg_calculate_project_progress(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )
    overdue = await pg_get_overdue_tasks(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
        status_not_in=["已完成", "completed", "done", "closed"],
        limit=200,
    )
    workloads = await pg_get_member_workload(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )
    blockers = await pg_detect_blocking_tasks(
        db=context.db,
        table_id=data.table_id,
        workspace_id=context.context.workspace_id,
        user_id=context.context.user_id,
    )

    # 2. 按策略排序
    sorted_tasks = _sort_by_strategy(overdue, blockers, workloads, data.strategy, data.max_parallel_tasks)

    # 3. 构建阶段
    phases = _build_phases(sorted_tasks, data.max_parallel_tasks)

    # 4. 关键路径
    critical_path = _identify_critical_path(sorted_tasks)

    # 5. 预估时间
    total_days = sum(p.get("estimatedDays", 1) for p in phases)
    today = date.today()
    estimated_completion = today + timedelta(days=int(total_days))

    # 6. 风险评估
    risk_warnings: list[str] = []
    health = progress.get("health", "fair")
    if health in ("critical", "concerning"):
        risk_warnings.append(f"项目健康度为 {health}，执行计划可能被延期任务影响")
    critical_blockers = [b for b in blockers if b.get("blockingSeverity") == "critical"]
    if critical_blockers:
        risk_warnings.append(f"存在 {len(critical_blockers)} 个紧急阻断任务，需优先解决")
    overloaded_ids = [w["memberId"] for w in workloads if w.get("loadLevel") == "overloaded"]
    if overloaded_ids:
        risk_warnings.append(f"成员 {overloaded_ids} 负载过重，可能影响执行效率")
    if total_days > 30:
        risk_warnings.append(f"预估完成需 {total_days} 天，周期较长，建议考虑更多并行")

    # 7. 摘要
    summary = _generate_plan_summary(
        strategy=data.strategy,
        total_tasks=len(sorted_tasks),
        phases=phases,
        total_days=total_days,
        estimated_completion=estimated_completion.isoformat(),
        risk_warnings=risk_warnings,
    )

    return {
        "tableId": data.table_id,
        "planId": f"plan_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "strategy": data.strategy,
        "totalTasks": len(sorted_tasks),
        "phases": phases,
        "criticalPath": critical_path,
        "estimatedTotalDays": round(total_days, 1),
        "estimatedCompletion": estimated_completion.isoformat(),
        "riskWarnings": risk_warnings,
        "summary": summary,
    }


def _sort_by_strategy(
    overdue: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    strategy: str,
    max_parallel: int,
) -> list[dict[str, Any]]:
    """按策略排序任务"""

    # 合并数据
    all_tasks = list(overdue)
    blocker_ids = {b.get("recordId") for b in blockers}

    # 标记阻断任务
    for task in all_tasks:
        task["_is_blocker"] = task.get("recordId") in blocker_ids

    if strategy == "urgent_first":
        # 紧急优先: 延期严重度 > 阻断状态 > 延期天数
        severity_order = {"critical": 0, "severe": 1, "moderate": 2, "minor": 3}
        all_tasks.sort(key=lambda t: (
            severity_order.get(t.get("severity", "minor"), 4),
            0 if t.get("_is_blocker") else 1,
            -t.get("overdueDays", 0),
        ))
    elif strategy == "deadline_first":
        # 截止日期优先: 距截止日期近的优先
        all_tasks.sort(key=lambda t: (
            0 if t.get("_is_blocker") else 1,
            t.get("dueDate", "9999-12-31"),
            -t.get("overdueDays", 0),
        ))
    elif strategy == "workload_balance":
        # 负载均衡: 尽量分散到负载低的成员
        member_load = {w["memberId"]: w.get("loadScore", 0) for w in workloads}
        all_tasks.sort(key=lambda t: (
            0 if t.get("_is_blocker") else 1,
            member_load.get(t.get("assignee", ""), 50),
            -t.get("overdueDays", 0),
        ))
    else:
        # balanced: 综合考虑
        severity_order = {"critical": 0, "severe": 1, "moderate": 2, "minor": 3}
        all_tasks.sort(key=lambda t: (
            0 if t.get("_is_blocker") else 1,
            severity_order.get(t.get("severity", "minor"), 4),
        ))

    return all_tasks


def _build_phases(
    sorted_tasks: list[dict[str, Any]],
    max_parallel: int,
) -> list[dict[str, Any]]:
    """构建分阶段执行计划"""
    phases: list[dict[str, Any]] = []
    today = date.today()
    current_date = today
    task_index = 0
    phase_order = 1

    # 阶段定义
    phase_definitions = [
        ("紧急处理", ["critical", "severe"]),
        ("阻滞解决", ["blocking"]),
        ("高优跟进", ["moderate"]),
        ("常规推进", ["minor"]),
    ]

    for phase_name, severities in phase_definitions:
        phase_tasks: list[dict[str, Any]] = []
        task_slots = max_parallel
        pending: list[dict[str, Any]] = []

        for task in sorted_tasks:
            if task in phase_tasks:
                continue
            if severities == ["blocking"]:
                if task.get("_is_blocker") and task not in phase_tasks:
                    pending.append(task)
            elif task.get("severity") in severities:
                pending.append(task)

        if not pending:
            continue

        # 分配并行组
        parallel_groups = 0
        while pending and task_slots > 0:
            group_tasks = pending[:task_slots]
            pending = pending[task_slots:]
            parallel_groups += 1

            phase_date = current_date
            for i, task in enumerate(group_tasks):
                task_index += 1
                overdue_days = task.get("overdueDays", 0)
                estimated_hours = max(2, overdue_days * 4)
                duration_days = max(1, int(estimated_hours / 6))

                phase_tasks.append({
                    "taskId": task.get("recordId", ""),
                    "title": task.get("title", ""),
                    "priority": task_index,
                    "suggestedStart": phase_date.isoformat(),
                    "suggestedEnd": (phase_date + timedelta(days=duration_days)).isoformat(),
                    "assignedTo": task.get("assignee"),
                    "dependsOn": task.get("dependsOn", []),
                    "blockBy": task.get("blockedBy", []),
                    "parallelGroup": parallel_groups,
                    "estimatedHours": round(estimated_hours, 1),
                    "reason": f"延期 {overdue_days} 天" if task.get("_is_blocker") else f"严重程度: {task.get('severity')}",
                })

            current_date += timedelta(days=1)

        if phase_tasks:
            phases.append({
                "phaseId": f"phase_{phase_order}",
                "phaseName": phase_name,
                "phaseOrder": phase_order,
                "tasks": phase_tasks,
                "parallelGroups": parallel_groups,
                "estimatedDays": (current_date - today).days or 1,
            })
            phase_order += 1

    return phases


def _identify_critical_path(sorted_tasks: list[dict[str, Any]]) -> list[str]:
    """识别关键路径（简化版）"""
    critical: list[str] = []
    for task in sorted_tasks:
        if task.get("severity") in ("critical", "severe") or task.get("_is_blocker"):
            critical.append(task.get("recordId", ""))
    return critical[:10]


def _generate_plan_summary(
    strategy: str,
    total_tasks: int,
    phases: list[dict[str, Any]],
    total_days: float,
    estimated_completion: str,
    risk_warnings: list[str],
) -> str:
    """生成计划摘要"""
    strategy_labels = {
        "urgent_first": "紧急优先",
        "deadline_first": "截止日期优先",
        "balanced": "平衡策略",
        "workload_balance": "负载均衡",
    }
    phase_descriptions = [
        f"阶段 {p['phaseOrder']}（{p['phaseName']}）：{len(p['tasks'])} 个任务"
        for p in phases
    ]

    warning_text = ""
    if risk_warnings:
        warning_text = "\n⚠️ 风险提示：\n" + "\n".join(f"  - {w}" for w in risk_warnings)

    return (
        f"执行计划已生成。使用 {strategy_labels.get(strategy, strategy)}。\n"
        f"共 {total_tasks} 个任务，分为 {len(phases)} 个阶段：\n"
        + "\n".join(f"  - {d}" for d in phase_descriptions) +
        f"\n\n预估总工期：{total_days:.1f} 天\n"
        f"预计完成日期：{estimated_completion}"
        + warning_text
    )


# ============ Tool 9: create_execution_plan ============

def build_create_execution_plan_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.workflow.execution_plan.create",
            version="1.0.0",
            title="Create Execution Plan",
            description=(
                "基于项目当前状态自动生成可执行的行动计划。"
                "整合延期任务、阻断任务、成员工作负载等分析结果，"
                "按指定策略（紧急优先/截止日期优先/平衡/负载均衡）排序，"
                "构建分阶段执行计划，包含建议开始/结束时间、并行组标识、"
                "资源分配、关键路径识别和风险评估。"
            ),
            tags=["workflow", "planning", "execution", "strategy"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                {
                    "resource": "table",
                    "action": "read",
                    "target_param": "table_id",
                }
            ],
        ),
        input_model=CreateExecutionPlanInput,
        output_model=CreateExecutionPlanOutput,
        handler=handle_create_execution_plan,
    )
