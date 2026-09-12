# QTable AI Tool Router

## 1. 目标

在现有 `Skill Runtime`、`Skill Registry` 和 `AI Assistant` 之上，新增一层真正可执行的 `AI Tool Router / Agent Layer`，让模型能够：

- 根据用户意图自动选择 Skill
- 自动提取参数
- 执行 Tool 并将结果回填给 LLM
- 支持多轮 Tool Calling
- 支持重试、权限、确认流和上下文管理
- 兼容 OpenAI Tool Calling / DeepSeek Function Calling / MCP
- 为未来 Multi-Agent 和长链路任务保留扩展位

本次落地代码：

- `app/services/ai_tool_router.py`
- `app/api/ai_router.py`
- `ui/src/lib/aiToolRouter.ts`
- `scripts/demo_ai_tool_router.py`

## 2. 总体架构

```text
React 19 Client / Workflow UI / Multi-Agent Orchestrator
  -> aiToolRouter.ts
  -> FastAPI Agent Layer (/api/ai/router/run)
  -> ToolRouterService
      -> Skill Matcher
      -> Prompt Builder
      -> OpenAI-Compatible LLM Adapter
      -> Tool Executor
      -> Tool Context Store
  -> Skill Registry Service
  -> Skill Runtime
      -> Authorizer
      -> Builtin Skills
      -> Workspace Skills
      -> HTTP Proxy / MCP / OpenAI-Compatible Proxy Skills
  -> QTable Domain Services / GraphQL / DB / Redis
```

分层职责：

1. `Skill Registry`
   - 负责 Skill 元数据、OpenAI Tool Schema、MCP Schema、动态加载。
2. `Tool Router`
   - 负责技能候选集筛选、Prompt 工程、多轮 LLM Tool Calling、结果回填。
3. `Tool Executor`
   - 负责 Skill Runtime 调用、重试、权限、确认流、错误标准化。
4. `Tool Context`
   - 负责跨轮持久化 `workspaceId / tableIds / variables / toolResults / notes`。
5. `Agent Layer`
   - 对外暴露 REST，未来可扩展 GraphQL mutation / workflow node / multi-agent gateway。

## 3. FastAPI Agent Layer

REST 入口：

- `POST /api/ai/router/run`

请求体核心字段：

```json
{
  "message": "帮我查看延期任务，并按负责人统计数量",
  "workspaceId": "wkbDefault",
  "tableIds": ["dstDefault"],
  "conversation": [],
  "toolContext": {
    "sessionId": "sess_001",
    "workspaceId": "wkbDefault",
    "tableIds": ["dstDefault"],
    "variables": {},
    "toolResults": [],
    "notes": [],
    "chainDepth": 0,
    "retryPolicy": {
      "maxAttempts": 2,
      "backoffMs": 350
    }
  },
  "allowedSkills": ["qtable.record.query", "qtable.table.describe"],
  "maxSteps": 6,
  "toolLimit": 12,
  "confirmed": false,
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai"
}
```

响应体核心字段：

```json
{
  "status": "completed",
  "answer": "共有 12 条延期任务，按负责人统计如下...",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "traceId": "trace_xxx",
  "steps": 2,
  "toolCalls": [],
  "toolContext": {},
  "usage": {
    "promptTokens": 1234,
    "completionTokens": 256,
    "totalTokens": 1490
  }
}
```

## 4. Tool Router 流程

```text
User Message
  -> Load AI Config
  -> Build OpenAI-Compatible Client
  -> Build Runtime Registry
  -> Skill Matching
  -> Build Tools + Prompt + Tool Context
  -> LLM Tool Calling
      -> Tool Executor
          -> Skill Runtime
              -> Authorizer
              -> Domain Skill
      -> Tool Result Backfill
  -> Multi-round Loop
  -> Final Answer
```

流程细节：

1. 加载当前用户的 AI 配置。
2. 根据 `provider` 选择 `OpenAI / DeepSeek / OpenAI-compatible` 适配器。
3. 从 `Skill Registry` 构建当前工作区可见的 Runtime Registry。
4. 使用 `Skill Matching` 先做候选 Skill 预筛选。
5. 把候选 Skill 转成 OpenAI Tool Schema。
6. 把 `toolContext`、对话历史和用户请求组装进 Prompt。
7. 由 LLM 决定是否调用 Tool。
8. Tool 执行结果以 `tool` message 回填模型。
9. 如果还需要更多 Tool，则继续下一轮。
10. 达到终止条件后输出最终回答。

## 5. Prompt Engineering

系统 Prompt 目标：

- 告诉模型自己是 `Tool Router / Agent`
- 强制先用真实 Tool，而不是臆测数据
- 明确写操作需要确认
- 明确工具失败时要修复参数或选择其他 Tool
- 明确可以多轮、多 Tool 链式调用

Prompt 组成：

1. `Router System Prompt`
   - 定义角色、能力边界和调用规则。
2. `Execution Context Prompt`
   - 注入 `workspaceId / tableIds / locale / timezone / toolResults / variables`。
