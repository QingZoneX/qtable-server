"""
Task Management Agent Tool System

分层架构:
    Layer 1: Environment Tools     - 环境感知 (时间/用户/工作空间)
    Layer 2: Schema Tools          - 表结构感知
    Layer 3: Domain Intelligence   - 领域智能 (延期检测/进度/工作负载/阻断/风险预测)
    Layer 4: Workflow Tools        - 工作流编排 (执行计划生成)
    Layer 5: AI Runtime Tools      - 运行时工具 (上下文注入/工具组合)

企业级特性:
    - MCP Compatible (每个工具输出 mcpToolSchema)
    - LangGraph Compatible (每个工具可独立作为 graph node)
    - Multi-Agent Ready (支持 agent_id 隔离)
    - PydanticAI Native (全程类型安全)
"""
from app.skills.task_management.contracts import (
    TaskManagementToolLayer,
    TaskManagementToolMetadata,
    ToolDomain,
    ToolIntelligenceLevel,
)
from app.skills.task_management.registry import build_task_management_registry

__all__ = [
    "TaskManagementToolLayer",
    "TaskManagementToolMetadata",
    "ToolDomain",
    "ToolIntelligenceLevel",
    "build_task_management_registry",
]
