# Query Records Skill 设计

本文档给出 `query_records` Skill 的完整落地方案，目标是让 AI、Agent、Workflow 可以安全、稳定地查询 QTable 多维表格数据，并支持后续扩展到复杂分析场景。

## 1. Skill Metadata

建议注册名使用命名空间风格，便于未来扩展：

```json
{
  "name": "qtable.record.query",
  "version": "1.0.0",
  "title": "Query QTable Records",
  "description": "Query records from QTable with filters, sorting, pagination, fuzzy search, relations, formulas, and aggregations for AI assistants and agent workflows.",
  "category": "Records",
  "workspaceId": "wkbDefault",
  "tags": ["record", "query", "search", "aggregation", "agent"],
  "visibility": "workspace",
  "source_type": "plugin",
  "runtime_kind": "http_proxy",
  "side_effect": "none",
  "confirmation_required": false,
  "idempotent": true,
  "supports_dry_run": true,
  "permissions": [
    {
      "resource": "table",
      "action": "read",
      "target_param": "tableId",
      "optional": false
    }
  ],
  "transportConfig": {
    "protocol": "http",
    "timeoutMs": 20000,
    "retry": 1
  },
  "embeddingText": "Query QTable records with filtering, sorting, pagination, fuzzy search, relation expansion, formulas, and aggregate analytics."
}
```

设计说明：

- `side_effect = none`：只读 Skill，适合 AI 自动调用。
- `idempotent = true`：相同输入可安全重试。
- `runtime_kind = http_proxy`：最容易接到 FastAPI 运行时，也适合后续拆分成独立查询服务。
- `permissions`：只要求 `table.read`，但仍保留工作区和表级权限检查。

## 2. JSON Schema

### 2.1 Input Schema

