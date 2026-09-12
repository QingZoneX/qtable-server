"""
Layer 2: Schema Tools - 表结构感知

describe_table_schema: 获取表的完整 Schema 信息，支持智能字段识别。
AI Agent 通过此工具理解表的字段结构、类型约束、枚举值等。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.services.smart_table_store import get_full_store, get_full_store_for_table
from app.skills.contracts import (
    SkillErrorCode,
    SkillMetadata,
    SkillPermissionRequirement,
    SkillSideEffect,
)
from app.skills.runtime import SkillDefinition, SkillExecutionContext, SkillRuntimeError
from app.skills.task_management.contracts import (
    TaskManagementToolLayer,
    TaskManagementToolMetadata,
    ToolDomain,
    ToolIntelligenceLevel,
    TaskManagementToolOperateMode,
    MCPCompatibilityInfo,
    LangGraphCompatibilityInfo,
    MultiAgentInfo,
    ToolObservability,
)


# ============ Field Type Classification ============

FIELD_TYPE_CATEGORIES: dict[str, list[str]] = {
    "date_fields": ["date", "datetime", "created_time", "last_modified_time"],
    "status_fields": ["select", "single_select", "multi_select"],
    "person_fields": ["user", "member", "created_by", "last_modified_by"],
    "number_fields": ["number", "currency", "percent", "rating", "formula"],
    "text_fields": ["text", "long_text", "rich_text", "url", "email", "phone"],
    "link_fields": ["link", "lookup", "formula"],
    "progress_fields": ["progress", "percent", "rating"],
    "attachment_fields": ["attachment", "image"],
    "checkbox_fields": ["checkbox"],
}


def _classify_field_types(fields: list[dict[str, Any]]) -> dict[str, list[str]]:
    """智能分类字段类型"""
    classification: dict[str, list[str]] = {}
    for field in fields:
        field_type = str(field.get("type", "")).lower()
        field_id = str(field.get("id", ""))
        for category, types in FIELD_TYPE_CATEGORIES.items():
            if field_type in types:
                classification.setdefault(category, []).append(field_id)
                break
    return classification


class DescribeTableSchemaInput(BaseModel):
    """describe_table_schema 输入"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(..., alias="tableId", description="目标表 ID")
    include_sample_rows: bool = Field(default=True, alias="includeSampleRows")
    sample_limit: int = Field(default=5, ge=1, le=20, alias="sampleLimit")
    include_enum_values: bool = Field(default=True, alias="includeEnumValues")
    include_field_classification: bool = Field(default=True, alias="includeFieldClassification")


class FieldSchemaInfo(BaseModel):
    """字段 Schema 详细信息"""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    type: str
    required: bool = False
    description: str = ""
    options: Optional[list[dict[str, Any]]] = Field(default=None)
    format: Optional[str] = None


