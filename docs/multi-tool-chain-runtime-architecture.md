# QTable Multi Tool Chain Runtime

## 1. 目标

在现有 `SkillRuntime`、`ToolExecutor`、`AI Tool Router` 之上，新增 `Multi Tool Chain Runtime`，实现：

- AI 自动规划多 Skill 执行链路
- 支持依赖编排、并行调度
- 支持跨步骤上下文传递与结果记忆
- 支持步骤级重试与回滚
- 支持流式进度推送
- 兼容 LangGraph 方向

## 2. 总体架构

```
Client (React 19 / REST / SSE)
  -> POST /api/tool-chains/run        (同步)
  -> POST /api/tool-chains/stream     (SSE 流式)
  -> GET  /api/tool-chains/runs/{id}  (查询)

FastAPI ToolChains Router (app/api/tool_chain.py)
  -> ToolChainRuntimeService (app/services/tool_chain.py)
      -> Planning Layer (LLM auto-plan or explicit plan)
      -> Step Scheduler (DAG topological + parallel)
      -> Tool Chain Executor
          -> ToolExecutor (复用, app/tool_adapter/executor.py)
          -> SkillRuntime (复用, app/skills/runtime.py)
      -> State Machine (step lifecycle)
      -> Context Passer (template + memory)
      -> Retry Engine (per-step config)
      -> Rollback Engine (best-effort / strict)
      -> Observation Hub (lifecycle events + SSE bridge)
      -> Persistence (PostgreSQL + Redis)

  PostgreSQL: tool_chain_runs / tool_chain_step_runs / tool_chain_event_logs
  Redis: run cache + progress pub/sub
```

## 3. 核心数据模型

### 3.1 ToolChainPlan（执行计划）

```python
class ToolChainPlan(BaseModel):
    goal: str
    summary: str
    reasoning: list[str]           # planner 推理过程
    steps: list[ToolChainStepDefinition]
    final_output_path: str         # 指定最终输出的 step，缺省取最后一步
    planner_metadata: dict
    langgraph_spec: dict           # LangGraph 兼容 spec
```

### 3.2 ToolChainStepDefinition（步骤定义）

```python
class ToolChainStepDefinition(BaseModel):
    step_id: str                   # 唯一 ID
    skill_name: str                # 对应 Skill 名
    description: str
    depends_on: list[str]          # 依赖的 step_id 列表
    arguments: dict                # 入参（支持 {{placeholder}} 模板）
    save_result_as: str            # 结果存入 context 的 key
    retry: ToolChainRetryPolicy    # 该步独立重试策略
    rollback: ToolChainStepRollback # 回滚策略
    timeout_seconds: int           # 超时秒数
```

### 3.3 参数模板

入参支持 `{{变量名}}` 占位符，Runtime 从以下来源自动填充：

1. `step.<step_id>.output` — 前置步骤输出
2. `context.variables` — 请求上下文变量
3. `memory.<key>` — 跨步骤记忆

示例：

```json
{
  "stepId": "create_records",
  "skillName": "qtable.task.records.create",
  "arguments": {
    "tableId": "{{context.tableId}}",
    "taskPlan": "{{step.split.output.result}}",
    "ganttData": "{{step.gantt.output}}"
  }
}
```

## 4. Tool State Machine

每个 `ToolChainStepRun` 经历以下状态迁移：

```
                  ┌──────────────────────────────────────────┐
                  ↓                                          │
  pending → ready → running → completed                      │
             │          ↓                                    │
             │      retrying ──→ running (retry loop)        │
             │          ↓                                    │
             │      waiting_confirmation ──→ (暂停，等用户确认) │
             │          ↓                                     │
             │      failed ──→ rolled_back (if rollback)     │
             │          ↓                                     │
             │      rolled_back (终态)                        │
             │                                                │
             └── skipped (依赖失败/条件不满足)                  │
```

Run 级状态：

```
queued → planning → running → completed
                           → waiting_confirmation
                           → rolling_back → rolled_back
                           → failed
```

## 5. Tool Chain Engine 调度

采用 DAG 拓扑调度 + 并行执行：

```text
1. 解析 Plan → 构建邻接表（depends_on → successors）
2. 入度为 0 的 step → 立即入执行队列
3. Step 完成 → 减后继入度 → 入度归 0 则入队列
4. 并行执行同批次 step（无依赖冲突）
5. 任意 step 失败 → 根据 rollback 策略决定是否终止
6. 全部 done → 构建最终响应
```

关键实现（`app/services/tool_chain.py`）：

```
_execute_plan():
  dag = _build_dag(plan)
  ready_queue = [step for step in dag if indegree=0]
  while ready_queue:
    results = await asyncio.gather(*[execute_step(s) for s in ready_queue])
    for each result:
      apply_context_pass(result)
      if failed and rollback.strict:
        trigger_rollback()
        return
      enqueue successor steps with indegree=0
```

## 6. Context Passing

### 6.1 模板解析

