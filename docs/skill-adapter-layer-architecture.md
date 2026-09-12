# QTable Skill Adapter Layer

## 1. 目标

在当前已有的：

- `Skill SDK`
- `Skill Registry`
- `PydanticAI Agent`

之上，新增统一的 `Skill Adapter Layer`，实现：

1. 所有 Skill 自动转换为 `PydanticAI Tool`
2. 自动生成 Tool Schema
3. 自动生成 JSON Schema
4. 自动注册 Tool
5. 支持权限校验
6. 支持 Tool Context
7. 支持 Tool Middleware
8. 支持 Tool 生命周期 Hook
9. 支持 Tool Logging
10. 支持 Tool Metrics

同时为未来 `Workflow` 与 `MCP` 保留兼容扩展位。

## 2. 分层架构

```text
FastAPI / GraphQL / Workflow / MCP Gateway
  -> Skill Registry Service
  -> Skill Runtime
  -> Skill Adapter Layer
      -> Schema Compiler
      -> Tool Adapter Registry
      -> Context Injection
      -> Tool Executor
      -> Middleware Chain
      -> Lifecycle Hooks
      -> Logging / Metrics
  -> PydanticAI Agent
  -> PostgreSQL / Redis / Domain Services
```

核心职责：

- `Skill Runtime`
  - 负责输入校验、权限兜底、确认流、Skill Handler 执行。
- `Skill Adapter Layer`
  - 负责把 `SkillDefinition` 编译成 `ToolSpec`，并生成 PydanticAI 可消费的 Tool callable。
- `Tool Adapter Registry`
  - 负责统一注册、索引和暴露 Skill -> Tool 的映射关系。
- `Tool Executor`
  - 负责执行链路编排、Middleware、Lifecycle Hook、Logging、Metrics、Retry。

## 3. Skill -> Tool 转换机制

当前实现路径：

```text
SkillDefinition
  -> manifest()
  -> ToolSchemaBundle
  -> PydanticAIToolSpec
  -> ToolAdapterRegistry
  -> PydanticAI callable tools
```

对应代码：

- `app/tool_adapter/schema.py`
  - 统一生成 `jsonSchema / openaiToolSchema / mcpToolSchema / pydanticAiSchema`
- `app/tool_adapter/registry.py`
  - 自动把 `SkillDefinition` 注册为 `PydanticAIToolSpec`
- `app/tool_adapter/contracts.py`
  - 定义 `PydanticAIToolSpec`、`ToolSchemaBundle`、`ToolExecutionRequest`

### 生成结果

每个 Skill 会自动派生出：

1. `JSON Schema`
   - 来自 `input_model.model_json_schema()`
2. `OpenAI Tool Schema`
   - 用于 OpenAI / DeepSeek function calling
3. `MCP Tool Schema`
   - 用于未来 MCP transport
4. `PydanticAI Schema`
   - 作为内部 Adapter 元数据和调试输出

## 4. Tool Middleware

当前已落地：

- `LoggingToolMiddleware`
  - 记录工具调用开始、结束、异常
- `MetricsToolMiddleware`
  - 记录调用次数、状态、耗时

位置：

- `app/tool_adapter/middleware.py`

执行顺序：

```text
ToolExecutionRequest
  -> LoggingToolMiddleware
  -> MetricsToolMiddleware
  -> SkillRuntime.invoke()
```

后续可继续扩展：

- `PermissionGuardMiddleware`
- `RateLimitMiddleware`
- `CircuitBreakerMiddleware`
- `AuditMiddleware`
- `WorkflowCheckpointMiddleware`

## 5. Context Injection

当前由 `build_tool_adapter_context()` 统一注入：

- Skill Context
- Runtime Context
- Request Context
- State
- Metrics

位置：

- `app/tool_adapter/context.py`

注入内容示例：

```json
{
  "skill": {
    "skillName": "qtable.record.create",
    "functionName": "qtable_record_create",
    "skillContext": {
      "user_id": 1,
      "workspace_id": "wkbDefault",
      "trace_id": "trace_xxx",
      "origin": "agent"
    }
  },
  "runtime": {
    "traceId": "trace_xxx",
    "runtimeMode": "router",
    "workspaceId": "wkbDefault",
    "tableIds": ["dstDefault"]
  },
  "request": {
    "arguments": {
      "tableId": "dstDefault"
    },
    "confirmed": false
  }
}
```

这保证：

- Tool 入参不混入认证信息
- Workflow 可序列化上下文
- MCP / Multi-Agent 可共享最小执行上下文

## 6. Tool Executor

位置：

- `app/tool_adapter/executor.py`

职责：

- 执行 Middleware 链
- 调用 `SkillRuntime.invoke()`
- 处理 Retry
- 触发生命周期 Hook
- 汇总耗时和执行结果

生命周期阶段：

- `before_execute`
- `after_execute`
- `requires_confirmation`
- `retry`
- `failed`

### Retry 策略

默认规则：

- 仅对 `INVALID_INPUT`、`EXECUTION_FAILED` 重试
- 达到最大次数停止
- 已确认且非幂等副作用 Skill 不再重试

