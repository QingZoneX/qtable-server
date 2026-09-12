"""
Task Management - Tool Registry

将 10 个核心 Task Management Tools 注册到 SkillRegistry。
与现有的 build_default_registry() 无缝集成。
"""
from __future__ import annotations

from app.skills.runtime import SkillRegistry
from app.skills.task_management.environment import (
    build_get_current_datetime_definition,
    build_get_current_user_definition,
    build_get_current_workspace_definition,
)
from app.skills.task_management.schema_tools import (
    build_describe_table_schema_definition,
)
from app.skills.task_management.domain_intelligence import (
    build_calculate_project_progress_definition,
    build_detect_blocking_tasks_definition,
    build_get_member_workload_definition,
    build_get_overdue_tasks_definition,
    build_predict_project_delay_definition,
)
from app.skills.task_management.workflow import (
    build_create_execution_plan_definition,
)


def build_task_management_registry() -> SkillRegistry:
    """
    构建任务管理工具注册表。

    包含 10 个核心工具:

    Layer 1 - Environment (环境感知):
        1. qtable.env.datetime.get      - get_current_datetime
        2. qtable.env.user.get          - get_current_user
        3. qtable.env.workspace.get     - get_current_workspace

    Layer 2 - Schema (表结构感知):
        4. qtable.schema.describe       - describe_table_schema

    Layer 3 - Domain Intelligence (领域智能):
        5. qtable.task.overdue.get      - get_overdue_tasks
        6. qtable.project.progress.calculate - calculate_project_progress
        7. qtable.member.workload.get   - get_member_workload
        8. qtable.task.blocking.detect  - detect_blocking_tasks
        9. qtable.project.delay.predict - predict_project_delay

    Layer 4 - Workflow (工作流):
        10. qtable.workflow.execution_plan.create - create_execution_plan

    使用:
        registry = build_task_management_registry()
        manifests = registry.list_manifest()
    """
    registry = SkillRegistry()

    # Layer 1: Environment Tools
    registry.register(build_get_current_datetime_definition())
    registry.register(build_get_current_user_definition())
    registry.register(build_get_current_workspace_definition())

    # Layer 2: Schema Tools
    registry.register(build_describe_table_schema_definition())

    # Layer 3: Domain Intelligence Tools
    registry.register(build_get_overdue_tasks_definition())
    registry.register(build_calculate_project_progress_definition())
    registry.register(build_get_member_workload_definition())
    registry.register(build_detect_blocking_tasks_definition())
    registry.register(build_predict_project_delay_definition())

    # Layer 4: Workflow Tools
    registry.register(build_create_execution_plan_definition())

    return registry


# 工具名称常量（便于引用）
TASK_MANAGEMENT_TOOL_NAMES = {
    # Layer 1: Environment
    "get_current_datetime": "qtable.env.datetime.get",
    "get_current_user": "qtable.env.user.get",
    "get_current_workspace": "qtable.env.workspace.get",
    # Layer 2: Schema
    "describe_table_schema": "qtable.schema.describe",
    # Layer 3: Domain Intelligence
    "get_overdue_tasks": "qtable.task.overdue.get",
    "calculate_project_progress": "qtable.project.progress.calculate",
    "get_member_workload": "qtable.member.workload.get",
    "detect_blocking_tasks": "qtable.task.blocking.detect",
    "predict_project_delay": "qtable.project.delay.predict",
    # Layer 4: Workflow
    "create_execution_plan": "qtable.workflow.execution_plan.create",
}


def get_task_management_manifest() -> list[dict[str, object]]:
    """
    获取任务管理工具的完整清单 (带分层元数据)。

    返回格式供前端 AI Assistant 面板使用。
    """
    registry = build_task_management_registry()

    layer_order = {
        "environment": 1,
        "schema": 2,
        "domain_intelligence": 3,
        "workflow": 4,
        "ai_runtime": 5,
    }

    from app.skills.task_management.contracts import TaskManagementToolLayer, ToolDomain

    tool_name_to_layer = {
        "qtable.env.datetime.get": TaskManagementToolLayer.ENVIRONMENT,
        "qtable.env.user.get": TaskManagementToolLayer.ENVIRONMENT,
        "qtable.env.workspace.get": TaskManagementToolLayer.ENVIRONMENT,
        "qtable.schema.describe": TaskManagementToolLayer.SCHEMA,
        "qtable.task.overdue.get": TaskManagementToolLayer.DOMAIN_INTELLIGENCE,
        "qtable.project.progress.calculate": TaskManagementToolLayer.DOMAIN_INTELLIGENCE,
        "qtable.member.workload.get": TaskManagementToolLayer.DOMAIN_INTELLIGENCE,
        "qtable.task.blocking.detect": TaskManagementToolLayer.DOMAIN_INTELLIGENCE,
        "qtable.project.delay.predict": TaskManagementToolLayer.DOMAIN_INTELLIGENCE,
        "qtable.workflow.execution_plan.create": TaskManagementToolLayer.WORKFLOW,
    }

    manifests = registry.list_manifest()
    output: list[dict[str, object]] = []
    for m in manifests:
        layer = tool_name_to_layer.get(m.metadata.name, TaskManagementToolLayer.DOMAIN_INTELLIGENCE)
        output.append({
            "name": m.metadata.name,
            "title": m.metadata.title,
            "description": m.metadata.description,
            "tags": m.metadata.tags,
            "layer": layer.value,
            "layerOrder": layer_order.get(layer.value, 99),
            "sideEffect": m.metadata.side_effect.value,
            "inputSchema": m.input_schema,
            "outputSchema": m.output_schema,
        })

    output.sort(key=lambda t: (int(t.get("layerOrder", 99)), str(t.get("name", ""))))
    return output