```python
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

def resolve_placeholders(template, ctx):
    for match in PLACEHOLDER_RE.finditer(str(template)):
        path = match.group(1).strip()
        value = get_by_path(ctx, path)  # step.X.output, context.Y, memory.Z
        template = template.replace(match.group(0), json.dumps(value))
    return template
```

### 6.2 传递路径

- **step.X.output** — 前置步骤的 output_payload，X 为 step_id
- **step.X.output.path.to.key** — 深层字段访问
- **context.xxx** — 请求带入的上下文（workspaceId, tableIds 等）
- **memory.xxx** — 跨步骤共享变量（前一步用 save_result_as 写入）

## 7. Retry 机制

双层重试：

### 7.1 ToolChain 层（步骤调度器）

```python
class ToolChainRetryPolicy:
    max_attempts: int = 2
    backoff_ms: int = 500
    retryable_states: list = ["failed", "rolled_back"]
```

由 `ToolChainStepDefinition.retry` 控制。

### 7.2 ToolExecutor 层（底层执行器）

复用现有 `ToolRetryConfig`，处理 `INVALID_INPUT` / `EXECUTION_FAILED` 等瞬态错误。

### 7.3 重试流程

```text
step 执行失败
  → 检查 retry.max_attempts > 当前 attempt
  → 等待 retry.backoff_ms
  → 重新提交执行队列
  → 记录 step_retrying 事件
  → 超限后标记 failed
```

## 8. Rollback 机制

### 8.1 策略

```python
class ToolChainRollbackPolicy:
    enabled: bool = True
    mode: Literal["best_effort", "strict"] = "best_effort"

class ToolChainStepRollback:
    kind: Literal["none", "delete_created_records", "skill"]
    skill_name: str | None           # 回滚用 Skill
    arguments: dict                  # 回滚 Skill 参数
```

### 8.2 回滚模式

- **best_effort**：按完成顺序倒序回滚，单个失败不影响后续
- **strict**：任一步骤失败立即全链终止，触发全部回滚

### 8.3 内置回滚

- `delete_created_records`：自动删除 `qtable.task.records.create` 生成的记录
- `skill`：调用指定的独立回滚 Skill
- `none`：不执行回滚

## 9. Observation（可观测性）

### 9.1 事件类型

```
run_started         → 链启动
planning_started    → Planner 开始规划
planning_completed  → 规划完成
step_started        → 步骤开始
step_completed      → 步骤完成
step_retrying       → 步骤重试
step_waiting_confirmation → 等待确认
step_failed         → 步骤失败
rollback_started    → 回滚开始
rollback_completed  → 回滚完成
run_completed       → 链完成
run_failed          → 链失败
```

### 9.2 SSE 流式输出

```text
POST /api/tool-chains/stream
  -> text/event-stream

data: {"type":"run_started","runId":"...","traceId":"...","data":{}}
data: {"type":"step_started","runId":"...","stepId":"split","data":{"skillName":"qtable.task.split"}}
data: {"type":"step_completed","runId":"...","stepId":"split","data":{"output":{...}}}
data: {"type":"run_completed","runId":"...","data":{"summary":"..."}}
```

### 9.3 持久化

所有事件写入 `tool_chain_event_logs` 表，支持事后审计。

## 10. Tool Result Memory

### 10.1 存储层次

| 层次 | 存储 | TTL | 用途 |
|------|------|-----|------|
| Run Context | 内存 dict | 单次 run | 步骤间上下文传递 |
| Session Memory | Redis | 86400s | 会话内跨轮复用 |
| Persistent Storage | PostgreSQL | 永久 | 审计/复盘 |

### 10.2 内存结构

```python
class ToolChainContextState:
    variables: dict[str, Any]     # 用户预置变量
    memory: dict[str, Any]        # 步骤产出（通过 save_result_as 写入）
    tool_results: list[dict]      # 各步骤执行摘要
```

## 11. Tool Dependency

```
depends_on: ["step_a", "step_b"]
  → 等价于有向边 step_a → this, step_b → this
  → 调度器保证 step_a 和 step_b 都完成才执行 this
```

图例（审批系统链）：

```
       ┌─────────┐
       │  split   │ (qtable.task.split)
       └────┬─────┘
            │
   ┌────────┼────────┐
   ↓        ↓        ↓
┌──────┐ ┌──────┐ ┌──────┐
│estimate│ │gantt │ └──────┘
│       │ │      │   (并行)
└───┬───┘ └──┬───┘
    ↓        │
┌───────────┐│
│create_rec │←┘
│  ords     │
└───────────┘
```

## 12. Long Task 支持

### 12.1 异步执行

- 所有 I/O 操作使用 `asyncio`
- 步骤执行使用 `asyncio.wait_for(timeout)`
- Redis 缓存中间状态，支持跨请求查询进度

### 12.2 进度查询

```
GET /api/tool-chains/runs/{run_id}
  → {
      "runId": "...",
      "status": "running",
      "steps": [
        {"stepId":"split","state":"completed","durationMs":3200},
        {"stepId":"gantt","state":"running","attemptCount":1},
        {"stepId":"estimate","state":"pending"}
      ]
    }
```

## 13. Streaming Progress