## 7. Tool Registry

位置：

- `app/tool_adapter/registry.py`

职责：

- 从 `SkillRegistry` 自动扫描 Skill
- 生成 `ToolSpec`
- 自动完成 Tool 名规范化
- 暴露 `build_pydantic_ai_tools()`

关键能力：

- `get_by_skill_name()`
- `get_by_function_name()`
- `list_specs()`
- `build_pydantic_ai_tools()`

## 8. Logging 与 Metrics

### Logging

已落地两层日志：

- `LoggingToolMiddleware`
- `LoggingLifecycleHook`

用途：

- 运行态日志
- 生命周期事件日志
- 便于后续对接审计中心 / OpenTelemetry

### Metrics

已实现三种指标 Sink：

- `NoOpToolMetricsSink`
- `InMemoryToolMetricsSink`
- `RedisToolMetricsSink`

位置：

- `app/tool_adapter/hooks.py`

推荐生产扩展：

- Redis 做实时聚合
- PostgreSQL 做审计明细
- Prometheus / OTEL 做指标采集

## 9. 权限校验

权限执行仍由 `SkillRuntime.authorize()` 统一兜底，Adapter 层不重复定义业务权限模型。

这带来两个好处：

1. `Router / Workflow / MCP` 共用同一套授权边界
2. 权限逻辑不被 Prompt 或 Tool 包装层绕过

当前闭环：

- Tool Executor -> `SkillRuntime.invoke()`
- `SkillRuntime.invoke()` -> `authorize()`
- 表级资源权限继续复用现有 `workspace.permissions`

## 10. 与现有系统的接入点

### Skill Registry

`app/services/skill_registry.py` 已改为统一调用 Adapter 层的 Schema 生成器：

- OpenAI Tool Schema 不再独立拼装
- MCP Tool Schema 不再独立拼装

### PydanticAI Agent

`app/services/ai_tool_router.py` 已改为：

1. 从 `SkillRegistry` 构建 `runtime_registry`
2. 用 `ToolAdapterRegistry` 自动编译 Tool
3. 用 `ToolExecutor` 统一执行 Skill
4. 把执行结果回填到 `ToolExecutionTrace`

## 11. 目录结构

```text
app/
  tool_adapter/
    __init__.py
    contracts.py
    schema.py
    context.py
    middleware.py
    hooks.py
    executor.py
    registry.py
  skills/
    contracts.py
    runtime.py
  services/
    skill_registry.py
    ai_tool_router.py
  api/
    skill_runtime.py
    ai.py

docs/
  skill-adapter-layer-architecture.md
```

## 12. 示例代码

### 12.1 Skill 自动编译为 Tool

```python
from app.tool_adapter import ToolAdapterRegistry

tool_registry = ToolAdapterRegistry.from_runtime_registry(
    runtime_registry,
    skill_names=["qtable.table.describe", "qtable.record.create"],
)

tools = tool_registry.build_pydantic_ai_tools(
    run_context_annotation=RunContext[AgentRuntimeDeps],
    invoke_handler=self._invoke_tool_spec,
)
```

### 12.2 Tool Executor

```python
from app.tool_adapter import (
    InMemoryToolMetricsSink,
    LoggingLifecycleHook,
    LoggingToolMiddleware,
    MetricsLifecycleHook,
    MetricsToolMiddleware,
    ToolExecutor,
)

metrics_sink = InMemoryToolMetricsSink()
executor = ToolExecutor(
    runtime,
    middlewares=[
        LoggingToolMiddleware(logger),
        MetricsToolMiddleware(metrics_sink),
    ],
    hooks=[
        LoggingLifecycleHook(logger),
        MetricsLifecycleHook(metrics_sink),
    ],
)
```

### 12.3 Context Injection

```python
from app.tool_adapter import build_tool_adapter_context

adapter_context = build_tool_adapter_context(
    execution_context=execution_context,
    skill_name=spec.skill_name,
    function_name=spec.function_name,
    runtime_context={"traceId": trace_id, "workspaceId": workspace_id},
    request_context={"arguments": arguments, "confirmed": confirmed},
    state={"metadata": spec.manifest.metadata.model_dump(mode="json")},
)
```

## 13. 面向 Workflow 与 MCP 的扩展建议

### Workflow

建议继续补：

- `ToolRun` 持久化表
- 节点级 `resumeToken / checkpoint`
- `WorkflowToolMiddleware`
- 异步任务队列与 Redis 状态存储

### MCP

当前基础已具备：

- `mcpToolSchema`
- Tool Context 可序列化
- Tool Executor 可替换 transport

后续只需新增：

- `MCPTransportAdapter`
- `MCPToolExecutor`
- `MCP Session Context`

## 14. 结论

这版 `Skill Adapter Layer` 已完成你要的闭环基础设施：

- Skill 自动转 PydanticAI Tool
- Tool / JSON Schema 自动生成
- Tool 自动注册
- Tool Context 注入
- Middleware
- Lifecycle Hook
- Logging
- Metrics
- 保留 Workflow / MCP 扩展位

并且是直接集成到当前 QTable 代码结构中，而不是独立的概念稿。