```json
{
  "type": "object",
  "properties": {
    "workspaceId": {
      "type": "string",
      "description": "Workspace ID. Optional when injected by runtime."
    },
    "tableId": {
      "type": "string",
      "description": "Target table ID."
    },
    "tableName": {
      "type": "string",
      "description": "Optional fallback when tableId is unknown."
    },
    "select": {
      "type": "array",
      "description": "Fields to return. Use field IDs when possible.",
      "items": {
        "type": "string"
      },
      "default": []
    },
    "filter": {
      "$ref": "#/$defs/filterGroup"
    },
    "sort": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/sortRule"
      },
      "default": []
    },
    "page": {
      "$ref": "#/$defs/page"
    },
    "search": {
      "$ref": "#/$defs/search"
    },
    "relations": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/relationQuery"
      },
      "default": []
    },
    "formula": {
      "$ref": "#/$defs/formula"
    },
    "aggregate": {
      "$ref": "#/$defs/aggregate"
    },
    "options": {
      "$ref": "#/$defs/options"
    }
  },
  "required": ["tableId"],
  "additionalProperties": false,
  "$defs": {
    "filterCondition": {
      "type": "object",
      "properties": {
        "field": { "type": "string" },
        "operator": {
          "type": "string",
          "enum": [
            "=",
            "!=",
            ">",
            ">=",
            "<",
            "<=",
            "in",
            "not_in",
            "between",
            "contains",
            "not_contains",
            "starts_with",
            "ends_with",
            "is_empty",
            "is_not_empty",
            "like",
            "ilike",
            "fts",
            "exists"
          ]
        },
        "value": {},
        "values": {
          "type": "array",
          "items": {}
        },
        "caseSensitive": {
          "type": "boolean",
          "default": false
        }
      },
      "required": ["field", "operator"],
      "additionalProperties": false
    },
    "filterGroup": {
      "type": "object",
      "properties": {
        "op": {
          "type": "string",
          "enum": ["and", "or", "not"]
        },
        "conditions": {
          "type": "array",
          "items": {
            "anyOf": [
              { "$ref": "#/$defs/filterCondition" },
              { "$ref": "#/$defs/filterGroup" }
            ]
          }
        }
      },
      "required": ["op", "conditions"],
      "additionalProperties": false
    },
    "sortRule": {
      "type": "object",
      "properties": {
        "field": { "type": "string" },
        "direction": {
          "type": "string",
          "enum": ["asc", "desc"]
        },
        "nulls": {
          "type": "string",
          "enum": ["first", "last"],
          "default": "last"
        }
      },
      "required": ["field", "direction"],
      "additionalProperties": false
    },
    "page": {
      "type": "object",
      "properties": {
        "limit": {
          "type": "integer",
          "minimum": 1,
          "maximum": 100,
          "default": 20
        },
        "offset": {
          "type": "integer",
          "minimum": 0,
          "default": 0
        },
        "cursor": {
          "type": ["string", "null"],
          "default": null
        }
      },
      "additionalProperties": false
    },
    "search": {
      "type": "object",
      "properties": {
        "keyword": { "type": "string" },
        "fields": {
          "type": "array",
          "items": { "type": "string" },
          "default": []
        },
        "mode": {
          "type": "string",
          "enum": ["contains", "prefix", "trigram", "full_text"],
          "default": "contains"
        }
      },
      "required": ["keyword"],
      "additionalProperties": false
    },
    "relationQuery": {
      "type": "object",
      "properties": {
        "field": { "type": "string" },
        "select": {
          "type": "array",
          "items": { "type": "string" },
          "default": []
        },
        "filter": {
          "$ref": "#/$defs/filterGroup"
        },
        "sort": {
          "type": "array",
          "items": { "$ref": "#/$defs/sortRule" },
          "default": []
        },
        "limit": {
          "type": "integer",
          "minimum": 1,
          "maximum": 50,
          "default": 10
        }
      },
      "required": ["field"],
      "additionalProperties": false
    },
    "formula": {
      "type": "object",
      "properties": {
        "include": {
          "type": "boolean",
          "default": true
        },
        "fields": {
          "type": "array",
          "items": { "type": "string" },
          "default": []
        }
      },
      "additionalProperties": false
    },
    "aggregate": {
      "type": "object",
      "properties": {
        "metrics": {
          "type": "array",
          "items": { "$ref": "#/$defs/metric" },
          "default": []
        },
        "groupBy": {
          "type": "array",
          "items": { "type": "string" },
          "default": []
        }
      },
      "additionalProperties": false
    },
    "metric": {
      "type": "object",
      "properties": {
        "type": {
          "type": "string",
          "enum": ["count", "sum", "avg", "min", "max", "count_distinct"]
        },
        "field": {
          "type": "string"
        },
        "as": {
          "type": "string"
        }
      },
      "required": ["type", "as"],
      "additionalProperties": false
    },
    "options": {
      "type": "object",
      "properties": {
        "includeHiddenFields": {
          "type": "boolean",
          "default": false
        },
        "returnDisplayValues": {
          "type": "boolean",
          "default": true
        },
        "relationDepth": {
          "type": "integer",
          "minimum": 0,
          "maximum": 2,
          "default": 1
        }
      },
      "additionalProperties": false
    }
  }
}
```

### 2.2 Output Schema

```json
{
  "type": "object",
  "properties": {
    "tableId": { "type": "string" },
    "selectedFields": {
      "type": "array",
      "items": { "type": "string" }
    },
    "records": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true
      }
    },
    "aggregates": {
      "type": "object",
      "additionalProperties": true
    },
    "pageInfo": {
      "type": "object",
      "properties": {
        "limit": { "type": "integer" },
        "offset": { "type": "integer" },
        "cursor": { "type": ["string", "null"] },
        "nextCursor": { "type": ["string", "null"] },
        "hasMore": { "type": "boolean" },
        "total": { "type": ["integer", "null"] }
      },
      "required": ["limit", "hasMore"],
      "additionalProperties": false
    },
    "relationData": {
      "type": "object",
      "additionalProperties": true
    },
    "execution": {
      "type": "object",
      "properties": {
        "mode": {
          "type": "string",
          "enum": ["postgres", "memory_fallback"]
        },
        "queryMs": { "type": "number" },
        "formulaMs": { "type": "number" },
        "usedIndexes": {
          "type": "array",
          "items": { "type": "string" }
        }
      },
      "required": ["mode"],
      "additionalProperties": false
    },
    "warnings": {
      "type": "array",
      "items": { "type": "string" }
    }
  },
  "required": ["tableId", "selectedFields", "records", "pageInfo", "execution", "warnings"],
  "additionalProperties": false
}
```

