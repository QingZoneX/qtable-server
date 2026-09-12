from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


InboxStatus = Literal["pending", "converted", "archived", "ignored", "duplicate"]


class SourceInboxSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(alias="sourceId", min_length=1, max_length=256)
    source_type: str = Field(default="qnote", alias="sourceType", max_length=32)
    url: Optional[str] = None
    canonical_url: Optional[str] = Field(default=None, alias="canonicalUrl")
    page_title: Optional[str] = Field(default=None, alias="pageTitle", max_length=2000)
    quote: Optional[str] = Field(default=None, max_length=20000)
    annotation: Optional[str] = Field(default=None, max_length=20000)
    anchor: Optional[dict[str, Any]] = None
    captured_at: Optional[datetime] = Field(default=None, alias="capturedAt")
    tags: list[str] = Field(default_factory=list, max_length=50)
    source_author: Optional[str] = Field(default=None, alias="sourceAuthor", max_length=255)
    screenshot_url: Optional[str] = Field(default=None, alias="screenshotUrl")
    preview: Optional[dict[str, Any]] = None

    @field_validator("source_id", "source_type", mode="after")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be empty")
        return value

    @field_validator("tags", mode="after")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or value.casefold() in seen:
                continue
            seen.add(value.casefold())
            result.append(value[:100])
        return result[:50]

    @model_validator(mode="after")
    def require_content(self) -> "SourceInboxSource":
        if not any(
            value and str(value).strip()
            for value in [self.url, self.page_title, self.quote, self.annotation]
        ):
            raise ValueError("source must contain url, pageTitle, quote or annotation")
        return self


class SourceInboxIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId", min_length=1, max_length=128)
    source: SourceInboxSource


class SourceInboxPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(alias="itemId", min_length=1, max_length=64)
    target_table_id: Optional[str] = Field(default=None, alias="targetTableId", max_length=128)
    model: Optional[str] = Field(default=None, max_length=128)


class SourceInboxAiSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_convert: bool = Field(alias="shouldConvert")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10000)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    suggested_assignee_user_id: Optional[int] = Field(default=None, alias="suggestedAssigneeUserId")
    workload_hours: Optional[float] = Field(default=None, alias="workloadHours", ge=0, le=10000)
    reason: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.5, ge=0, le=1)


class SourceInboxConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(alias="itemId", min_length=1, max_length=64)
    target_table_id: str = Field(alias="targetTableId", min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10000)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    assignee_user_id: Optional[int] = Field(default=None, alias="assigneeUserId", ge=1)
    workload_hours: Optional[float] = Field(default=None, alias="workloadHours", ge=0, le=10000)

    @field_validator("title", mode="after")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title cannot be empty")
        return value


class SourceInboxStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[str] = Field(alias="itemIds", min_length=1, max_length=100)
    status: Literal["pending", "archived", "ignored"]

    @field_validator("item_ids", mode="after")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
