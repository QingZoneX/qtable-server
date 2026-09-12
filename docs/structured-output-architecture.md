## Structured Output System

### 目标

为 QTable 当前 AI 系统补齐一条企业级 Structured Output 闭环：

1. 使用 `PydanticAI` 强约束最终输出
2. 使用 `Pydantic` 进行 schema 校验和 typed result
3. 通过 output retry + full-run retry 提升稳定性
4. 在 native structured output 失败后，进入 JSON repair 恢复链路
5. 支持 nested output、tool observation、tool result schema
6. 提供 `FastAPI` 同步接口和 `SSE` 流式接口

### 架构分层

#### 1. API 层

文件：`app/api/structured_output.py`

职责：

- 接收结构化请求
- 复用当前 JWT 鉴权
- 自动从 Header 注入 `workspaceId`、`sessionId` 等上下文
- 提供：
  - `POST /api/structured-output/run`
  - `POST /api/structured-output/stream`

#### 2. Schema 层

文件：`app/schemas/structured_output.py`

职责：

- 定义统一输入输出契约
- 提供 nested output 的强类型模型
- 定义工具观测、验证报告和流式事件模型

核心模型：

- `StructuredOutputRequest`
- `StructuredOutputResult`
- `StructuredRecordDraft`
- `ToolObservation`
- `ValidationReport`
- `StructuredOutputResponse`
- `StructuredOutputStreamEvent`

#### 3. Service 层

文件：`app/services/structured_output.py`

职责：

- 构建 `PydanticAI Agent`
- 注册工具并记录工具观测
- 执行 output validator
- 执行 native run / repair fallback
- 处理 SSE 事件流

### 闭环执行流程

1. API 接收请求并标准化上下文
2. Service 读取当前用户 AI 配置
3. 构建 `PydanticAI Agent(output_type=StructuredOutputResult)`
4. 运行 output validator 做业务规则校验
5. 如有需要调用工具：
   - `load_table_context`
   - `preview_record_draft`
6. 记录 `ToolObservation`，包含：
   - tool arguments
   - input schema
   - result schema
   - latency
   - result/error
7. native 输出成功则直接返回
8. native 输出失败则按策略重试
9. 多次失败后进入 raw JSON repair
10. repair 后再次 `Pydantic` 校验，成功则返回 repaired result

### 支持能力映射

1. `Pydantic Structured Output`
   - `Agent(..., output_type=StructuredOutputResult)`
2. `Output Validation`
   - `@agent.output_validator`
   - `StructuredOutputResult` / `StructuredRecordDraft` 的模型校验
3. `Output Retry`
   - `output_retries`
   - `max_attempts` 全链路重试
4. `Output Repair`
   - `repair_json_text()`
   - `_repair_with_raw_completion()`
5. `Nested Output`
   - `sections / actions / recordDraft / evidence`
6. `Typed Result`
   - 全部输出以 `Pydantic` 类型表达
7. `Tool Observation`
   - `ToolObservation`
8. `Tool Result Schema`
   - `inputSchema / resultSchema`
9. `Error Recovery`
   - native -> retry -> repair -> failed envelope
10. `Streaming Output`
   - `StructuredOutputStreamEvent`
   - `POST /api/structured-output/stream`

### 示例请求

```json
{
  "prompt": "请分析这个任务表的延期风险，并输出一个可执行建议。",
  "mode": "analysis",
  "tableIds": ["dstDefault"],
  "useTools": true,
  "conversation": [
    {
      "role": "user",
      "content": "重点看高优先级任务"
    }
  ]
}
```

### 示例响应

```json
{
  "traceId": "9a1b7d0f-7e27-4c1b-b3e1-7217c5c2e5e1",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "attempts": 1,
  "validation": {
    "passed": true,
    "repaired": false,
    "recoveryStage": "native",
    "issues": []
  },
  "toolObservations": [],
  "result": {
    "status": "completed",
    "intent": "analysis",
    "summary": "当前高优先级任务存在延期风险。",
    "answer": "建议先处理阻塞项并重新排期。",
    "sections": [
      {
        "key": "risk",
        "title": "延期风险",
        "summary": "高优先级任务中存在未启动和即将到期项。",
        "bullets": [
          "未启动任务集中在同一负责人",
          "两项任务截止时间接近"
        ],
        "metrics": [],
        "evidence": [],
        "data": {}
      }
    ],
    "actions": [],
    "warnings": [],
    "nextSteps": [
      "确认阻塞任务的负责人",
      "重新评估本周容量"
    ],
    "evidence": [],
    "confidence": 0.82
  }
}
```

### 后续增强建议

1. 将 `ToolObservation` 写入 PostgreSQL 审计表
2. 为不同业务域拆分多套 `output_type`
3. 增加 Prometheus 指标和告警
4. 增加结构化输出回放与回归测试