## 3. GraphQL 查询设计

### 3.1 Schema

```graphql
scalar JSON

input QueryFilterConditionInput {
  field: String!
  operator: String!
  value: JSON
  values: [JSON!]
  caseSensitive: Boolean = false
}

input QueryFilterGroupInput {
  op: String!
  conditions: [JSON!]!
}

input QuerySortInput {
  field: String!
  direction: String!
  nulls: String = "last"
}

input QueryPageInput {
  limit: Int = 20
  offset: Int = 0
  cursor: String
}

input QuerySearchInput {
  keyword: String!
  fields: [String!] = []
  mode: String = "contains"
}

input QueryRelationInput {
  field: String!
  select: [String!] = []
  filter: QueryFilterGroupInput
  sort: [QuerySortInput!] = []
  limit: Int = 10
}

input QueryMetricInput {
  type: String!
  field: String
  as: String!
}

input QueryAggregateInput {
  metrics: [QueryMetricInput!] = []
  groupBy: [String!] = []
}

input QueryFormulaInput {
  include: Boolean = true
  fields: [String!] = []
}

input QueryOptionsInput {
  includeHiddenFields: Boolean = false
  returnDisplayValues: Boolean = true
  relationDepth: Int = 1
}

input QueryRecordsInput {
  workspaceId: String
  tableId: String!
  tableName: String
  select: [String!] = []
  filter: QueryFilterGroupInput
  sort: [QuerySortInput!] = []
  page: QueryPageInput
  search: QuerySearchInput
  relations: [QueryRelationInput!] = []
  formula: QueryFormulaInput
  aggregate: QueryAggregateInput
  options: QueryOptionsInput
}

type QueryPageInfo {
  limit: Int!
  offset: Int
  cursor: String
  nextCursor: String
  hasMore: Boolean!
  total: Int
}

type QueryExecutionInfo {
  mode: String!
  queryMs: Float
  formulaMs: Float
  usedIndexes: [String!]!
}

type QueryRecordsResult {
  tableId: String!
  selectedFields: [String!]!
  records: [JSON!]!
  aggregates: JSON
  pageInfo: QueryPageInfo!
  relationData: JSON
  execution: QueryExecutionInfo!
  warnings: [String!]!
}

extend type Query {
  queryRecords(input: QueryRecordsInput!): QueryRecordsResult!
}

extend type Mutation {
  callSkill(input: SkillCallInput!): SkillCallResultInfo!
}
```

### 3.2 设计原则

- GraphQL 直接暴露 `queryRecords`，适合前端数据浏览和强类型接入。
- AI Agent 仍优先走 Skill Runtime 的 `callSkill`，这样可以复用权限、日志、审计、缓存和未来 Workflow。
- `queryRecords` 与 `qtable.record.query` 共享同一服务层，避免出现两套查询逻辑。

## 4. FastAPI 实现

### 4.1 路由设计

新增运行时代理端点：

