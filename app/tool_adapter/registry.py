from __future__ import annotations

from typing import Any, Iterable

from app.skills.runtime import SkillRegistry
from app.tool_adapter.contracts import PydanticAIToolSpec, ToolInvokeHandler
from app.tool_adapter.schema import build_tool_schema_bundle, normalize_tool_function_name


class ToolAdapterRegistry:
    def __init__(self) -> None:
        self._specs_by_skill: dict[str, PydanticAIToolSpec] = {}
        self._specs_by_function: dict[str, PydanticAIToolSpec] = {}

    @classmethod
    def from_runtime_registry(
        cls,
        runtime_registry: SkillRegistry,
        skill_names: Iterable[str] | None = None,
    ) -> "ToolAdapterRegistry":
        registry = cls()
        if skill_names is None:
            skill_names = [item.metadata.name for item in runtime_registry.list_manifest()]
        for skill_name in skill_names:
            definition = runtime_registry.get(skill_name)
            if definition is None:
                continue
            registry.register_definition(definition)
        return registry

    def register_definition(self, definition) -> PydanticAIToolSpec:
        manifest = definition.manifest()
        function_name = normalize_tool_function_name(manifest.metadata.name)
        spec = PydanticAIToolSpec(
            skill_name=manifest.metadata.name,
            function_name=function_name,
            description=manifest.metadata.description,
            input_model=definition.input_model,
            output_model=definition.output_model,
            manifest=manifest,
            schema_bundle=build_tool_schema_bundle(definition),
            definition=definition,
        )
        self._specs_by_skill[spec.skill_name] = spec
        self._specs_by_function[spec.function_name] = spec
        return spec

    def get_by_skill_name(self, skill_name: str) -> PydanticAIToolSpec | None:
        return self._specs_by_skill.get(skill_name)

    def get_by_function_name(self, function_name: str) -> PydanticAIToolSpec | None:
        return self._specs_by_function.get(function_name)

    def list_specs(self) -> list[PydanticAIToolSpec]:
        return sorted(self._specs_by_skill.values(), key=lambda item: item.skill_name)

    def build_pydantic_ai_tools(
        self,
        *,
        run_context_annotation: Any,
        invoke_handler: ToolInvokeHandler,
    ) -> list[Any]:
        return [
            spec.build_callable(run_context_annotation, invoke_handler)
            for spec in self.list_specs()
        ]