3. `Conversation History`
   - 保留用户上下文和上轮工具结果。
4. `Current User Message`
   - 当前轮自然语言请求。

推荐原则：

- 不要把全量数据库内容塞进 Prompt。
- 只注入必要上下文，把数据获取交给 Tool。
- Tool 结果只保留近几轮摘要，超长结果做截断。
- Prompt 中明确要求“优先复用 toolContext，避免重复工具调用”。

## 6. Skill Matching 方案

采用 `Hybrid Matching`：

1. `规则/词法预筛选`
   - 从 `name/title/description/tags/input schema` 提取关键词。
   - 和用户请求、`tableIds`、`toolContext.notes` 做 token overlap 计算。
2. `LLM 最终决策`
   - 在候选 Skill 子集内，由模型决定具体调用哪个 Tool、何时调用、参数如何填写。

这样做的原因：

- 减少 token 消耗
- 降低模型在大工具集中的误选率
- 保留多 Skill 路由能力
- 兼容未来向量检索 / Embedding Ranking

未来可扩展为三阶段匹配：

1. Embedding Recall
2. Rule / Permission Filter
3. LLM Planner Selection

## 7. Tool Executor 设计

`Tool Executor` 负责：

- 参数解析
- Runtime 调用
- 错误处理
- 自动重试
- 权限控制
- 确认流处理
- 输出标准化

执行逻辑：

```text
LLM Tool Call
  -> parse arguments
  -> map functionName -> skillName
  -> SkillRuntime.invoke()
      -> authorize()
      -> validate input
      -> requires confirmation?
      -> execute handler
  -> normalize result
  -> append tool message back to LLM
```

### Retry 策略

当前自动重试策略：

- `INVALID_INPUT`
- `EXECUTION_FAILED`

自动重试条件：

- 未超过 `retryPolicy.maxAttempts`
- 该 Skill 不是已确认的非幂等写操作
- Runtime 返回的是可恢复型失败

后续建议：

- 区分 `transport retry` 和 `semantic retry`
- 为 HTTP Proxy / MCP 增加 circuit breaker
- 写操作引入幂等键，支持更安全的 retry

## 8. Tool Context 设计

`toolContext` 是跨轮传递的执行态，不属于 Tool 的业务参数。

结构：

```json
{
  "sessionId": "sess_001",
  "workspaceId": "wkbDefault",
  "tableIds": ["dstDefault"],
  "variables": {
    "targetStatus": "overdue"
  },
  "toolResults": [
    {
      "toolCallId": "call_1",
      "skillName": "qtable.record.query",
      "state": "completed",
      "summary": "qtable.record.query -> completed",
      "output": {},
      "error": null
    }
  ],
  "notes": [
    "Need only overdue tasks."
  ],
  "chainDepth": 1,
  "retryPolicy": {
    "maxAttempts": 2,
    "backoffMs": 350
  }
}
```

字段职责：

- `sessionId`
  - 标识一条工具链会话，便于未来持久化和审计。
- `workspaceId`
  - 用于多租户 / Multi-Agent 共享上下文。
- `tableIds`
  - 当前链路的主要数据域。
- `variables`
  - 保存推理出的结构化中间变量。
- `toolResults`
  - 保存最近几轮工具输出摘要。
- `notes`
  - 保存人类提示、规划备注或 agent handoff note。
- `chainDepth`
  - 记录长链路深度，便于中断与恢复。
- `retryPolicy`
  - 让不同链路能采用不同重试策略。

## 9. 权限控制

权限控制不在 LLM 层做，而是在 `Skill Runtime` 统一兜底：

- 未登录：`UNAUTHORIZED`
- 无资源权限：`FORBIDDEN`
- 输入不合法：`INVALID_INPUT`
- 技能不存在：`SKILL_NOT_FOUND`
- 底层失败：`EXECUTION_FAILED`

当前实现：

- `Tool Router` 只做技能选择和上下文编排
- 真正授权仍由 `SkillRuntime.authorize()` 完成
- 表级权限复用现有 `workspace.permissions` 能力

这保证：

- Router 可以演进
- 权限边界不被 Prompt 绕过
- Multi-Agent 共享同一执行安全模型

## 10. OpenAI / DeepSeek / MCP 兼容

兼容策略：

1. `OpenAI Tool Calling`
   - 直接向 `chat.completions.create(..., tools=[...])` 提交 Tool Schema。
2. `DeepSeek Function Calling`
   - 通过 OpenAI-compatible `AsyncOpenAI` 客户端统一处理。
3. `MCP Compatible`
   - Skill Registry 已持久化 `mcpToolSchema`，Router 只需在未来增加 `MCP transport adapter` 即可。

核心原则：

- Router 内部统一使用 `Skill Manifest`
- 对 OpenAI / DeepSeek / MCP 只做 schema 和 transport 适配
- Skill 本体不感知模型厂商差异

## 11. Future Multi-Agent

为未来 Multi-Agent 预留的扩展点：

- `toolContext.sessionId`
- `toolContext.variables`
- `toolContext.notes`
- `workspaceId`
- `traceId`
- `allowedSkills / deniedSkills`