```python
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.services.record_query_service import RecordQueryService, QueryRecordsInput

router = APIRouter(prefix="/api/skills/qtable-record-query", tags=["skills"])


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


@router.post("")
async def query_records_proxy(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    input_payload = payload.get("input") or {}
    context_payload = payload.get("context") or {}
    try:
        query_input = QueryRecordsInput.model_validate(input_payload)
        result = await RecordQueryService(db).query_records(
            query_input,
            user_id=context_payload.get("user_id"),
            workspace_id=context_payload.get("workspace_id"),
        )
        return {"output": result.model_dump(mode="json", by_alias=True)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

### 4.2 服务层设计

建议新增文件：

- `app/services/record_query_service.py`
- `app/services/query_planner.py`
- `app/services/query_formula.py`

核心流程：

1. 解析输入，校验 `tableId`、字段、权限。
2. 将 DSL 转换为 `QueryPlan`。
3. Query Planner 决定走：
   - PostgreSQL SQL 直推
   - Postgres + Python 公式后处理
   - 内存回退模式
4. 返回统一结果模型。

### 4.3 关键 Pydantic 模型

```python
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class QuerySortInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: str
    nulls: str = "last"


class QueryPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    cursor: Optional[str] = None


class QueryMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    field: Optional[str] = None
    alias: str = Field(alias="as")


class QueryRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    table_id: str = Field(alias="tableId")
    table_name: Optional[str] = Field(default=None, alias="tableName")
    select: list[str] = Field(default_factory=list)
    filter: Optional[dict[str, Any]] = None
    sort: list[QuerySortInput] = Field(default_factory=list)
    page: QueryPageInput = Field(default_factory=QueryPageInput)
    search: Optional[dict[str, Any]] = None
    relations: list[dict[str, Any]] = Field(default_factory=list)
    formula: Optional[dict[str, Any]] = None
    aggregate: Optional[dict[str, Any]] = None
    options: dict[str, Any] = Field(default_factory=dict)
```

### 4.4 SQLAlchemy 查询骨架

当前你的 `TableRecord.data` 是 JSON 存储，所以第一阶段建议继续基于 JSONB 查询：

```python
from sqlalchemy import and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.sqltypes import Float, Integer, String

from app.models.smart_table import TableRecord


def jsonb_text(field_id: str):
    return TableRecord.data.op("->>")(field_id)


def jsonb_numeric(field_id: str):
    return cast(TableRecord.data.op("->>")(field_id), Float)


async def build_base_query(table_id: str):
    return select(TableRecord).where(TableRecord.table_id == table_id)


def apply_filter(query, condition: dict):
    field = condition["field"]
    operator = condition["operator"]
    value = condition.get("value")

    if operator == "=":
        return query.where(jsonb_text(field) == str(value))
    if operator == "!=":
        return query.where(jsonb_text(field) != str(value))
    if operator == "contains":
        return query.where(jsonb_text(field).ilike(f"%{value}%"))
    if operator == ">=":
        return query.where(jsonb_numeric(field) >= float(value))
    raise ValueError(f"Unsupported operator: {operator}")
```

### 4.5 关联字段查询

建议将关联字段存成目标 `recordId` 数组，然后在服务层二次批量查询：

1. 基表先查出当前页记录。
2. 收集关联字段中的全部目标记录 ID。
3. 按目标表批量查询。
4. 按 `relations[].select` 做裁剪。

这样比对每一行逐条 join 更稳定，也更适合 AI 工具调用。

### 4.6 公式字段

建议分两类：

- 可 SQL 化公式：
  - `concat`
  - `coalesce`
  - `+ - * /`
  - `case when`
- 仅 Python 计算公式：
  - 跨表 lookup
  - 自定义表达式
  - 日期差、文本模板等复杂逻辑

推荐策略：

- 查询主数据时优先返回原始字段。
- 若 `formula.include = true`，再在服务层做公式补算。
- 常用公式字段可后续做物化列或缓存。

## 5. PostgreSQL 查询优化

你当前 `TableRecord.data` 适合先走 JSONB 路线，优化建议如下。

### 5.1 基础索引

```sql
create index if not exists idx_table_records_table_id
on table_records (table_id);

create index if not exists idx_table_records_table_id_order
on table_records (table_id, order_index);

