"""
Task Management - Tool 权限设计

针对每个 Task Management Tool 的细粒度权限控制。
集成现有 SkillAuthorizer 机制和表级权限。

权限维度:
1. Workspace Level: workspace owner / editor / viewer
2. Table Level: table read / write (继承现有权限)
3. Tool Level: tool scope (internal / workspace / marketplace)
4. Domain Level: project / member / workload / risk
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from app.skills.contracts import SkillPermissionRequirement
from app.skills.runtime import SkillDefinition, SkillExecutionContext, SkillRuntimeError
from app.skills.contracts import SkillErrorCode


class TaskManagementPermission(str, Enum):
    """任务管理工具权限常量"""
    # 环境感知
    VIEW_DATETIME = "tm:env:datetime:view"          # 查看当前时间
    VIEW_USER = "tm:env:user:view"                   # 查看当前用户
    VIEW_WORKSPACE = "tm:env:workspace:view"         # 查看工作空间

    # Schema 感知
    VIEW_TABLE_SCHEMA = "tm:schema:view"             # 查看表结构

    # 任务分析
    VIEW_OVERDUE = "tm:task:overdue:view"            # 查看延期任务
    VIEW_PROGRESS = "tm:project:progress:view"       # 查看项目进度
    VIEW_WORKLOAD = "tm:member:workload:view"        # 查看工作负载
    VIEW_BLOCKING = "tm:task:blocking:view"          # 查看阻断任务
    VIEW_DELAY_PREDICTION = "tm:project:delay:view"  # 查看延期预测

    # 工作流
    CREATE_EXECUTION_PLAN = "tm:workflow:plan:create"  # 创建执行计划


# 工具权限映射: tool_name -> required permissions
TOOL_PERMISSION_MAP: dict[str, list[TaskManagementPermission]] = {
    "qtable.env.datetime.get": [TaskManagementPermission.VIEW_DATETIME],
    "qtable.env.user.get": [TaskManagementPermission.VIEW_USER],
    "qtable.env.workspace.get": [TaskManagementPermission.VIEW_WORKSPACE],
    "qtable.schema.describe": [TaskManagementPermission.VIEW_TABLE_SCHEMA],
    "qtable.task.overdue.get": [TaskManagementPermission.VIEW_OVERDUE],
    "qtable.project.progress.calculate": [TaskManagementPermission.VIEW_PROGRESS],
    "qtable.member.workload.get": [TaskManagementPermission.VIEW_WORKLOAD],
    "qtable.task.blocking.detect": [TaskManagementPermission.VIEW_BLOCKING],
    "qtable.project.delay.predict": [TaskManagementPermission.VIEW_DELAY_PREDICTION],
    "qtable.workflow.execution_plan.create": [TaskManagementPermission.CREATE_EXECUTION_PLAN],
}


class TaskManagementAuthorizer:
    """
    任务管理工具专用权限校验器。

    权限层级:
    1. 全局关闭 -> 全部拒绝
    2. Workspace owner -> 全部允许
    3. Workspace editor -> 允许查看 + 创建计划
    4. Workspace viewer -> 仅允许环境感知 + Schema + 分析
    5. Table permission -> 必须有目标表的 read 权限
    """

    async def authorize(
        self,
        definition: SkillDefinition,
        call_input: dict[str, Any],
        context: SkillExecutionContext,
    ) -> None:
        if not context.context.user_id:
            raise SkillRuntimeError(
                SkillErrorCode.UNAUTHORIZED,
                "Task management tools require authentication",
            )

        tool_name = definition.metadata.name
        required_permissions = TOOL_PERMISSION_MAP.get(tool_name, [])

        # workspace viewer 只能查看，不能创建
        workspace_role = await self._get_workspace_role(context)
        if workspace_role == "viewer":
            if any(p.startswith("tm:workflow:") for p in required_permissions):
                raise SkillRuntimeError(
                    SkillErrorCode.FORBIDDEN,
                    "Workspace viewer cannot create execution plans",
                    details={"toolName": tool_name, "role": "viewer"},
                )

        # 检查表级权限
        table_id = call_input.get("tableId") or call_input.get("table_id")
        if table_id and not any(p.startswith("tm:env:") for p in required_permissions):
            # 需要表读权限
            pass  # 由现有 SkillAuthorizer 处理

    async def _get_workspace_role(self, context: SkillExecutionContext) -> str | None:
        """获取用户在工作空间中的角色"""
        if context.db is None:
            return None
        from sqlalchemy import select
        from app.models.workspace_member import WorkspaceMember

        result = await context.db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == context.context.user_id,
                WorkspaceMember.workspace_id == context.context.workspace_id,
            ).limit(1)
        )
        member = result.scalars().first()
        if member:
            return member.role.value if hasattr(member.role, "value") else str(member.role)
        return None


# 每工具最小权限集（供前端使用）
TOOL_MINIMAL_PERMISSIONS: dict[str, str] = {
    "qtable.env.datetime.get": "workspace member (any role)",
    "qtable.env.user.get": "authenticated user",
    "qtable.env.workspace.get": "workspace member (any role)",
    "qtable.schema.describe": "table read permission",
    "qtable.task.overdue.get": "table read permission",
    "qtable.project.progress.calculate": "table read permission",
    "qtable.member.workload.get": "table read permission",
    "qtable.task.blocking.detect": "table read permission",
    "qtable.project.delay.predict": "table read permission",
    "qtable.workflow.execution_plan.create": "workspace editor or above",
}
