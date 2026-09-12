from __future__ import annotations

import re

from app.skills.contracts import SkillManifestEntry
from app.skills.runtime import SkillDefinition
from app.tool_adapter.contracts import ToolSchemaBundle


def normalize_tool_function_name(skill_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", skill_name)
    return normalized[:64]


def build_tool_schema_bundle(definition: SkillDefinition) -> ToolSchemaBundle:
    return build_tool_schema_bundle_from_manifest(definition.manifest())


def build_tool_schema_bundle_from_manifest(manifest: SkillManifestEntry) -> ToolSchemaBundle:
    metadata = manifest.metadata
    function_name = normalize_tool_function_name(metadata.name)
    json_schema = manifest.input_schema or {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    output_schema = manifest.output_schema or {
        "type": "object",
        "properties": {},
    }
    return ToolSchemaBundle(
        jsonSchema=json_schema,
        outputSchema=output_schema,
        openaiToolSchema={
            "type": "function",
            "function": {
                "name": function_name,
                "description": metadata.description,
                "parameters": json_schema,
            },
        },
        mcpToolSchema={
            "name": metadata.name,
            "title": metadata.title,
            "description": metadata.description,
            "inputSchema": json_schema,
            "outputSchema": output_schema,
        },
        pydanticAiSchema={
            "name": function_name,
            "description": metadata.description,
            "inputSchema": json_schema,
            "outputSchema": output_schema,
            "metadata": metadata.model_dump(mode="json"),
        },
    )
