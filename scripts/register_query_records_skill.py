import asyncio

from app.db.init_db import init_models
from app.db.session import AsyncSessionLocal
from app.services.skill_registry import (
    SkillRegistrationRequest,
    skill_registry_service,
)


QUERY_RECORDS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "workspaceId": {
            "type": "string",
            "description": "Workspace ID. Optional when injected by runtime.",
        },
        "tableId": {
            "type": "string",
            "description": "Target table ID.",
        },
        "tableName": {
            "type": "string",
            "description": "Optional fallback when tableId is unknown.",
        },
        "select": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
        },
        "filter": {
            "type": "object",
            "description": "Nested filter tree in DSL format.",
            "additionalProperties": True,
        },
        "sort": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                    "nulls": {"type": "string", "enum": ["first", "last"]},
                },
                "required": ["field", "direction"],
                "additionalProperties": False,
            },
            "default": [],
        },
        "page": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "cursor": {"type": ["string", "null"], "default": None},
            },
            "additionalProperties": False,
        },
        "search": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}, "default": []},
                "mode": {
                    "type": "string",
                    "enum": ["contains", "prefix", "trigram", "full_text"],
                    "default": "contains",
                },
            },
            "required": ["keyword"],
            "additionalProperties": False,
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "select": {"type": "array", "items": {"type": "string"}, "default": []},
                    "filter": {"type": "object", "additionalProperties": True},
                    "sort": {"type": "array", "items": {"type": "object"}, "default": []},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["field"],
                "additionalProperties": False,
            },
            "default": [],
        },
        "formula": {
            "type": "object",
            "properties": {
                "include": {"type": "boolean", "default": True},
                "fields": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "additionalProperties": False,
        },
        "aggregate": {
            "type": "object",
            "properties": {
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["count", "sum", "avg", "min", "max", "count_distinct"],
                            },
                            "field": {"type": "string"},
                            "as": {"type": "string"},
                        },
                        "required": ["type", "as"],
                        "additionalProperties": False,
                    },
                    "default": [],
                },
                "groupBy": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "additionalProperties": False,
        },
        "options": {
            "type": "object",
            "properties": {
                "includeHiddenFields": {"type": "boolean", "default": False},
                "returnDisplayValues": {"type": "boolean", "default": True},
                "relationDepth": {"type": "integer", "minimum": 0, "maximum": 2, "default": 1},
            },
            "additionalProperties": False,
        },
    },
    "required": ["tableId"],
    "additionalProperties": False,
}


QUERY_RECORDS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tableId": {"type": "string"},
        "selectedFields": {"type": "array", "items": {"type": "string"}},
        "records": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "aggregates": {"type": "object", "additionalProperties": True},
        "pageInfo": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "cursor": {"type": ["string", "null"]},
                "nextCursor": {"type": ["string", "null"]},
                "hasMore": {"type": "boolean"},
                "total": {"type": ["integer", "null"]},
            },
            "required": ["limit", "hasMore"],
            "additionalProperties": False,
        },
        "relationData": {"type": "object", "additionalProperties": True},
        "execution": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["postgres", "memory_fallback"]},
                "queryMs": {"type": "number"},
                "formulaMs": {"type": "number"},
                "usedIndexes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tableId", "selectedFields", "records", "pageInfo", "execution", "warnings"],
    "additionalProperties": False,
}


async def main() -> None:
    await init_models()
    async with AsyncSessionLocal() as session:
        registered = await skill_registry_service.register_skill(
            session,
            SkillRegistrationRequest.model_validate(
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
                    "confirmation_required": False,
                    "idempotent": True,
                    "supports_dry_run": True,
                    "entrypoint": "http://localhost:8000/api/skills/qtable-record-query",
                    "inputSchema": QUERY_RECORDS_INPUT_SCHEMA,
                    "outputSchema": QUERY_RECORDS_OUTPUT_SCHEMA,
                    "permissions": [
                        {
                            "resource": "table",
                            "action": "read",
                            "target_param": "tableId",
                            "optional": False,
                        }
                    ],
                    "transportConfig": {
                        "protocol": "http",
                        "timeoutMs": 20000,
                        "retry": 1,
                    },
                    "embeddingText": "Query QTable records with filtering, sorting, pagination, fuzzy search, relation expansion, formulas, and aggregate analytics.",
                    "changelog": "Initial query_records skill registration",
                }
            ),
            actor="system-script",
        )
        print(registered)


if __name__ == "__main__":
    asyncio.run(main())
