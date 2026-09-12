from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.task_duplicates import TaskDuplicateError, task_duplicate_service


@strawberry.type
class TaskDuplicateQueries:
    @strawberry.field(name="taskDuplicateCandidates")
    async def task_duplicate_candidates(
        self,
        info: Info,
        table_id: str,
        title: str,
        description: str = "",
        source_quote: str = "",
        source_url: str = "",
        source_id: str = "",
        tags: Optional[list[str]] = None,
        exclude_record_ids: Optional[list[str]] = None,
        limit: int = 5,
    ) -> JSON:
        """Preview permission-safe duplicate candidates before creating a task.

        The operation is advisory only: it never writes, merges or blocks task
        creation. Callers can offer reuse/merge/create choices based on the
        returned candidate metadata.
        """
        if _resolve_backend(None) != "db":
            raise GraphQLError("Task duplicate detection requires database backend")
        user = await _require_user(info)
        try:
            return await task_duplicate_service.scan_table(
                info.context["db"],
                user_id=user.id,
                table_id=table_id,
                title=title,
                description=description,
                source_quote=source_quote,
                source_url=source_url,
                source_id=source_id,
                tags=tags or [],
                exclude_record_ids={str(value) for value in (exclude_record_ids or [])},
                limit=limit,
            )
        except (TaskDuplicateError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