create index if not exists idx_table_records_data_gin
on table_records
using gin (data jsonb_path_ops);
```

### 5.2 常用字段表达式索引

对于高频过滤和排序字段，建立表达式索引：

```sql
create index if not exists idx_orders_status
on table_records ((data->>'fld_status'))
where table_id = 'dst_orders';

create index if not exists idx_orders_amount_num
on table_records (((data->>'fld_amount')::numeric))
where table_id = 'dst_orders';
```

### 5.3 模糊搜索优化

```sql
create extension if not exists pg_trgm;

create index if not exists idx_orders_customer_trgm
on table_records
using gin ((data->>'fld_customer') gin_trgm_ops)
where table_id = 'dst_orders';
```

适用场景：

- `contains`
- `ilike`
- 相近词模糊匹配

### 5.4 全文检索

若某些表的文本搜索很重，增加 `tsvector` 物化列：

```sql
alter table table_records
add column if not exists search_vector tsvector;

create index if not exists idx_table_records_search_vector
on table_records using gin (search_vector);
```

然后把重要文本字段拼成 `search_vector`，用于 `fts` 模式。

### 5.5 分页策略

- 小表或管理态：`offset + limit`
- 大表或 AI 多轮读取：`cursor pagination`
- Cursor 推荐包含：
  - 最后一条排序字段值
  - `record_id`

避免深分页扫描导致性能抖动。

### 5.6 聚合优化

- 高频统计可建立物化视图
- `group by` 字段适合单列表达式索引
- 若某字段聚合使用频繁，可将其拆分为真实列

## 6. AI 可理解的 Query DSL

推荐 DSL 设计重点：

- 对 LLM 足够自然
- 对 Planner 足够稳定
- 对后端足够容易编译成 SQL

### 6.1 标准 DSL

```json
{
  "workspaceId": "wkbDefault",
  "tableId": "dst_orders",
  "select": ["recordId", "fld_customer", "fld_amount", "fld_status"],
  "filter": {
    "op": "and",
    "conditions": [
      { "field": "fld_status", "operator": "in", "value": ["paid", "shipped"] },
      {
        "op": "or",
        "conditions": [
          { "field": "fld_amount", "operator": ">=", "value": 1000 },
          { "field": "fld_priority", "operator": "=", "value": "vip" }
        ]
      }
    ]
  },
  "sort": [
    { "field": "fld_amount", "direction": "desc", "nulls": "last" },
    { "field": "recordId", "direction": "asc", "nulls": "last" }
  ],
  "page": {
    "limit": 20,
    "cursor": null
  },
  "search": {
    "keyword": "Acme",
    "fields": ["fld_customer", "fld_note"],
    "mode": "trigram"
  },
  "relations": [
    {
      "field": "fld_customer_ref",
      "select": ["recordId", "fld_name", "fld_tier"],
      "limit": 5
    }
  ],
  "formula": {
    "include": true,
    "fields": ["fld_total_score"]
  },
  "aggregate": {
    "metrics": [
      { "type": "count", "as": "totalRecords" },
      { "type": "sum", "field": "fld_amount", "as": "totalAmount" },
      { "type": "avg", "field": "fld_amount", "as": "avgAmount" }
    ],
    "groupBy": ["fld_status"]
  },
  "options": {
    "includeHiddenFields": false,
    "returnDisplayValues": true,
    "relationDepth": 1
  }
}
```

### 6.2 LLM 使用约束

- `tableId` 优先于 `tableName`
- `field` 尽量使用 field ID
- `groupBy` 仅允许标量字段
- `relationDepth` 默认 1，最大 2
- `limit` 最大 100

### 6.3 Agent Workflow 友好点

输出中保留：

- `selectedFields`
- `pageInfo.nextCursor`
- `execution.mode`
- `warnings`

这样后续 Agent 可以继续：

- 拉下一页
- 重试公式补算
- 降级为简化查询
- 把聚合结果交给分析节点

## 7. TypeScript Tool 定义

```ts
export type QueryOperator =
  | "="
  | "!="
  | ">"
  | ">="
  | "<"
  | "<="
  | "in"
  | "not_in"
  | "between"
  | "contains"
  | "not_contains"
  | "starts_with"
  | "ends_with"
  | "is_empty"
  | "is_not_empty"
  | "like"
  | "ilike"
  | "fts"
  | "exists";

