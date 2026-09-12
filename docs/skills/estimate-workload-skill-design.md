# Estimate Workload Skill 设计

本文档描述 `qtable.project.estimate_workload` 的落地方案，目标是在你当前 QTable 架构里形成一个可闭环的 Agent 化工作量评估能力。

## 1. Skill 架构

```text
User / Agent / Workflow
  -> /api/estimate-workload/run
  -> app/services/estimate_workload.py
      -> Context Engine
      -> Historical Learning (PostgreSQL + Redis cache)
      -> PydanticAI Agent
          -> load_historical_estimates()
          -> load_table_context()
      -> Structured Output (Pydantic Schema)
      -> Result Persistence
      -> Feedback Loop
  -> app/skills/estimate_workload.py
      -> SkillRuntime / SkillRegistry
      -> /api/skills/call
```

组成说明：

- `app/schemas/estimate_workload.py`
  - 定义请求、结构化输出、反馈模型。
- `app/models/estimate_workload.py`
  - 持久化估算结果与反馈结果。
- `app/services/estimate_workload.py`
  - Agent 主流程、Prompt、Context 注入、历史学习、重试、归一化、持久化。
- `app/api/estimate_workload.py`
  - 暴露 REST 接口：运行估算、读取估算、提交反馈。
- `app/skills/estimate_workload.py`
  - 以 builtin Skill 的方式接入 `SkillRuntime`，可通过 `/api/skills/call` 被 Agent/Tool Router 复用。

闭环路径：

1. 调用 `run`
2. 生成结构化估算结果
3. 落库到 `workload_estimate_runs`
4. 执行后提交反馈
5. 历史样本进入下一轮估算学习

## 2. Prompt 模板

系统 Prompt 核心模板：

```text
你是 QTable 的 estimate_workload Agent，负责对复杂项目进行可审计、可学习、可复用的工作量评估。

必须输出：
1. Story Point
2. P50/P90
3. riskCoefficient
4. teamCapabilityFactor
5. techStackFactor
6. historicalAdjustmentFactor
7. confidence
8. breakdown
9. topRisks
10. assumptions / warnings / nextActions

建议公式：
adjustedStoryPoints =
  baseStoryPoints
  * riskCoefficient
  * techStackFactor
  * historicalAdjustmentFactor
  * teamCapabilityFactor

约束：
- 不得编造历史事实
- 历史样本不足时 historicalAdjustmentFactor 保持接近 1.0
- p90Hours 必须 >= p50Hours
- breakdown 至少覆盖 3 个可交付工作块
- 输出必须严格符合结构化 schema
```

用户 Prompt 核心模板：

```text
用户目标：{prompt}

团队画像：
- teamSize: {teamSize}
- avgSeniority: {avgSeniority}
- domainFamiliarity: {domainFamiliarity}
- stackFamiliarity: {stackFamiliarity}
- deliveryMaturity: {deliveryMaturity}

技术栈画像：
- primaryStack: {primaryStack}
- architecture: {architecture}
- integrations: {integrations}
- unknowns: {unknowns}

上下文亮点：
{contextHighlights}

历史学习摘要：
- averageHoursPerStoryPoint: {avgHoursPerSP}
- averageBias: {avgBias}
- examples: {historyExamples}

请给出完整估算结果，并确保 confidence、historicalLearning、formula、nextActions 都有内容。
```

## 3. Pydantic Schema

核心请求：

```python
class EstimateWorkloadRequest(BaseModel):
    prompt: str
    persist_result: bool
    dry_run: bool
    workspace_id: str | None
    project_id: str | None
    task_id: str | None
    table_ids: list[str]
    team_profile: TeamCapabilityProfile
    tech_stack: TechStackProfile
    business_domain: str | None
    quality_bar: Literal["prototype", "production", "enterprise"]
    non_functional_requirements: list[str]
    tags: list[str]
    historical_learning: HistoricalLearningConfig
    retry: EstimateWorkloadRetryPolicy
```

核心结果：

```python
class EstimateWorkloadResult(BaseModel):
    summary: str
    scope: str
    base_story_points: float
    adjusted_story_points: float
    p50_hours: float
    p90_hours: float
    risk_coefficient: float
    team_capability_factor: float
    tech_stack_factor: float
    historical_adjustment_factor: float
    confidence: EstimateConfidence
    context_insight: ContextInsight
    historical_learning: HistoricalLearningSummary
    breakdown: list[WorkloadBreakdownItem]
    top_risks: list[WorkloadRisk]
    assumptions: list[str]
    warnings: list[str]
    formula: str
    next_actions: list[str]
```

反馈 Schema：

```python
class EstimateFeedbackRequest(BaseModel):
    actual_story_points: float | None
    actual_hours: float | None
    outcome_status: Literal["better_than_expected", "on_track", "worse_than_expected"]
    accuracy_rating: int | None
    notes: str
```

## 4. 历史数据学习方案

当前实现采用“轻学习闭环”：

1. 数据来源
   - 历史估算记录：`workload_estimate_runs`
   - 已回填反馈的记录：`actual_story_points` / `actual_hours`

2. 召回逻辑
   - `prompt` 关键词重叠
   - `tech_stack_tags` 重叠
   - `work_type_tags` 重叠
   - 同 workspace / project / business domain 加分
   - 有反馈的样本优先

3. 学习特征
   - `averageHoursPerStoryPoint`
   - `averageBias = actualHours / p50Hours`
   - 相似样本列表 `examples`

4. Redis 用法
   - 将历史学习摘要缓存在 `estimate-workload-history:*`
   - 在新反馈提交后主动失效缓存

5. 闭环方式
   - 初次估算 -> 记录预测
   - 项目执行完成 -> 提交实际工时 / 实际 Story Point
   - 后续估算 -> 读取已反馈样本进行修正

