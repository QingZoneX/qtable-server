from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


ActionType = Literal[
    "assign_owner",
    "set_priority",
    "set_deadline",
    "create_task",
    "set_dependency",
    "set_status",
    "add_tags",
]


class AiActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: Optional[str] = Field(default=None, alias="actionId")
    action_type: ActionType = Field(alias="type")
    table_id: str = Field(alias="tableId")
    record_id: Optional[str] = Field(default=None, alias="recordId")
    user_id: Optional[int] = Field(default=None, alias="userId")
    value: Optional[str] = None
    values: list[str] = Field(default_factory=list)

    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    deadline: Optional[str] = None
    status: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    dependency_record_ids: list[str] = Field(
        default_factory=list,
        alias="dependencyRecordIds",
    )

    reason: str = ""
    risk: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def normalize(self) -> "AiActionDraft":
        self.table_id = str(self.table_id).strip()
        self.record_id = (
            str(self.record_id).strip()
            if self.record_id is not None and str(self.record_id).strip()
            else None
        )
        self.action_id = (
            str(self.action_id).strip()
            if self.action_id is not None and str(self.action_id).strip()
            else None
        )
        self.value = (
            str(self.value).strip()
            if self.value is not None and str(self.value).strip()
            else None
        )
        self.values = list(dict.fromkeys(
            str(item).strip() for item in self.values if str(item).strip()
        ))[:100]
        self.tags = list(dict.fromkeys(
            str(item).strip() for item in self.tags if str(item).strip()
        ))[:50]
        self.dependency_record_ids = list(dict.fromkeys(
            str(item).strip()
            for item in self.dependency_record_ids
            if str(item).strip()
        ))[:100]
        self.title = self.title.strip() if isinstance(self.title, str) else self.title
        self.description = (
            self.description.strip()
            if isinstance(self.description, str)
            else self.description
        )
        self.reason = self.reason.strip()
        self.risk = self.risk.strip()
        if not self.table_id:
            raise ValueError("tableId is required")
        if self.action_type != "create_task" and not self.record_id:
            raise ValueError(f"recordId is required for {self.action_type}")
        if self.action_type == "assign_owner" and self.user_id is None:
            raise ValueError("userId is required for assign_owner")
        if self.action_type in {"set_priority", "set_deadline", "set_status"} and not self.value:
            raise ValueError(f"value is required for {self.action_type}")
        if self.action_type == "create_task" and not self.title:
            raise ValueError("title is required for create_task")
        return self


class AiActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[AiActionDraft] = Field(default_factory=list, max_length=40)


class AiActionPlanPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis_id: str = Field(alias="diagnosisId")
    member_assignment_batch_id: Optional[str] = Field(
        default=None,
        alias="memberAssignmentBatchId",
    )
    proposed_actions: Optional[list[AiActionDraft]] = Field(
        default=None,
        alias="proposedActions",
    )
    model: Optional[str] = None

    @model_validator(mode="after")
    def normalize(self) -> "AiActionPlanPreviewRequest":
        self.diagnosis_id = self.diagnosis_id.strip()
        if not self.diagnosis_id:
            raise ValueError("diagnosisId is required")
        if self.member_assignment_batch_id is not None:
            self.member_assignment_batch_id = self.member_assignment_batch_id.strip() or None
        if self.proposed_actions is not None:
            if len(self.proposed_actions) > 40:
                raise ValueError("At most 40 proposed actions are allowed")
            ids = [item.action_id for item in self.proposed_actions if item.action_id]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate actionId")
        return self


class AiActionPlanApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(alias="planId")
    action_ids: list[str] = Field(default_factory=list, alias="actionIds")

    @model_validator(mode="after")
    def normalize(self) -> "AiActionPlanApplyRequest":
        self.plan_id = self.plan_id.strip()
        if not self.plan_id:
            raise ValueError("planId is required")
        self.action_ids = list(dict.fromkeys(
            str(item).strip() for item in self.action_ids if str(item).strip()
        ))[:40]
        return self
