from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend, publish_table_update
from app.schemas.source_inbox import (
    SourceInboxConvertRequest,
    SourceInboxIngestRequest,
    SourceInboxPreviewRequest,
    SourceInboxStatusRequest,
)
from app.services.source_inbox import SourceInboxError
from app.services.source_inbox_unified import source_inbox_service
from app.services.task_duplicates import task_duplicate_service


@strawberry.type
class SourceInboxMutations:
    @strawberry.mutation(name="ingestSourceInboxItem")
    async def ingest_source_inbox_item(
        self,
        info: Info,
        workspace_id: str,
        source: JSON,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            request = SourceInboxIngestRequest.model_validate(
                {"workspaceId": workspace_id, "source": source}
            )
            return await source_inbox_service.ingest(
                info.context["db"], user_id=user.id, request=request
            )
        except (SourceInboxError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="previewSourceInboxItem")
    async def preview_source_inbox_item(
        self,
        info: Info,
        item_id: str,
        target_table_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            result = await source_inbox_service.preview(
                info.context["db"],
                user_id=user.id,
                request=SourceInboxPreviewRequest.model_validate(
                    {
                        "itemId": item_id,
                        "targetTableId": target_table_id,
                        "model": model,
                    }
                ),
            )
            result["duplicateThresholds"] = task_duplicate_service.thresholds()
            result["duplicatePermissionScope"] = {
                "rowVisibilityApplied": True,
                "hiddenRecordsExcluded": True,
                "advisoryOnly": True,
            }
            return result
        except (SourceInboxError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="convertSourceInboxItem")
    async def convert_source_inbox_item(
        self,
        info: Info,
        item_id: str,
        target_table_id: str,
        title: str,
        description: str = "",
        priority: str = "medium",
        assignee_user_id: Optional[int] = None,
        workload_hours: Optional[float] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            result = await source_inbox_service.convert(
                info.context["db"],
                user_id=user.id,
                request=SourceInboxConvertRequest.model_validate(
                    {
                        "itemId": item_id,
                        "targetTableId": target_table_id,
                        "title": title,
                        "description": description,
                        "priority": priority,
                        "assigneeUserId": assignee_user_id,
                        "workloadHours": workload_hours,
                    }
                ),
            )
        except (SourceInboxError, PermissionError, ValueError) as exc:
            await info.context["db"].rollback()
            raise GraphQLError(str(exc)) from exc
        task = result.get("task") if isinstance(result, dict) else None
        if isinstance(task, dict) and task.get("tableId"):
            await publish_table_update(info.context["db"], str(task["tableId"]))
        return result

    @strawberry.mutation(name="updateSourceInboxStatus")
    async def update_source_inbox_status(
        self,
        info: Info,
        item_ids: list[str],
        status: str,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            return await source_inbox_service.update_status(
                info.context["db"],
                user_id=user.id,
                request=SourceInboxStatusRequest.model_validate(
                    {"itemIds": item_ids, "status": status}
                ),
            )
        except (SourceInboxError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