推荐扩展模式：

1. `Planner Agent`
   - 只负责技能规划，不直接写数据。
2. `Worker Agent`
   - 负责真实 Tool 执行。
3. `Reviewer Agent`
   - 负责验证工具结果、判断是否需要额外步骤。
4. `Approval Agent / Human`
   - 处理写操作确认。

## 12. 长链路任务设计

当前同步版适合中短链路。

未来长链路建议：

1. 把 `toolContext` 持久化到 Redis / DB
2. 为每轮调用记录 `traceId / step / toolCalls`
3. 当步数过多时切为异步 Job
4. 对长任务新增：
   - `callId`
   - `runId`
   - `resumeToken`
   - `checkpoint`

建议新增接口：

- `POST /api/ai/router/run`
- `POST /api/ai/router/resume`
- `GET /api/ai/router/runs/{runId}`
- `POST /api/ai/router/runs/{runId}/confirm`

## 13. TypeScript 示例

参考文件：

- `ui/src/lib/aiToolRouter.ts`

调用示例：

```ts
import { runToolRouter } from "./aiToolRouter";

const result = await runToolRouter({
  message: "帮我查看延期任务，并按负责人汇总",
  workspaceId: "wkbDefault",
  tableIds: ["dstDefault"],
  allowedSkills: [
    "qtable.record.query",
    "qtable.table.describe",
  ],
  toolContext: {
    sessionId: "sess_001",
    workspaceId: "wkbDefault",
    tableIds: ["dstDefault"],
    variables: {},
    toolResults: [],
    notes: [],
    chainDepth: 0,
    retryPolicy: {
      maxAttempts: 2,
      backoffMs: 350,
    },
  },
});

if (result.status === "completed") {
  console.log(result.answer);
}
```

确认流示例：

```ts
import { confirmToolRouterRun } from "./aiToolRouter";

const confirmed = await confirmToolRouterRun({
  message: "创建一个新的延期任务，负责人是 Evan，截止明天",
  workspaceId: "wkbDefault",
  tableIds: ["dstDefault"],
  toolContext: previous.toolContext,
});
```

## 14. Python 示例

参考文件：

- `scripts/demo_ai_tool_router.py`

核心调用：

```python
payload = {
    "message": "帮我查看延期任务，并按负责人统计数量",
    "workspaceId": "wkbDefault",
    "tableIds": ["dstDefault"],
    "allowedSkills": ["qtable.record.query", "qtable.table.describe"],
    "toolContext": {
        "sessionId": "demo-session",
        "tableIds": ["dstDefault"],
        "variables": {},
        "toolResults": [],
        "notes": [],
        "chainDepth": 0,
        "retryPolicy": {"maxAttempts": 2, "backoffMs": 350},
    },
    "maxSteps": 6,
    "toolLimit": 8,
    "confirmed": False,
}
```

## 15. 多 Tool Chain 示例

### 示例 A：查看延期任务

用户：

```text
帮我查看延期任务
```

链路：

1. `qtable.record.query`
   - 条件：`status = overdue`
2. Router 回填 Tool 结果
3. LLM 输出自然语言总结

### 示例 B：查看延期任务并按负责人统计

用户：

```text
帮我查看延期任务，并按负责人统计数量
```

链路：

1. `qtable.record.query`
   - 过滤延期任务
   - 聚合 `groupBy = owner`
2. Router 回填聚合结果
3. LLM 输出统计摘要

### 示例 C：创建任务前先读表结构

用户：

```text
新增一条任务：标题是修复 Tool Router，负责人 Evan，截止明天
```

链路：

1. `qtable.table.describe`
   - 读取字段与选项
2. LLM 自动提取参数并映射字段
3. `qtable.record.create`
   - 首轮返回 `requires_confirmation`
4. 用户确认
5. `qtable.record.create`
   - 再次执行，真正写入
6. LLM 输出写入成功结果

### 示例 D：长链路任务

用户：

```text
找出延期任务，按照优先级排序，再为最高优先级任务创建一条跟进记录
```

链路：

1. `qtable.record.query`
2. Router 从结果中选出最高优先级记录
3. `qtable.record.create`
   - 生成跟进记录
4. 返回确认
5. 用户确认后写入

## 16. 下一步建议

建议继续补齐：

1. `GraphQL callAgentTools` mutation
2. Router Run 持久化表结构和步骤日志
3. Router Resume / Confirm / Cancel API
4. Redis-backed 长任务 checkpoint
5. Embedding-based Skill recall
6. MCP transport executor
7. Multi-Agent orchestration layer

## 17. 结论

当前这版 Tool Router 已经形成最小可运行闭环：

- OpenAI Tool Calling
- DeepSeek Function Calling
- 多 Skill 路由
- 参数自动提取
- Tool 执行
- Tool 结果回填
- 多轮 Tool Calling
- Tool Retry
- Tool 权限控制
- Tool Context 管理

并且保持：

- MCP Compatible
- 兼容未来 Multi-Agent
- 可扩展到长链路任务