```
POST /api/tool-chains/stream
  body: { "message": "帮我规划审批系统", "plan": ..., "stream": true }

SSE 输出:
  data: {"type":"run_started",...}
  data: {"type":"planning_completed","data":{"plan":{...}}}
  data: {"type":"step_started","stepId":"split","data":{"skillName":"qtable.task.split"}}
  data: {"type":"step_completed","stepId":"split","data":{"output":{...}}}
  data: {"type":"step_started","stepId":"estimate","data":{"skillName":"qtable.project.estimate_workload"}}
  data: {"type":"step_started","stepId":"gantt","data":{"skillName":"qtable.gantt.generate"}}
  data: {"type":"step_completed","stepId":"gantt","data":{"output":{...}}}
  data: {"type":"step_completed","stepId":"estimate","data":{"output":{...}}}
  data: {"type":"step_started","stepId":"create_records","data":{...}}
  data: {"type":"step_completed","stepId":"create_records","data":{"output":{...}}}
  data: {"type":"run_completed","data":{"summary":"..."}}
```

## 14. 完整执行流程

以"帮我规划一个审批系统"为例：

```text
用户请求:
  POST /api/tool-chains/run
  {
    "message": "帮我规划一个审批系统",
    "autoPlan": true,
    "workspaceId": "ws-1",
    "tableIds": ["dstDefault"],
    "dryRun": false,
    "stream": false
  }

Runtime 自动规划 (autoPlan=true):
  1. LLM Planner 生成 ToolChainPlan:
     - step_split:  qtable.task.split  (prompt="规划审批系统")
     - step_est:    qtable.project.estimate_workload (dependsOn=[split])
     - step_recs:   qtable.task.records.create (dependsOn=[split, estimate])
     - step_gantt:  qtable.gantt.generate (dependsOn=[split])

执行链路:
  Phase 1: step_split running
    → SkillRuntime.invoke("qtable.task.split")
    → task_split_service.run()
    → 返回 TaskSplitPlan (任务树、依赖、Mermaid)

  Phase 2: step_est + step_gantt running (并行)
    step_est:
      → SkillRuntime.invoke("qtable.project.estimate_workload")
      → input: { prompt: "审批系统", ...context from step_split.output }
      → 返回 EstimateWorkloadResult (P50/P90, 风险, 置信度)

    step_gantt:
      → SkillRuntime.invoke("qtable.gantt.generate")
      → input: { taskPlan: {{step.split.output.result}} }
      → 返回 Gantt 时间线 (按依赖排期)

  Phase 3: step_recs running
    → SkillRuntime.invoke("qtable.task.records.create")
    → input: {
        tableId: "dstDefault",
        taskPlan: {{step.split.output.result}},
        ganttData: {{step.gantt.output}}
      }
    → 在 QTable 中创建任务记录，填入甘特日期

最终响应:
  {
    "runId": "run_xxx",
    "status": "completed",
    "steps": [
      {"stepId":"split","state":"completed","output":{...}},
      {"stepId":"estimate","state":"completed","output":{...}},
      {"stepId":"gantt","state":"completed","output":{...}},
      {"stepId":"create_records","state":"completed","output":{...}}
    ],
    "result": {
      "goal": "规划审批系统",
      "plan": {...},
      "recordsCreated": 12,
      "ganttItems": 12
    }
  }
```

## 15. LangGraph 兼容性

预留 `langgraph_spec` 字段，未来可将 `ToolChainPlan` 直接映射为 LangGraph StateGraph：

```python
# plan.steps → graph nodes
# plan.steps.depends_on → graph edges
# ToolChainContextState → State (TypedDict)
# ToolChainRunResponse → graph final output
```

迁移路径：
1. `ToolChainPlan` → `StateGraph`
2. `ToolChainStepDefinition` → `node` + `add_node`
3. `depends_on` → `add_edge` / `add_conditional_edges`
4. `ToolChainRuntimeService` → LangGraph `RunnableConfig`

## 16. 文件清单

| 文件 | 职责 |
|------|------|
| `app/schemas/tool_chain.py` | 所有 Pydantic Schema |
| `app/models/tool_chain.py` | PostgreSQL ORM 模型 |
| `app/services/tool_chain.py` | 核心 Runtime 逻辑 |
| `app/api/tool_chain.py` | FastAPI Router（run / stream / get） |
| `app/skills/runtime.py` | 新增 qtable.gantt.generate / qtable.task.records.create |
| `app/main.py` | 注册 tool_chain_router |
| `app/models/__init__.py` | 导出新模型 |
| `app/db/init_db.py` | 导入新模型（自动建表） |

## 17. 新增 Skill

| Skill | 描述 | 副作用 |
|-------|------|--------|
| `qtable.gantt.generate` | 根据 task_plan 生成甘特时间线 | none |
| `qtable.task.records.create` | 批量创建任务记录并填入甘特日期 | write |

## 18. API 端点

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/tool-chains/run` | 同步执行 Tool Chain |
| POST | `/api/tool-chains/stream` | SSE 流式执行 |
| GET | `/api/tool-chains/runs/{run_id}` | 查询执行状态与结果 |
