from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.automation import (
    AutomationError,
    AutomationValidationError,
    create_automation,
    delete_automation,
    execute_manual_automation,
    retry_automation_execution,
    set_automation_enabled,
    update_automation,
    validate_automation_definition,
)
from app.services.automation.repository import serialize_automation
from app.services.workspace import get_effective_permission_for_item, permission_allows


def _require_db_backend() -> None:
    if _resolve_backend(None) != "db":
        raise GraphQLError("Automations require database backend")


async def _require_table_edit(db, *, user_id: int, table_id: str) -> None:
    permission = await get_effective_permission_for_item(db, user_id, table_id)
    if not permission_allows(permission, "edit"):
        raise PermissionError("Table not found or no access")


@strawberry.type
class AutomationMutations:
    @strawberry.mutation(name="validateAutomation")
    async def validate_automation(
        self,
        info: Info,
        table_id: strawberry.ID,
        trigger: JSON,
        conditions: JSON,
        actions: JSON,
        timezone: str = "UTC",
        max_retries: int = 3,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            await _require_table_edit(db, user_id=user.id, table_id=str(table_id))
            normalized = await validate_automation_definition(
                db,
                table_id=str(table_id),
                trigger=trigger,
                conditions=conditions,
                actions=actions,
                timezone=timezone,
                max_retries=max_retries,
            )
            return {"valid": True, **normalized}
        except (AutomationError, AutomationValidationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="createAutomation")
    async def create_automation_mutation(
        self,
        info: Info,
        table_id: strawberry.ID,
        name: str,
        trigger: JSON,
        conditions: JSON,
        actions: JSON,
        description: Optional[str] = None,
        timezone: str = "UTC",
        max_retries: int = 3,
        enabled: bool = False,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            rule = await create_automation(
                db,
                user_id=user.id,
                table_id=str(table_id),
                name=name,
                trigger=trigger,
                conditions=conditions,
                actions=actions,
                description=description,
                timezone_name=timezone,
                max_retries=max_retries,
                enabled=enabled,
            )
            return serialize_automation(rule)
        except (AutomationError, AutomationValidationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="updateAutomation")
    async def update_automation_mutation(
        self,
        info: Info,
        automation_id: strawberry.ID,
        name: Optional[str] = None,
        description: Optional[str] = None,
        trigger: Optional[JSON] = None,
        conditions: Optional[JSON] = None,
        actions: Optional[JSON] = None,
        timezone: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            rule = await update_automation(
                db,
                user_id=user.id,
                automation_id=str(automation_id),
                name=name,
                description=description,
                trigger=trigger,
                conditions=conditions,
                actions=actions,
                timezone_name=timezone,
                max_retries=max_retries,
            )
            return serialize_automation(rule)
        except (AutomationError, AutomationValidationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="setAutomationEnabled")
    async def set_automation_enabled_mutation(
        self,
        info: Info,
        automation_id: strawberry.ID,
        enabled: bool,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            rule = await set_automation_enabled(
                db,
                user_id=user.id,
                automation_id=str(automation_id),
                enabled=enabled,
            )
            return serialize_automation(rule)
        except (AutomationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="deleteAutomation")
    async def delete_automation_mutation(
        self,
        info: Info,
        automation_id: strawberry.ID,
    ) -> bool:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            return await delete_automation(
                db,
                user_id=user.id,
                automation_id=str(automation_id),
            )
        except (AutomationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="runAutomation")
    async def run_automation(
        self,
        info: Info,
        automation_id: strawberry.ID,
        record_id: Optional[strawberry.ID] = None,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            return await execute_manual_automation(
                db,
                user_id=user.id,
                automation_id=str(automation_id),
                record_id=str(record_id) if record_id is not None else None,
            )
        except (AutomationError, AutomationValidationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="retryAutomationExecution")
    async def retry_automation_execution_mutation(
        self,
        info: Info,
        execution_id: strawberry.ID,
    ) -> JSON:
        _require_db_backend()
        user = await _require_user(info)
        db = info.context["db"]
        try:
            return await retry_automation_execution(
                db,
                user_id=user.id,
                execution_id=str(execution_id),
            )
        except (AutomationError, AutomationValidationError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc
