from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _require_user,
    _resolve_backend,
)
from app.services.table_templates import (
    TemplateValidationError,
    create_custom_template,
    delete_custom_template,
    get_manageable_custom_template,
    serialize_template,
    set_custom_template_archived,
    update_custom_template,
)


def _require_template_database_backend() -> None:
    if _resolve_backend(None) != "db":
        raise GraphQLError("Custom templates require database backend")


@strawberry.type
class TemplateMutations:
    """Commercial personal/workspace template lifecycle mutations."""

    @strawberry.mutation(name="saveTableAsTemplate")
    async def save_table_as_template(
        self,
        info: Info,
        source_table_id: str,
        name: str,
        scope: str = "personal",
        workspace_id: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        icon: Optional[str] = None,
        include_records: bool = False,
    ) -> JSON:
        _require_template_database_backend()
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        await _require_item_permission(info, source_table_id, "read")
        try:
            template = await create_custom_template(
                db,
                source_table_id=source_table_id,
                name=name,
                scope=scope,
                user_id=user.id,
                workspace_id=workspace_id,
                description=description,
                category=category,
                tags=tags,
                icon=icon,
                include_records=include_records,
            )
        except TemplateValidationError as exc:
            raise GraphQLError(str(exc)) from exc
        return serialize_template(template, include_snapshot=True)

    @strawberry.mutation(name="updateTableTemplate")
    async def update_table_template(
        self,
        info: Info,
        template_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        icon: Optional[str] = None,
        source_table_id: Optional[str] = None,
        include_records: Optional[bool] = None,
    ) -> JSON:
        _require_template_database_backend()
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            manageable = await get_manageable_custom_template(
                db,
                template_id,
                user_id=user.id,
            )
            refresh_source_id = source_table_id
            if refresh_source_id is None and include_records is not None:
                refresh_source_id = manageable.source_table_id
            if refresh_source_id:
                await _require_item_permission(info, refresh_source_id, "read")

            template = await update_custom_template(
                db,
                template_id,
                user_id=user.id,
                name=name,
                description=description,
                category=category,
                tags=tags,
                icon=icon,
                source_table_id=source_table_id,
                include_records=include_records,
            )
        except TemplateValidationError as exc:
            raise GraphQLError(str(exc)) from exc
        return serialize_template(template, include_snapshot=True)

    @strawberry.mutation(name="archiveTableTemplate")
    async def archive_table_template(
        self,
        info: Info,
        template_id: str,
        archived: bool = True,
    ) -> JSON:
        _require_template_database_backend()
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            template = await set_custom_template_archived(
                db,
                template_id,
                user_id=user.id,
                archived=archived,
            )
        except TemplateValidationError as exc:
            raise GraphQLError(str(exc)) from exc
        return serialize_template(template, include_snapshot=False)

    @strawberry.mutation(name="deleteTableTemplate")
    async def delete_table_template(
        self,
        info: Info,
        template_id: str,
    ) -> bool:
        _require_template_database_backend()
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            return await delete_custom_template(
                db,
                template_id,
                user_id=user.id,
            )
        except TemplateValidationError as exc:
            raise GraphQLError(str(exc)) from exc
