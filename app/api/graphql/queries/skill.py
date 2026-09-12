from __future__ import annotations

from typing import Any, Optional

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.api.graphql.types import (
    SkillCategoryInfo,
    SkillMetadataInfo,
    SkillPermissionRequirementInfo,
    SkillRegistryEntryInfo,
)
from app.services.skill_registry import skill_registry_service


def _to_category(value: dict[str, Any] | None) -> SkillCategoryInfo | None:
    if not value:
        return None
    return SkillCategoryInfo(
        id=value["id"],
        slug=value["slug"],
        name=value["name"],
        description=value.get("description"),
        parentId=value.get("parentId"),
        sortOrder=value.get("sortOrder", 0),
    )


def _to_skill_entry(value: dict[str, Any]) -> SkillRegistryEntryInfo:
    metadata = value["metadata"]
    permissions = [
        SkillPermissionRequirementInfo(
            resource=item["resource"],
            action=item["action"],
            targetParam=item.get("target_param"),
            optional=bool(item.get("optional", False)),
        )
        for item in metadata.get("permissions", [])
    ]
    metadata_info = SkillMetadataInfo(
        name=metadata["name"],
        version=metadata["version"],
        title=metadata["title"],
        description=metadata["description"],
        tags=list(metadata.get("tags", [])),
        sideEffect=metadata["side_effect"],
        confirmationRequired=bool(metadata.get("confirmation_required", False)),
        idempotent=bool(metadata.get("idempotent", True)),
        supportsDryRun=bool(metadata.get("supports_dry_run", True)),
        visibility=metadata["visibility"],
        permissions=permissions,
    )
    return SkillRegistryEntryInfo(
        id=value["id"],
        workspaceId=value.get("workspaceId"),
        category=_to_category(value.get("category")),
        metadata=metadata_info,
        inputSchema=value.get("inputSchema") or {},
        outputSchema=value.get("outputSchema") or {},
        visibility=value["visibility"],
        sourceType=value["sourceType"],
        runtimeKind=value["runtimeKind"],
        entrypoint=value.get("entrypoint"),
        modulePath=value.get("modulePath"),
        handlerName=value.get("handlerName"),
        icon=value.get("icon"),
        latestVersion=value["latestVersion"],
        status=value["status"],
        isEnabled=bool(value["isEnabled"]),
        openaiToolSchema=value.get("openaiToolSchema"),
        mcpToolSchema=value.get("mcpToolSchema"),
        transportConfig=value.get("transportConfig") or {},
        cacheTtlSeconds=int(value.get("cacheTtlSeconds", 300)),
        embeddingStatus=value.get("embeddingStatus", "pending"),
        embeddingText=value.get("embeddingText"),
        startedAt=value.get("startedAt"),
        stoppedAt=value.get("stoppedAt"),
        createdAt=value.get("createdAt"),
        updatedAt=value.get("updatedAt"),
    )


@strawberry.type
class SkillQueries:
    @strawberry.field(name="skillManifest")
    async def skill_manifest(
        self,
        info: Info,
        workspaceId: Optional[str] = None,
        search: Optional[str] = None,
        categorySlug: Optional[str] = None,
        includeDisabled: bool = False,
    ) -> list[SkillRegistryEntryInfo]:
        await _require_user(info)
        db = info.context["db"]
        items = await skill_registry_service.list_manifest(
            db,
            workspace_id=workspaceId,
            search=search,
            category_slug=categorySlug,
            include_disabled=includeDisabled,
        )
        return [_to_skill_entry(item) for item in items]

    @strawberry.field(name="skillCategories")
    async def skill_categories(self, info: Info) -> list[SkillCategoryInfo]:
        await _require_user(info)
        db = info.context["db"]
        items = await skill_registry_service.list_categories(db)
        return [_to_category(item) for item in items if item]
