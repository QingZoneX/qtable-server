from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.automation import (
    AutomationError,
    get_automation,
    get_automation_execution,
    list_automation_executions,
    list_automations,
    preview_automation,
)
from app.services.automation.repository import serialize_automation


def _require_db_backend() -> None:
    if _resolve_backend(None) != "db":
        raise GraphQLError("Automations require database backend")


@strawberry.type
class AutomationQueries:
    @strawberry.field(name="automations")
    async def automations(
        self,
        info: Info,
        workspace_id: Optional[strawberry.ID] = None,
        table_id: Optional[strawberry.ID] = None,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        try:
            return await list_automations(
                info.context["db"],
                user_id=user.id,
                workspace_id=str(workspace_id) if workspace_id is not None else None,
                table_id=str(table_id) if table_id is not None else None,
            )
        except (AutomationError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="automation")
    async def automation(
        self,
        info: Info,
        automation_id: strawberry.ID,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        try:
            rule = await get_automation(
                info.context["db"],
                user_id=user.id,
                automation_id=str(automation_id),
            )
            return serialize_automation(rule)
        except (AutomationError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="automationPreview")
    async def automation_preview(
        self,
        info: Info,
        automation_id: strawberry.ID,
        record_id: Optional[strawberry.ID] = None,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        try:
            return await preview_automation(
                info.context["db"],
                user_id=user.id,
                automation_id=str(automation_id),
                record_id=str(record_id) if record_id is not None else None,
            )
        except (AutomationError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="automationExecutions")
    async def automation_executions(
        self,
        info: Info,
        automation_id: strawberry.ID,
        status: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        try:
            return await list_automation_executions(
                info.context["db"],
                user_id=user.id,
                automation_id=str(automation_id),
                status=status,
                offset=offset,
                limit=limit,
            )
        except (AutomationError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="automationExecution")
    async def automation_execution(
        self,
        info: Info,
        execution_id: strawberry.ID,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        try:
            return await get_automation_execution(
                info.context["db"],
                user_id=user.id,
                execution_id=str(execution_id),
            )
        except (AutomationError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