export interface QueryFilterCondition {
  field: string;
  operator: QueryOperator;
  value?: unknown;
  values?: unknown[];
  caseSensitive?: boolean;
}

export interface QueryFilterGroup {
  op: "and" | "or" | "not";
  conditions: Array<QueryFilterCondition | QueryFilterGroup>;
}

export interface QuerySortRule {
  field: string;
  direction: "asc" | "desc";
  nulls?: "first" | "last";
}

export interface QueryPage {
  limit?: number;
  offset?: number;
  cursor?: string | null;
}

export interface QuerySearch {
  keyword: string;
  fields?: string[];
  mode?: "contains" | "prefix" | "trigram" | "full_text";
}

export interface QueryRelation {
  field: string;
  select?: string[];
  filter?: QueryFilterGroup;
  sort?: QuerySortRule[];
  limit?: number;
}

export interface QueryMetric {
  type: "count" | "sum" | "avg" | "min" | "max" | "count_distinct";
  field?: string;
  as: string;
}

export interface QueryRecordsInput {
  workspaceId?: string;
  tableId: string;
  tableName?: string;
  select?: string[];
  filter?: QueryFilterGroup;
  sort?: QuerySortRule[];
  page?: QueryPage;
  search?: QuerySearch;
  relations?: QueryRelation[];
  formula?: {
    include?: boolean;
    fields?: string[];
  };
  aggregate?: {
    metrics?: QueryMetric[];
    groupBy?: string[];
  };
  options?: {
    includeHiddenFields?: boolean;
    returnDisplayValues?: boolean;
    relationDepth?: number;
  };
}

export interface QueryRecordsResult {
  tableId: string;
  selectedFields: string[];
  records: Array<Record<string, unknown>>;
  aggregates?: Record<string, unknown>;
  pageInfo: {
    limit: number;
    offset?: number;
    cursor?: string | null;
    nextCursor?: string | null;
    hasMore: boolean;
    total?: number | null;
  };
  relationData?: Record<string, unknown>;
  execution: {
    mode: "postgres" | "memory_fallback";
    queryMs?: number;
    formulaMs?: number;
    usedIndexes: string[];
  };
  warnings: string[];
}

