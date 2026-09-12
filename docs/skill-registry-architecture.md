# QTable Skill Registry 设计

## 1. Registry 架构设计

```text
React 19 UI
  -> Zustand Skill Registry Store
  -> skillSdk.ts
  -> GraphQL / REST

FastAPI
  -> /api/skills/manifest
  -> /api/skills/call
  -> /api/skills/registry/register
  -> /api/skills/registry/{id}/status
  -> Strawberry GraphQL skillManifest / registerSkill / callSkill

Skill Registry Service
  -> Builtin Skill Sync
  -> PostgreSQL Registry Catalog
  -> Redis Cache
  -> Dynamic Loader
  -> OpenAI Tool Schema Adapter
  -> MCP Tool Schema Adapter

Skill Runtime
  -> Builtin SkillDefinition
  -> Python Module Loader
  -> HTTP / MCP / OpenAI Proxy Skill
  -> Permission / Confirmation / Dry Run
```

核心分层：

- `Builtin Registry`：系统内置技能，启动时同步到 PostgreSQL。
- `Persistent Registry`：工作区技能、插件技能、市场技能统一存放在 `skill_registry_entries`。
- `Version Registry`：每次注册生成 `skill_versions`，支持版本回滚与多版本管理。
- `Embedding Queue Layer`：`skill_embeddings` 负责保存描述向量或待生成状态。
- `Dynamic Runtime Layer`：执行时合并内置技能与启用中的动态技能。
- `Compatibility Layer`：输出 MCP Tool Schema 与 OpenAI Tool Schema。

## 2. PostgreSQL 表结构

### `skill_categories`

- `id`
- `slug`
- `name`
- `description`
- `parent_id`
- `sort_order`
- `is_enabled`
- `created_at`
- `updated_at`

### `skill_registry_entries`

- `id`
- `workspace_id`
- `category_id`
- `canonical_name`
- `namespace`
- `latest_version`
- `title`
- `description`
- `tags`
- `visibility`
- `source_type`
- `runtime_kind`
- `status`
- `is_enabled`
- `entrypoint`
- `module_path`
- `handler_name`
- `icon`
- `manifest_json`
- `input_schema`
- `output_schema`
- `permissions_json`
- `openai_tool_schema`
- `mcp_tool_schema`
- `transport_config`
- `cache_ttl_seconds`
- `embedding_status`
- `embedding_text`
- `created_by`
- `started_at`
- `stopped_at`
- `created_at`
- `updated_at`

### `skill_versions`

- `id`
- `skill_id`
- `version`
- `changelog`
- `checksum`
- `is_current`
- `manifest_json`
- `input_schema`
- `output_schema`
- `permissions_json`
- `openai_tool_schema`
- `mcp_tool_schema`
- `transport_config`
- `embedding_text`
- `published_at`
- `created_at`

### `skill_embeddings`

- `id`
- `skill_version_id`
- `provider`
- `model`
- `vector_dim`
- `embedding`
- `content_hash`
- `indexed_at`
- `created_at`

## 3. Redis 缓存设计

缓存 Key：

- `skill-registry:manifest:{workspace}:{category}:{includeDisabled}:{search}`
- `skill-registry:categories`

缓存策略：

- Manifest 缓存 TTL 默认 `120s`
- Category 缓存 TTL 默认 `300s`
- 注册、版本变更、启停时执行 `skill-registry:*` 前缀失效
- Redis 不可用时自动降级为 PostgreSQL 直查

推荐后续扩展：

- `skill-registry:embedding:pending`
- `skill-registry:call-rate:{workspace}:{skill}`
- `skill-registry:health:{skill}`

## 4. GraphQL Schema

