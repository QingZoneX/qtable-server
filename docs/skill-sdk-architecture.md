# QTable AI Skill SDK 设计

## 1. 目标

QTable 需要一套企业级、Web Only、可扩展的 Skill 系统，让 AI、Agent、Workflow、Multi-Agent 与 Marketplace 都能基于统一协议调用系统能力，而不是直接耦合到某个特定接口。

本设计基于你当前仓库的技术栈与已有模块：

- 前端：React 19 + Vite + Zustand + Apollo Client + VTable
- 后端：FastAPI + Strawberry GraphQL + PostgreSQL/SQLite + Redis
- 认证：OAuth 2.0 Authorization Code Flow with PKCE
- 已有能力：AI Assistant、表格读写服务、GraphQL API、OAuth 登录

本次初步闭环已经落地以下最小骨架：

- `app/skills/contracts.py`
- `app/skills/runtime.py`
- `app/api/skill_runtime.py`
- `ui/src/lib/skillSdk.ts`

## 2. 总体架构

```text
React AI Client / Agent UI / Workflow Builder
    -> TypeScript Skill SDK
    -> FastAPI Skill Runtime
    -> Skill Registry
    -> Skill Authorizer
    -> Builtin Skills / Workspace Skills / Marketplace Skills
    -> QTable Domain Services (table / record / workspace / attachment / ai)
    -> GraphQL / REST / Redis / PostgreSQL
```

分层建议：

1. `Skill Contract Layer`
   - 统一定义 Metadata、Input/Output Schema、调用协议、错误模型。
2. `Skill Registry Layer`
   - 统一注册内置 Skill、工作区 Skill、第三方 Marketplace Skill。
3. `Skill Runtime Layer`
   - 负责校验输入、注入上下文、权限判断、执行 Skill、返回标准响应。
4. `Tool Calling Layer`
   - 给 LLM / Agent 使用的函数调用协议，风格类似 MCP / OpenAI Function Calling。
5. `Execution Layer`
   - 真正复用你已有的 `smart_table_store`、workspace、AI 服务。

## 3. Skill 标准接口

Skill 的最小标准接口建议如下：

```ts
type SkillHandler<TInput, TOutput> = (
  ctx: SkillExecutionContext,
  input: TInput
) => Promise<TOutput>;
```

关键原则：

- Skill 是纯协议对象，不直接暴露 UI。
- Skill 必须有稳定名字，例如 `qtable.table.describe`。
- Skill 必须声明输入输出 Schema。
- Skill 必须声明副作用等级。
- Skill 必须声明权限需求。
- 所有 Skill 返回统一的标准响应包络。

## 4. Skill Metadata

推荐 Metadata 字段：

| 字段 | 说明 |
|------|------|
| `name` | 全局唯一技能名，如 `qtable.record.create` |
| `version` | 版本号，用于 Marketplace 与兼容控制 |
| `title` | 展示名 |
| `description` | 给人类与 LLM 的描述 |
| `tags` | 分类标签 |
| `side_effect` | `none` / `write` / `external` |
| `confirmation_required` | 是否需要确认 |
| `idempotent` | 是否幂等 |
| `supports_dry_run` | 是否支持试运行 |
| `visibility` | `internal` / `workspace` / `marketplace` |
| `permissions` | 资源权限要求 |

Metadata 既是给前端/后台用的，也是给 Agent Planner 用的。

## 5. Skill Registry

Registry 负责回答三个问题：

1. 系统当前有哪些 Skill？
2. 谁可以看到这些 Skill？
3. 给定技能名时，应该执行哪个 Handler？

建议分三层注册：

1. Builtin Registry
   - QTable 内置能力，如表结构读取、记录创建、视图变更。
2. Workspace Registry
   - 某个工作区私有 Skill，例如企业自定义业务动作。
3. Marketplace Registry
   - 第三方发布的 Skill 包，支持签名、版本、审核与启停。

当前初版在 `app/skills/runtime.py` 中提供的是 Builtin Registry。

