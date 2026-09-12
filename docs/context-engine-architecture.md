# QTable Context Engine

## 目标

Context Engine 为 QTable 当前的 AI/Skill/Agent Runtime 增加统一上下文闭环，自动聚合并注入以下八类上下文：

1. User Context
2. Project Context
3. Table Context
4. Task Context
5. Team Context
6. Organization Context
7. View Context
8. Session / Conversation Context

它同时支持：

- 长对话
- Multi-Agent
- Workflow
- Redis 热缓存
- PostgreSQL 持久化
- Context Compression
- Context Window Optimization

## 架构

```text
FastAPI / GraphQL Request
        |
        v
ContextBuildInput
        |
        v
Context Builder
  |- User / Workspace / Membership
  |- Table / View
  |- Conversation / Session
  |- Workflow / Agent Stack
        |
        +--> Redis Cache
        +--> PostgreSQL Snapshot + Session Store
        |
        v
Context Injector
  |- ToolRouterRequest
  |- ToolContextState
  |- ToolAdapter runtime.agentContext
        |
        v
PydanticAI Agent / Skill Runtime / Workflow
```

## 数据模型

核心 Pydantic 模型定义在：

- `app/context_engine/models.py`

关键对象：

- `ContextBuildInput`: 从请求、Header、会话中抽取上下文输入
- `AgentContext`: 聚合后的完整业务上下文快照
- `SessionContext`: 当前会话和 cache/session state
- `ConversationContext`: 最近对话 + 压缩摘要
- `ContextSummary`: 注入给 Agent 的紧凑上下文文本
- `ContextWindow`: 当前上下文窗口占用估算

## Redis Session 设计

前缀配置：

- `context-engine:session:{scope_key}`
- `context-engine:snapshot:{sha256}`

用途：

- Session Key 保存热路径会话状态
- Snapshot Key 保存可复用的聚合上下文快照

TTL：

- `CONTEXT_SESSION_TTL_SECONDS`
- `CONTEXT_CACHE_TTL_SECONDS`

建议 Redis Value：

```json
{
  "id": "session_uuid",
  "sessionKey": "user:1:workspace:wkbDefault:session:conv_xxx",
  "lastContextHash": "sha256",
  "expiresAt": "2026-05-15T12:00:00+00:00"
}
```

## PostgreSQL Schema

SQLAlchemy 模型：

- `app/models/context_session.py`
- `app/models/context_snapshot.py`

`context_sessions` 用于保存：

- 会话主键
- user/workspace/conversation/project/table/view/task/team/organization/workflow/agent 关联
- session state
- memory summary
- 最近一次 context hash

`context_snapshots` 用于保存：

- 每次构建出的完整上下文快照
- 上下文摘要
- 压缩元数据
- trace id

## Context Builder

实现位置：

- `app/context_engine/builder.py`

能力：

- 优先从 Redis 读取聚合快照
- 从 PostgreSQL 读取用户、工作区、成员角色、会话、消息、表、视图
- 对长对话执行 tail-window + heuristic summary 压缩
- 生成 `ContextSummary` 与 `ContextWindow`
- 将 Session 与 Snapshot 回写到 Redis/PostgreSQL

## Context Injector

实现位置：

- `app/context_engine/injector.py`

注入目标：

- `ToolRouterRequest`
- `ToolContextState`
- `ToolAdapterContext.runtime.agentContext`

注入字段：

- `sessionId`
- `conversationId`
- `workspaceId`
- `projectId`
- `tableIds`
- `viewId`
- `taskId`
- `teamId`
- `organizationId`
- `workflow`
- `agentStack`
- `contextSummary`
- `contextSnapshot`

## 生命周期

1. FastAPI/GraphQL 收到请求
2. 从 Header/Body 提取 `ContextBuildInput`
3. Context Builder 查 Redis 快照
4. 若 miss，则聚合 PostgreSQL/业务上下文
5. 压缩长对话，生成摘要与窗口预算
6. 保存 Session 和 Snapshot
7. Context Injector 将摘要和完整快照注入 Tool Router
8. Tool Router 继续传递给 Tool Adapter / Skill Runtime
9. 下一轮对话通过 session/conversation 继续复用

## 示例代码

FastAPI 入口注入示例：

```python
from app.context_engine import ContextBuildInput, context_injector

injected_request, agent_context = await context_injector.build_and_inject(
    db=db,
    request=router_request,
    build_input=ContextBuildInput(
        source="api.ai.chat",
        userId=user_id,
        sessionId=session_id,
        conversationId=conversation_id,
        workspaceId=workspace_id,
        tableIds=table_ids,
        viewId=view_id,
        workflowId=workflow_id,
        message=question,
        locale="zh-CN",
        timezone="Asia/Shanghai",
    ),
)
```

Tool Router 调用示例：

```python
response = await tool_router_service.run(
    db=db,
    user_id=user_id,
    request=injected_request,
    agent_context=agent_context,
)
```

Tool Adapter 侧读取上下文：

```python
agent_context = adapter_context.runtime.get("agentContext", {})
context_summary = adapter_context.runtime.get("contextSummary", {})
```