```graphql
type SkillCategoryInfo {
  id: ID!
  slug: String!
  name: String!
  description: String
  parentId: String
  sortOrder: Int!
}

type SkillPermissionRequirementInfo {
  resource: String!
  action: String!
  targetParam: String
  optional: Boolean!
}

type SkillMetadataInfo {
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
  permissions: [SkillPermissionRequirementInfo!]!
}

type SkillRegistryEntryInfo {
  id: ID!
  workspaceId: String
  category: SkillCategoryInfo
  metadata: SkillMetadataInfo!
  inputSchema: JSON!
  outputSchema: JSON!
  visibility: String!
  sourceType: String!
  runtimeKind: String!
  latestVersion: String!
  status: String!
  isEnabled: Boolean!
  openaiToolSchema: JSON
  mcpToolSchema: JSON
  transportConfig: JSON!
  embeddingStatus: String!
}

type SkillCallErrorInfo {
  code: String!
  message: String!
  retryable: Boolean!
  details: JSON
}

type SkillCallResultInfo {
  callId: ID!
  skillName: String!
  state: String!
  output: JSON
  error: SkillCallErrorInfo
  requiresConfirmation: Boolean!
  metadata: JSON!
}

input SkillCallInput {
  skillName: String!
  input: JSON!
  dryRun: Boolean = false
  confirmed: Boolean = false
  origin: String = "graphql"
  traceId: String
}

input SkillRegistryInput {
  name: String!
  version: String = "1.0.0"
  title: String!
  description: String!
  category: String = "general"
  workspaceId: String
  tags: [String!]!
  visibility: String = "internal"
  sourceType: String = "plugin"
  runtimeKind: String = "python_module"
  sideEffect: String = "none"
  confirmationRequired: Boolean = false
  idempotent: Boolean = true
  supportsDryRun: Boolean = true
  inputSchema: JSON!
  outputSchema: JSON!
  permissions: [JSON!]!
}

input SkillStatusInput {
  isEnabled: Boolean!
  status: String = "active"
}

extend type Query {
  skillManifest(
    workspaceId: String
    search: String
    categorySlug: String
    includeDisabled: Boolean = false
  ): [SkillRegistryEntryInfo!]!
  skillCategories: [SkillCategoryInfo!]!
}

extend type Mutation {
  registerSkill(input: SkillRegistryInput!): SkillRegistryEntryInfo!
  setSkillStatus(skillId: String!, input: SkillStatusInput!): SkillRegistryEntryInfo!
  callSkill(input: SkillCallInput!): SkillCallResultInfo!
}
```

## 5. FastAPI 实现

REST 已实现的入口：

- `GET /api/skills/manifest`
- `GET /api/skills/categories`
- `POST /api/skills/call`
- `POST /api/skills/registry/register`
- `POST /api/skills/registry/{skill_id}/status`

运行时能力：

- Manifest 查询
- 搜索、分类、按工作区过滤
- 动态启停
- 动态 Python Module / HTTP Proxy 加载
- OpenAI Tool Schema 生成
- MCP Tool Schema 生成
- Builtin Skill 自动同步入库

## 6. Zustand Store 设计

`ui/src/store/skillRegistryStore.ts`

状态：

- `skills`
- `categories`
- `filters.workspaceId`
- `filters.search`
- `filters.category`
- `filters.includeDisabled`
- `selectedSkillId`
- `loading`
- `error`

动作：

- `loadSkills()`
- `refresh()`
- `createSkill()`
- `toggleSkillStatus()`
- `setSearch()`
- `setCategory()`
- `setWorkspaceId()`
- `setIncludeDisabled()`
- `selectSkill()`

## 7. Skill Manifest 规范

```json
{
  "metadata": {
    "name": "qtable.note.summarize",
    "version": "1.0.0",
    "title": "Summarize Note",
    "description": "Generate a concise summary for a qtable note.",
    "tags": ["notes", "summary", "ai"],
    "side_effect": "none",
    "confirmation_required": false,
    "idempotent": true,
    "supports_dry_run": true,
    "visibility": "workspace",
    "permissions": [
      {
        "resource": "workspace",
        "action": "read",
        "target_param": null,
        "optional": true
      }
    ]
  },
  "input_schema": {
    "type": "object",
    "properties": {
      "noteId": { "type": "string" },
      "maxLength": { "type": "integer", "default": 200 }
    },
    "required": ["noteId"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "summary": { "type": "string" },
      "keywords": {
        "type": "array",
        "items": { "type": "string" }
      }
    },
    "required": ["summary"],
    "additionalProperties": false
  }
}
```

扩展字段：

- `runtime_kind`
- `source_type`
- `transport_config`
- `openai_tool_schema`
- `mcp_tool_schema`
- `embedding_text`

## 8. 示例 Skill 注册代码

Python 示例：

```python
from app.services.skill_registry import SkillRegistrationRequest, skill_registry_service

payload = SkillRegistrationRequest.model_validate(
    {
        "name": "qtable.note.summarize",
        "version": "1.0.0",
        "title": "Summarize Note",
        "description": "Generate a concise summary for a qtable note.",
        "category": "Notes",
        "workspaceId": "wkbDefault",
        "tags": ["notes", "summary", "ai"],
        "visibility": "workspace",
        "source_type": "plugin",
        "runtime_kind": "http_proxy",
        "entrypoint": "http://localhost:8010/skills/note-summarize",
        "inputSchema": {
            "type": "object",
            "properties": {
                "noteId": {"type": "string"}
            },
            "required": ["noteId"]
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"}
            },
            "required": ["summary"]
        }
    }
)

registered = await skill_registry_service.register_skill(
    session,
    payload,
    actor="admin-user-id",
)
```

完整示例脚本：

- `scripts/register_skill_registry_example.py`

## 兼容性说明

- MCP Compatible：持久化 `mcp_tool_schema`，Manifest 输出 `inputSchema / outputSchema`。
- OpenAI Tool Calling Compatible：持久化 `openai_tool_schema`，自动生成 `function.parameters`。
- 支持未来插件化：支持 `python_module`、`http_proxy`、`mcp`、`openai_tool` 四类运行时入口。
