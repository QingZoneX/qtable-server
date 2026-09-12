"""
Task Management - Integration & AI Runtime

将 Task Management Tool System 集成到现有 AI Runtime。
提供上下文注入、智能工具选择、执行链编排。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.skills.contracts import SkillContext, SkillManifestEntry
from app.skills.runtime import SkillExecutionContext, SkillRegistry
from app.skills.task_management.registry import build_task_management_registry, TASK_MANAGEMENT_TOOL_NAMES
from app.skills.task_management.redis_cache import (
    TaskManagementCache,
    get_task_management_cache,
    cached_tool_call,
    CacheTTL,
)
from app.skills.task_management.contracts import TaskManagementToolLayer

logger = logging.getLogger(__name__)


class TaskManagementRuntime:
    """
    任务管理工具运行时集成层。

    职责:
    1. 合并默认 Skill Registry 和 Task Management Registry
    2. 注入环境上下文 (当前时间、用户、工作空间)
    3. 智能工具选择推荐
    4. 缓存管理
    5. 执行链编排
    """

    def __init__(self) -> None:
        self._registry: SkillRegistry | None = None
        self._cache: TaskManagementCache | None = None

    @property
    def cache(self) -> TaskManagementCache:
        if self._cache is None:
            self._cache = get_task_management_cache()
        return self._cache

    def get_full_registry(self) -> SkillRegistry:
        """获取包含所有工具的完整注册表"""
        if self._registry is None:
            from app.skills.runtime import build_default_registry
            self._registry = build_default_registry()
            tm_registry = build_task_management_registry()
            for manifest in tm_registry.list_manifest():
                definition = tm_registry.get(manifest.metadata.name)
                if definition:
                    self._registry.register(definition)
        return self._registry

    def get_task_management_manifests(self) -> list[SkillManifestEntry]:
        """获取仅任务管理工具的清单"""
        registry = build_task_management_registry()
        return registry.list_manifest()

    def build_injected_context(
        self,
        execution_context: SkillExecutionContext,
        *,
        inject_datetime: bool = True,
        inject_user: bool = True,
        inject_workspace: bool = True,
    ) -> dict[str, Any]:
        """
        构建注入的环境上下文。

        AI Agent 在调用 Task Management 工具前需要这些信息。
        """
        context = execution_context.context
        injected: dict[str, Any] = {
            "locale": context.locale,
            "timezone": context.timezone,
            "sessionId": context.session_id,
            "conversationId": context.conversation_id,
            "agentId": context.agent_id,
            "traceId": context.trace_id,
        }

        if inject_datetime:
            from datetime import datetime, timezone
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(context.timezone or "Asia/Shanghai")
            except Exception:
                tz = None
            now = datetime.now(tz or timezone.utc)
            injected["currentDatetime"] = {
                "iso8601": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "dayOfWeek": now.strftime("%A"),
                "unixTimestamp": now.timestamp(),
            }

        if inject_user and context.user_id:
            injected["currentUser"] = {
                "userId": context.user_id,
            }

        if inject_workspace and context.workspace_id:
            injected["currentWorkspace"] = {
                "workspaceId": context.workspace_id,
            }

        return injected

    def recommend_tool_chain(self, user_intent: str) -> list[str]:
        """
        根据用户意图推荐工具链。

        典型流程:
        1. 环境感知 (总是优先)
        2. Schema 理解
        3. 领域分析
        4. 执行建议
        """
        intent_lower = user_intent.lower()

        # 基础链 (环境感知)
        base_chain = [
            TASK_MANAGEMENT_TOOL_NAMES["get_current_datetime"],
        ]

        # 延期分析
        if any(kw in intent_lower for kw in ["延期", "overdue", "过期", "超期", "截止"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["get_overdue_tasks"],
            ]

        # 进度分析
        if any(kw in intent_lower for kw in ["进度", "progress", "完成率", "状态"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["calculate_project_progress"],
            ]

        # 负载分析
        if any(kw in intent_lower for kw in ["负载", "工作量", "workload", "分配", "谁"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["get_member_workload"],
            ]

        # 阻塞分析
        if any(kw in intent_lower for kw in ["阻塞", "阻断", "blocked", "卡住", "依赖"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["detect_blocking_tasks"],
            ]

        # 风险评估
        if any(kw in intent_lower for kw in ["风险", "预测", "risk", "delay", "能不能按时", "能否完成"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["calculate_project_progress"],
                TASK_MANAGEMENT_TOOL_NAMES["predict_project_delay"],
            ]

        # 执行计划
        if any(kw in intent_lower for kw in ["计划", "plan", "方案", "执行", "下一步", "怎么做"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["get_overdue_tasks"],
                TASK_MANAGEMENT_TOOL_NAMES["detect_blocking_tasks"],
                TASK_MANAGEMENT_TOOL_NAMES["get_member_workload"],
                TASK_MANAGEMENT_TOOL_NAMES["create_execution_plan"],
            ]

        # 综合健康检查
        if any(kw in intent_lower for kw in ["健康", "概览", "总结", "summary", "overview", "整体"]):
            return base_chain + [
                TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
                TASK_MANAGEMENT_TOOL_NAMES["calculate_project_progress"],
                TASK_MANAGEMENT_TOOL_NAMES["get_overdue_tasks"],
                TASK_MANAGEMENT_TOOL_NAMES["get_member_workload"],
                TASK_MANAGEMENT_TOOL_NAMES["detect_blocking_tasks"],
                TASK_MANAGEMENT_TOOL_NAMES["predict_project_delay"],
            ]

        # 默认: 基础报告
        return base_chain + [
            TASK_MANAGEMENT_TOOL_NAMES["describe_table_schema"],
            TASK_MANAGEMENT_TOOL_NAMES["calculate_project_progress"],
        ]

    def get_tool_info(self, tool_name: str) -> dict[str, Any] | None:
        """获取工具的详细信息"""
        registry = build_task_management_registry()
        definition = registry.get(tool_name)
        if definition is None:
            return None
        manifest = definition.manifest()
        return {
            "name": manifest.metadata.name,
            "title": manifest.metadata.title,
            "description": manifest.metadata.description,
            "tags": manifest.metadata.tags,
            "sideEffect": manifest.metadata.side_effect.value,
            "inputSchema": manifest.input_schema,
            "outputSchema": manifest.output_schema,
            "layer": _guess_layer(tool_name).value,
        }


def _guess_layer(tool_name: str) -> TaskManagementToolLayer:
    """根据工具名称猜测所属分层"""
    if ".env." in tool_name:
        return TaskManagementToolLayer.ENVIRONMENT
    if ".schema." in tool_name:
        return TaskManagementToolLayer.SCHEMA
    if ".workflow." in tool_name:
        return TaskManagementToolLayer.WORKFLOW
    return TaskManagementToolLayer.DOMAIN_INTELLIGENCE


# 全局运行时实例
_task_management_runtime: TaskManagementRuntime | None = None


def get_task_management_runtime() -> TaskManagementRuntime:
    global _task_management_runtime
    if _task_management_runtime is None:
        _task_management_runtime = TaskManagementRuntime()
    return _task_management_runtime