## 6. Skill 生命周期

Skill 生命周期建议统一如下：

1. `discovered`
   - Skill 被 Registry 暴露到 Manifest。
2. `selected`
   - LLM / Agent 选择某个 Skill。
3. `validated`
   - Runtime 完成输入 JSON Schema 校验。
4. `authorized`
   - Runtime 完成权限检查。
5. `requires_confirmation`
   - 对写操作或外部操作要求用户确认。
6. `running`
   - Skill 正在执行。
7. `completed`
   - Skill 正常完成并返回标准输出。
8. `failed`
   - Skill 执行失败，返回标准错误。

这套状态机适用于：

- 单轮 Tool Calling
- Agent 多步规划
- Workflow 节点编排
- Human-in-the-loop 审批

## 7. Skill Context

Skill Context 是执行时动态注入的，不属于输入参数本身。

推荐包含：

```json
{
  "user_id": 123,
  "workspace_id": "ws_xxx",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "origin": "assistant",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai",
  "dry_run": false,
  "confirmed": false
}
```

设计原则：

- 用户身份、工作区、租户、trace 信息应来自 Runtime 注入。
- Skill 输入不要重复携带认证信息。
- Context 要可序列化，便于 Workflow 持久化与审计。
- Context 要支持 Agent 间传递，但敏感字段应做最小暴露。

## 8. Skill 权限体系

建议采用三层权限模型：

1. 平台级
   - `skill.execute`
   - `marketplace.manage`
2. 资源级
   - `table.read`
   - `table.write`
   - `workspace.manage`
3. 运行级
   - `confirmation_required`
   - `dry_run_only`
   - `human_approval_required`

当前初版策略：

- Runtime 要求用户已登录。
- `table` 资源在 DB 模式下会走 `WorkspaceItem` 权限检查。
- `qtable.table.describe` 需要读权限。
- `qtable.record.create` 需要写权限，并默认开启确认。

建议后续扩展：

- 支持 Skill 白名单
- 支持 LLM Provider 级别限权
- 支持工作区管理员禁用某些 Marketplace Skill
- 支持按 OAuth Client 限定可调用 Skill 集

## 9. Tool Calling 协议

Tool Calling 建议采用统一 JSON 协议：

```json
{
  "call_id": "call_123",
  "skill_name": "qtable.record.create",
  "input": {
    "tableId": "dst1770791467952",
    "values": {
      "fld_name": "新客户",
      "fld_status": "potential"
    }
  },
  "dry_run": false,
  "confirmed": false,
  "origin": "assistant",
  "trace_id": "trace_abc"
}
```

标准响应：

```json
{
  "call_id": "call_123",
  "skill_name": "qtable.record.create",
  "state": "requires_confirmation",
  "output": null,
  "error": null,
  "requires_confirmation": true,
  "metadata": {
    "sideEffect": "write",
    "previewInput": {
      "tableId": "dst1770791467952",
      "values": {
        "fld_name": "新客户"
      }
    }
  }
}
```

确认后再次调用：

```json
{
  "call_id": "call_123",
  "skill_name": "qtable.record.create",
  "input": {
    "tableId": "dst1770791467952",
    "values": {
      "fld_name": "新客户",
      "fld_status": "potential"
    }
  },
  "confirmed": true,
  "origin": "assistant"
}
```

## 10. JSON Schema 输入输出规范

规范建议：

1. Input / Output 一律使用 JSON Schema 表达。
2. 后端真实校验以 Pydantic Model 为准。
3. 前端以 Schema 生成表单、参数编辑器、Agent Tool 面板。
4. Marketplace 只暴露 Schema 与 Metadata，不暴露内部实现。

当前实现方式：

- Python 使用 `Pydantic.model_json_schema()`
- TypeScript 端直接消费 Runtime 暴露的 `manifest`

示例：

```json
{
  "type": "object",
  "properties": {
    "tableId": { "type": "string" },
    "includeSampleRows": { "type": "boolean", "default": true },
    "sampleLimit": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 }
  },
  "required": ["tableId"],
  "additionalProperties": false
}
```