export async function queryRecords(
  input: QueryRecordsInput,
): Promise<QueryRecordsResult> {
  const result = await callSkill<QueryRecordsInput, QueryRecordsResult>({
    skill_name: "qtable.record.query",
    input,
    origin: "assistant",
  });

  if (result.state !== "completed" || !result.output) {
    throw new Error(result.error?.message || "query_records failed");
  }

  return result.output;
}
```

## 8. Tool Calling 示例

### 8.1 简单表查询

```json
{
  "skill_name": "qtable.record.query",
  "input": {
    "tableId": "dst_orders",
    "select": ["recordId", "fld_customer", "fld_amount", "fld_status"],
    "page": { "limit": 10 }
  },
  "origin": "assistant"
}
```

### 8.2 条件过滤 + 排序

```json
{
  "skill_name": "qtable.record.query",
  "input": {
    "tableId": "dst_orders",
    "filter": {
      "op": "and",
      "conditions": [
        { "field": "fld_status", "operator": "=", "value": "paid" },
        { "field": "fld_amount", "operator": ">=", "value": 5000 }
      ]
    },
    "sort": [
      { "field": "fld_amount", "direction": "desc" }
    ],
    "page": { "limit": 20 }
  },
  "origin": "agent"
}
```

### 8.3 模糊搜索 + 字段选择

```json
{
  "skill_name": "qtable.record.query",
  "input": {
    "tableId": "dst_customers",
    "select": ["recordId", "fld_name", "fld_company", "fld_email"],
    "search": {
      "keyword": "acme",
      "fields": ["fld_name", "fld_company"],
      "mode": "trigram"
    },
    "page": { "limit": 15 }
  },
  "origin": "assistant"
}
```

### 8.4 关联字段 + 公式字段 + 聚合

```json
{
  "skill_name": "qtable.record.query",
  "input": {
    "tableId": "dst_opportunities",
    "select": ["recordId", "fld_name", "fld_stage", "fld_amount", "fld_owner_ref"],
    "relations": [
      {
        "field": "fld_owner_ref",
        "select": ["recordId", "fld_name", "fld_team"],
        "limit": 1
      }
    ],
    "formula": {
      "include": true,
      "fields": ["fld_weighted_amount"]
    },
    "aggregate": {
      "metrics": [
        { "type": "count", "as": "dealCount" },
        { "type": "sum", "field": "fld_amount", "as": "totalPipeline" }
      ],
      "groupBy": ["fld_stage"]
    }
  },
  "origin": "workflow"
}
```

## 9. 错误处理方案

### 9.1 标准错误结构

```json
{
  "code": "FIELD_NOT_FOUND",
  "message": "Field not found: fld_stauts",
  "retryable": false,
  "details": {
    "field": "fld_stauts",
    "suggestion": "fld_status",
    "tableId": "dst_orders"
  }
}
```

### 9.2 推荐错误码

```text
UNAUTHORIZED
FORBIDDEN
TABLE_NOT_FOUND
FIELD_NOT_FOUND
INVALID_INPUT
INVALID_FILTER
INVALID_SORT
INVALID_PAGE
INVALID_AGGREGATE
INVALID_RELATION
RELATION_DEPTH_EXCEEDED
UNSUPPORTED_FORMULA
QUERY_TIMEOUT
TOO_MANY_RESULTS
EXECUTION_FAILED
```

### 9.3 分类处理策略

- 输入错误：直接返回，不重试。
- 权限错误：直接返回，并提示缺少哪个权限。
- 字段解析错误：返回最接近字段名，便于 Agent 自修复。
- 查询超时：返回 `retryable = true`，并建议缩小 `select` 或降低 `limit`。
- 关系深度过大：提示降为 1 层或 2 层。
- 公式不支持：返回基础字段结果，并把公式写入 `warnings`。

### 9.4 Agent 友好降级策略

服务端可按以下顺序降级：

1. 去掉 `relations`
2. 去掉 `formula`
3. 去掉 `aggregate`
4. 缩小 `select`
5. 减少 `limit`

若发生降级，必须在 `warnings` 中显式返回。

## 10. 推荐落地步骤

建议按下面顺序实现：

1. 先新增 `RecordQueryService`，支持 `select/filter/sort/page/search`
2. 再支持 `aggregate`
3. 再支持 `relations`
4. 最后支持 `formula`

这样能够最快形成 MVP，同时保留后续演进空间。

## 11. 与当前仓库的接入建议

结合你当前项目结构，建议新增或修改：

- 新增 `app/services/record_query_service.py`
- 新增 `app/api/skills_query_records.py` 或直接挂到 Skill HTTP Proxy 路由
- 新增 GraphQL `queryRecords`
- 通过 `scripts/register_query_records_skill.py` 注册到 `skill_registry_entries`
- 前端可在 `ui/src/lib/skillSdk.ts` 基础上增加 `queryRecords()`

## 12. 结论

这个 `query_records` Skill 方案满足你的目标：

- AI Friendly
- 支持后续 Agent Workflow
- 支持复杂过滤
- 支持多表关联
- 能复用现有 FastAPI + Strawberry GraphQL + PostgreSQL + Skill Registry 架构

第一阶段建议把它实现成只读查询 Skill，并以 `http_proxy` 形式注册。这样最稳，且不会和现有 Runtime 设计冲突。
