from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.skills.contracts import SkillCallResponse, SkillManifestEntry
from app.skills.runtime import SkillDefinition, SkillExecutionContext


class ToolLifecycleStage(str, Enum):
    REGISTERED = "registered"
    BEFORE_EXECUTE = "before_execute"
    AFTER_EXECUTE = "after_execute"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    RETRY = "retry"
    FAILED = "failed"


class ToolSchemaBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_schema: dict[str, Any] = Field(default_factory=dict, alias="jsonSchema")
    output_schema: dict[str, Any] = Field(default_factory=dict, alias="outputSchema")
    openai_tool_schema: dict[str, Any] = Field(default_factory=dict, alias="openaiToolSchema")
    mcp_tool_schema: dict[str, Any] = Field(default_factory=dict, alias="mcpToolSchema")
    pydantic_ai_schema: dict[str, Any] = Field(default_factory=dict, alias="pydanticAiSchema")


class ToolAdapterContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    request: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(..., alias="toolCallId")
    skill_name: str = Field(..., alias="skillName")
    function_name: str = Field(..., alias="functionName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    dry_run: bool = False
    trace_id: str | None = Field(default=None, alias="traceId")


class ToolRetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    backoff_ms: int = Field(default=350, ge=0, le=5_000, alias="backoffMs")


@dataclass
class ToolLifecycleEvent:
    stage: ToolLifecycleStage
    request: ToolExecutionRequest
    adapter_context: ToolAdapterContext
    execution_context: SkillExecutionContext
    attempt: int = 1
    response: SkillCallResponse | None = None
    error: Exception | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecutionOutcome:
    request: ToolExecutionRequest
    response: SkillCallResponse
    adapter_context: ToolAdapterContext
    attempt_count: int
    duration_ms: float


class ToolMetricsSink(Protocol):
    async def increment(
        self,
        metric: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        ...

    async def observe(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        ...


class ToolLifecycleHook(Protocol):
    async def on_event(self, event: ToolLifecycleEvent) -> None:
        ...


ToolCallHandler = Callable[[ToolExecutionRequest, ToolAdapterContext], Awaitable[SkillCallResponse]]
ToolMiddlewareNext = Callable[[ToolExecutionRequest, ToolAdapterContext], Awaitable[SkillCallResponse]]


class ToolMiddleware(Protocol):
    async def __call__(
        self,
        request: ToolExecutionRequest,
        adapter_context: ToolAdapterContext,
        call_next: ToolMiddlewareNext,
    ) -> SkillCallResponse:
        ...


ToolInvokeHandler = Callable[[Any, "PydanticAIToolSpec", BaseModel], Awaitable[dict[str, Any]]]


@dataclass
class PydanticAIToolSpec:
    skill_name: str
    function_name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    manifest: SkillManifestEntry
    schema_bundle: ToolSchemaBundle
    definition: SkillDefinition

    def build_callable(self, run_context_annotation: Any, invoke_handler: ToolInvokeHandler):
        async def dynamic_tool(ctx, payload) -> dict[str, Any]:
            return await invoke_handler(ctx, self, payload)

        dynamic_tool.__name__ = self.function_name
        dynamic_tool.__qualname__ = self.function_name
        dynamic_tool.__doc__ = self.description
        dynamic_tool.__annotations__ = {
            "ctx": run_context_annotation,
            "payload": self.input_model,
            "return": dict[str, Any],
        }
        return dynamic_tool