## 5. PostgreSQL Schema

主表：`workload_estimate_runs`

关键字段：

- `id`
- `workspace_id`
- `project_id`
- `task_id`
- `team_id`
- `source_prompt`
- `normalized_scope`
- `business_domain`
- `quality_bar`
- `tech_stack_tags`
- `work_type_tags`
- `request_payload`
- `context_summary`
- `historical_summary`
- `structured_output`
- `confidence_score`
- `base_story_points`
- `adjusted_story_points`
- `p50_hours`
- `p90_hours`
- `risk_coefficient`
- `team_capability_factor`
- `tech_stack_factor`
- `historical_adjustment_factor`
- `status`
- `provider`
- `model`
- `trace_id`
- `created_by`
- `actual_story_points`
- `actual_hours`
- `outcome_status`
- `accuracy_rating`
- `feedback_notes`
- `feedback_at`
- `created_at`
- `updated_at`

设计原则：

- 一条记录同时保存请求、上下文、估算结果与反馈结果。
- 无需单独建“学习样本表”，直接基于历史估算记录做召回。
- 后续若要做更强学习，可拆出 `estimate_workload_feedback_events` 或向量索引表。

## 6. FastAPI 实现

REST 接口：

- `POST /api/estimate-workload/run`
  - 运行估算 Agent
- `GET /api/estimate-workload/estimates/{estimate_id}`
  - 读取估算详情
- `POST /api/estimate-workload/estimates/{estimate_id}/feedback`
  - 提交实际反馈，形成学习闭环

Skill 接口：

- builtin skill 名称：`qtable.project.estimate_workload`
- 通过 `POST /api/skills/call` 调用
- 启动时通过 `build_default_registry()` + `sync_builtin_registry_entries()` 自动同步到 Skill Registry

实现特点：

- Context 感知：复用 `context_builder`
- Structured Output：复用 `PydanticAI + output_type`
- Retry：
  - `maxAttempts`
  - `maxOutputRetries`
  - `maxToolRetries`
  - `backoffMs`
- 历史学习：`PostgreSQL + Redis`

## 7. 示例结果

```json
{
  "traceId": "5a4c4452-6613-4b16-9d58-f5cb1d0d8f9c",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "attempts": 1,
  "persisted": true,
  "estimateId": "0b82c56d-4d4e-4c18-8a1f-f5be43b8a77a",
  "result": {
    "summary": "该项目属于中高复杂度企业交付，主要成本来自认证链路、数据建模、历史任务学习与结构化输出稳定性。",
    "scope": "开发 Agent 化 estimate_workload Skill，并接入 QTable 当前 Skill/Runtime/API 闭环。",
    "baseStoryPoints": 21,
    "adjustedStoryPoints": 29.4,
    "p50Hours": 148,
    "p90Hours": 212,
    "riskCoefficient": 1.2,
    "teamCapabilityFactor": 0.95,
    "techStackFactor": 1.18,
    "historicalAdjustmentFactor": 1.1,
    "confidence": {
      "score": 0.78,
      "level": "high",
      "rationale": "上下文较完整，且存在可参考的同类后端 Agent 任务样本。"
    },
    "contextInsight": {
      "summary": "当前系统已具备 SkillRuntime、Context Engine、Structured Output 与 Task Split 闭环，可直接复用。",
      "signals": [
        "已有 task_split Agent 实现",
        "已有 SkillRegistry 与 builtin sync",
        "已有 Context Engine 可注入 workspace/project/table/conversation"
      ]
    },
    "historicalLearning": {
      "sampleCount": 4,
      "feedbackSampleCount": 2,
      "averageHoursPerStoryPoint": 5.1,
      "averageBias": 1.08,
      "notes": [
        "历史样本略偏乐观，建议 P90 额外保留联调与回归缓冲。"
      ],
      "examples": []
    },
    "breakdown": [
      {
        "name": "Schema 与模型设计",
        "description": "定义请求、结果、反馈与数据库模型",
        "storyPoints": 5,
        "p50Hours": 24,
        "p90Hours": 32,
        "riskCoefficient": 1.05,
        "assumptions": []
      },
      {
        "name": "Agent 服务实现",
        "description": "实现 Prompt、历史学习、上下文注入、归一化与重试",
        "storyPoints": 13,
        "p50Hours": 68,
        "p90Hours": 102,
        "riskCoefficient": 1.25,
        "assumptions": []
      },
      {
        "name": "API 与 Skill 集成",
        "description": "接入 REST、SkillRuntime、Registry Sync 与反馈闭环",
        "storyPoints": 8,
        "p50Hours": 56,
        "p90Hours": 78,
        "riskCoefficient": 1.15,
        "assumptions": []
      }
    ],
    "topRisks": [
      {
        "title": "历史反馈样本不足",
        "level": "medium",
        "impact": "历史学习修正力度有限",
        "mitigation": "引导团队在执行完成后回填 actualHours/actualStoryPoints"
      },
      {
        "title": "需求描述模糊",
        "level": "high",
        "impact": "Story Point 与 P50/P90 容易失真",
        "mitigation": "要求输入更明确的范围、验收标准和非功能约束"
      }
    ],
    "assumptions": [
      "当前项目默认复用现有认证、SkillRegistry 与 Context Engine 基础设施",
      "无需引入额外的异步队列编排层"
    ],
    "warnings": [],
    "formula": "adjustedStoryPoints = baseStoryPoints * riskCoefficient * techStackFactor * historicalAdjustmentFactor * teamCapabilityFactor",
    "nextActions": [
      "将结果拆成实现任务并进入 task_split",
      "执行后提交 feedback 以增强历史学习效果"
    ]
  }
}
```