## 11. Skill 错误处理机制

错误分三层：

1. 协议错误
   - Skill 不存在
   - 输入不合法
2. 权限错误
   - 未登录
   - 无资源权限
   - 缺少确认
3. 执行错误
   - 底层服务失败
   - 数据库失败
   - 第三方 API 失败

推荐统一错误结构：

```json
{
  "code": "FORBIDDEN",
  "message": "Forbidden",
  "retryable": false,
  "details": {
    "tableId": "dst_xxx",
    "required": "read"
  }
}
```

初版错误码：

- `UNAUTHORIZED`
- `FORBIDDEN`
- `SKILL_NOT_FOUND`
- `INVALID_INPUT`
- `EXECUTION_FAILED`

## 12. 可扩展的 Skill Runtime

Runtime 设计上要能兼容未来的四类执行主体：

1. Assistant
   - 用户聊天时单次调用 Skill。
2. Agent
   - Planner 先读 Manifest，再多步调用 Skill。
3. Workflow
   - 每个节点都是一次 Skill Call。
4. Multi-Agent
   - 多 Agent 共享 Manifest，但上下文与权限隔离。

扩展点建议：

- `SkillResolver`
  - 负责按租户 / 工作区 / Marketplace 组合可见 Skill。
- `SkillInterceptor`
  - 负责审计、日志、Tracing、限流、熔断。
- `Async Job Adapter`
  - 长任务转交 Redis Queue / Background Task。
- `Approval Adapter`
  - 接入人工审批与确认流。

## 13. TypeScript SDK 设计

前端 SDK 目标：

- 获取 Skill Manifest
- 让 AI/Agent 选择 Skill
- 发起标准化调用
- 处理确认流
- 与 Zustand / Apollo / Chat UI 解耦

已落地文件：`ui/src/lib/skillSdk.ts`

包含内容：

- `SkillMetadata`
- `SkillManifestEntry`
- `SkillCallRequest`
- `SkillCallResponse`
- `fetchSkillManifest()`
- `callSkill()`

建议前端后续增加：

1. `useSkillManifestStore`
2. `SkillCallComposer`
3. `SkillApprovalDialog`
4. `AgentToolPanel`

## 14. Python Backend 接口设计

已落地：

- `GET /api/skills/manifest`
- `POST /api/skills/call`

建议未来再增加：

- `POST /api/skills/call/batch`
- `GET /api/skills/calls/{call_id}`
- `POST /api/skills/calls/{call_id}/confirm`
- `POST /api/skills/calls/{call_id}/cancel`

这样就能更好支持 Workflow / Long Running Task / Approval。

## 15. FastAPI Skill Runtime 设计

当前实现组成：

1. `contracts.py`
   - 协议、Metadata、Context、错误模型。
2. `runtime.py`
   - Registry、Authorizer、Runtime、Builtin Skills。
3. `skill_runtime.py`
   - FastAPI 路由入口。

当前内置示例 Skill：

1. `qtable.table.describe`
   - 读取表结构和样例数据。
2. `qtable.record.create`
   - 创建记录，默认需要确认，支持 dry-run 思路。

## 16. GraphQL 集成方案

建议 GraphQL 不直接承载真正的 Skill 执行，而是承载：

1. Skill Manifest 查询
2. Skill 调用日志查询
3. Skill 调用记录审批
4. Workflow 编排配置

推荐 Schema：

```graphql
type SkillMetadata {
  name: String!
  version: String!
  title: String!
  description: String!
  tags: [String!]!
  sideEffect: String!
  confirmationRequired: Boolean!
  idempotent: Boolean!
  supportsDryRun: Boolean!
  visibility: String!
}

type SkillManifestEntry {
  metadata: SkillMetadata!
  inputSchema: JSON!
  outputSchema: JSON!
}

type SkillCallError {
  code: String!
  message: String!
  retryable: Boolean!
  details: JSON
}

type SkillCallResult {
  callId: ID!
  skillName: String!
  state: String!
  output: JSON
  requiresConfirmation: Boolean!
  metadata: JSON!
  error: SkillCallError
}

input SkillCallInput {
  skillName: String!
  input: JSON!
  dryRun: Boolean = false
  confirmed: Boolean = false
  origin: String = "graphql"
  traceId: String
}

extend type Query {
  skillManifest: [SkillManifestEntry!]!
}

extend type Mutation {
  callSkill(input: SkillCallInput!): SkillCallResult!
}
```