class TableSchemaOutput(BaseModel):
    """describe_table_schema 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    table_id: str = Field(alias="tableId")
    table_name: str = Field(default="", alias="tableName")
    field_count: int = Field(alias="fieldCount")
    record_count: int = Field(alias="recordCount")
    fields: list[dict[str, Any]]
    field_classification: dict[str, list[str]] = Field(
        default_factory=dict,
        alias="fieldClassification",
    )
    sample_rows: list[dict[str, Any]] = Field(default_factory=list, alias="sampleRows")
    task_relevant_fields: dict[str, Any] = Field(
        default_factory=dict,
        alias="taskRelevantFields",
    )


TASK_FIELD_KEYWORDS = {
    "title": ["title", "name", "task", "subject", "标题", "任务名", "名称"],
    "status": ["status", "state", "phase", "阶段", "状态"],
    "assignee": ["assignee", "owner", "handler", "assigned_to", "负责人", "处理人"],
    "start_date": ["start", "begin", "开始", "计划开始"],
    "due_date": ["due", "end", "deadline", "截止", "结束", "到期"],
    "priority": ["priority", "severity", "urgent", "优先级", "紧急"],
    "estimated_hours": ["estimate", "hours", "effort", "story_point", "工时", "预估"],
    "actual_hours": ["actual", "spent", "logged", "实际", "耗时"],
    "progress": ["progress", "completion", "done", "进度", "完成"],
    "blocker": ["blocker", "blocked", "impediment", "阻断", "阻塞"],
    "dependency": ["dependency", "depends", "link", "依赖", "前置"],
    "project": ["project", "workspace", "sprint", "iteration", "项目", "迭代"],
    "created_at": ["created", "创建时间", "提交时间"],
    "updated_at": ["updated", "modified", "更新时间", "修改时间"],
}


def _detect_task_relevant_fields(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """自动检测任务相关字段映射"""
    mapping: dict[str, Optional[str]] = {key: None for key in TASK_FIELD_KEYWORDS}
    for field in fields:
        field_name = str(field.get("name", "")).lower()
        field_type = str(field.get("type", "")).lower()
        field_id = str(field.get("id", ""))
        for key, keywords in TASK_FIELD_KEYWORDS.items():
            if mapping[key] is not None:
                continue
            for kw in keywords:
                if kw in field_name:
                    mapping[key] = field_id
                    break

    detected = {key: fid for key, fid in mapping.items() if fid is not None}
    return {
        "detectedFields": detected,
        "coverage": len(detected) / len(TASK_FIELD_KEYWORDS),
        "missingFields": [key for key, fid in mapping.items() if fid is None],
    }


async def handle_describe_table_schema(
    context: SkillExecutionContext,
    data: DescribeTableSchemaInput,
) -> dict[str, Any]:
    """获取表的完整 Schema 信息"""
    backend = (settings.DATA_BACKEND or "").lower()
    if backend in {"db", "database", "postgres", "sqlite"}:
        if context.db is None:
            raise SkillRuntimeError(
                SkillErrorCode.EXECUTION_FAILED,
                "Database session is not available",
            )
        store = await get_full_store(context.db, data.table_id)
    else:
        store = await get_full_store_for_table(data.table_id)

    fields = list(store.get("fields", []))
    records = list(store.get("records", []))

    field_classification = {}
    if data.include_field_classification:
        field_classification = _classify_field_types(fields)

    task_relevant = {}
    if data.include_field_classification:
        task_relevant = _detect_task_relevant_fields(fields)

    # 提取枚举值
    enriched_fields: list[dict[str, Any]] = []
    for field in fields:
        enriched = dict(field)
        if data.include_enum_values and field.get("type") in ("select", "single_select", "multi_select"):
            options = field.get("options") or field.get("config", {}).get("options") or []
            enriched["enumValues"] = options
        enriched_fields.append(enriched)

    return {
        "tableId": data.table_id,
        "tableName": store.get("name", ""),
        "fieldCount": len(fields),
        "recordCount": len(records),
        "fields": enriched_fields,
        "fieldClassification": field_classification,
        "sampleRows": records[:data.sample_limit] if data.include_sample_rows else [],
        "taskRelevantFields": task_relevant,
    }


def build_describe_table_schema_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.schema.describe",
            version="1.0.0",
            title="Describe Table Schema",
            description=(
                "获取 QTable 表的完整 Schema 信息。包含字段列表、字段类型、"
                "枚举值、字段智能分类（日期字段/状态字段/人员字段/数字字段等）、"
                "任务相关字段自动检测（标题/状态/负责人/截止日期/优先级等）、"
                "以及样本数据行。AI Agent 通过此工具理解数据结构，进行精准查询。"
            ),
            tags=["schema", "table", "metadata", "read", "describe"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[
                SkillPermissionRequirement(
                    resource="table",
                    action="read",
                    target_param="table_id",
                )
            ],
        ),
        input_model=DescribeTableSchemaInput,
        output_model=TableSchemaOutput,
        handler=handle_describe_table_schema,
    )
