from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssignmentMemberProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(alias="userId")
    skill_tags: list[str] = Field(default_factory=list, alias="skillTags")
    role_tags: list[str] = Field(default_factory=list, alias="roleTags")
    capacity_hours: float = Field(default=40.0, gt=0.0, le=500.0, alias="capacityHours")

    @model_validator(mode="after")
    def normalize_tags(self) -> "AssignmentMemberProfile":
        self.skill_tags = list(dict.fromkeys(
            str(item).strip() for item in self.skill_tags if str(item).strip()
        ))[:32]
        self.role_tags = list(dict.fromkeys(
            str(item).strip() for item in self.role_tags if str(item).strip()
        ))[:16]
        return self


class MemberAssignmentPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId")
    table_id: str = Field(alias="tableId")
    record_ids: list[str] = Field(default_factory=list, alias="recordIds")
    workload_batch_id: Optional[str] = Field(default=None, alias="workloadBatchId")
    member_profiles: list[AssignmentMemberProfile] = Field(default_factory=list, alias="memberProfiles")
    capacity_overrides: dict[int, float] = Field(default_factory=dict, alias="capacityOverrides")
    locked_assignments: dict[str, int] = Field(default_factory=dict, alias="lockedAssignments")
    preserve_existing_assignees: bool = Field(default=True, alias="preserveExistingAssignees")
    top_k: int = Field(default=3, ge=1, le=3, alias="topK")
    default_task_hours: float = Field(default=8.0, gt=0.0, le=200.0, alias="defaultTaskHours")

    @model_validator(mode="after")
    def normalize(self) -> "MemberAssignmentPreviewRequest":
        self.record_ids = list(dict.fromkeys(
            str(item).strip() for item in self.record_ids if str(item).strip()
        ))[:100]
        normalized_locks: dict[str, int] = {}
        for key, value in self.locked_assignments.items():
            rid = str(key).strip()
            if rid:
                normalized_locks[rid] = int(value)
        self.locked_assignments = normalized_locks
        self.capacity_overrides = {
            int(key): float(value) for key, value in self.capacity_overrides.items()
        }
        for value in self.capacity_overrides.values():
            if value <= 0 or value > 500:
                raise ValueError("Capacity override must be in (0, 500]")
        seen: set[int] = set()
        unique_profiles: list[AssignmentMemberProfile] = []
        for profile in self.member_profiles:
            if profile.user_id in seen:
                raise ValueError("Duplicate member profile")
            seen.add(profile.user_id)
            unique_profiles.append(profile)
        self.member_profiles = unique_profiles
        return self


class MemberAssignmentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(alias="recordId")
    user_id: int = Field(alias="userId")


class MemberAssignmentApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(alias="batchId")
    workspace_id: str = Field(alias="workspaceId")
    table_id: str = Field(alias="tableId")
    assignments: list[MemberAssignmentSelection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_records(self) -> "MemberAssignmentApplyRequest":
        seen: set[str] = set()
        for item in self.assignments:
            item.record_id = str(item.record_id).strip()
            if not item.record_id:
                raise ValueError("recordId is required")
            if item.record_id in seen:
                raise ValueError("Each record can only be assigned once per apply")
            seen.add(item.record_id)
        return self


class MemberAssignmentWhatIfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(alias="batchId")
    capacity_overrides: dict[int, float] = Field(default_factory=dict, alias="capacityOverrides")

    @model_validator(mode="after")
    def normalize(self) -> "MemberAssignmentWhatIfRequest":
        self.capacity_overrides = {
            int(key): float(value) for key, value in self.capacity_overrides.items()
        }
        for value in self.capacity_overrides.values():
            if value <= 0 or value > 500:
                raise ValueError("Capacity override must be in (0, 500]")
        return self