GraphQL 角色：

- 管理态与查询态优先
- 执行态优先走 REST Runtime
- 长流或事件态再考虑 Subscription / SSE

## 17. 推荐目录结构

```text
app/
  api/
    ai.py
    skill_runtime.py
    graphql/
  skills/
    __init__.py
    contracts.py
    runtime.py
    builtin/
      table.py
      record.py
    marketplace/
      loader.py
      verifier.py
  services/
    smart_table_store/
    workspace/
    ai_service.py

ui/src/
  lib/
    aiApi.ts
    skillSdk.ts
  store/
    aiAssistantStore.ts
    skillManifestStore.ts
  components/
    AiAssistant/
      SkillApprovalDialog.tsx
      ToolCallCard.tsx
      ToolResultCard.tsx
  agent/
    planner.ts
    skillResolver.ts
```

## 18. 示例代码

### 18.1 获取 Manifest

```ts
import { fetchSkillManifest } from "./lib/skillSdk";

const skills = await fetchSkillManifest();
const describeTable = skills.find((item) => item.metadata.name === "qtable.table.describe");
```

### 18.2 调用只读 Skill

```ts
import { callSkill, DescribeTableOutput } from "./lib/skillSdk";

const result = await callSkill<{ tableId: string }, DescribeTableOutput>({
  skill_name: "qtable.table.describe",
  input: { tableId: "dstDefault" },
});

if (result.state === "completed") {
  console.log(result.output?.fields);
}
```

### 18.3 调用写 Skill 并走确认流

```ts
import { callSkill, CreateRecordOutput } from "./lib/skillSdk";

const preview = await callSkill({
  skill_name: "qtable.record.create",
  input: {
    tableId: "dstDefault",
    values: { fld_name: "测试客户" },
  },
});

if (preview.requires_confirmation) {
  const confirmed = await callSkill<Record<string, unknown>, CreateRecordOutput>({
    skill_name: "qtable.record.create",
    input: {
      tableId: "dstDefault",
      values: { fld_name: "测试客户" },
    },
    confirmed: true,
  });
  console.log(confirmed.output?.recordId);
}
```

## 19. 与现有 AI Assistant 的衔接建议

你当前 `app/api/ai.py` 的 `agent` 模式可以逐步替换成：

1. Planner 先加载 Skill Manifest
2. LLM 决定调用哪个 Skill
3. 生成 `SkillCallRequest`
4. Runtime 执行并返回标准化结果
5. LLM 基于 Skill 结果继续推理

这样能把现有的：

- `create_record_from_request`
- `analyze_table_data`
- `table read/write`

都逐步 Skill 化。

## 20. 下一步建议

建议按以下顺序继续推进：

1. 把 GraphQL `skillManifest` 与 `callSkill` 补上
2. 将 `app/api/ai.py` 的 `agent` 模式改成基于 Runtime 调 Skill
3. 新增 `qtable.record.update`、`qtable.record.delete`
4. 增加 Skill Call Log 表与审计追踪
5. 引入 Redis 任务队列支持长任务 Skill
6. 设计 Workspace Skill 与 Marketplace 包格式

## 21. 结论

这套方案满足你的核心要求：

- 类 MCP / OpenAI Function Calling
- 支持 Agent
- 支持 Workflow
- 支持 Multi-Agent
- 支持 Marketplace
- 企业级
- 支持 Web Only 架构

并且已经在当前仓库内落下了最小闭环骨架，而不是只停留在概念设计。
