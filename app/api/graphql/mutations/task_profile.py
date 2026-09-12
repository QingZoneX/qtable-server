import copy

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _require_user,
    _resolve_backend,
    _resolve_db_table_id,
    publish_table_update,
)
from app.services.change_history import append_change_set
from app.services.task_profile import (
    TaskProfileValidationError,
    clear_task_profile,
    get_task_profile_record,
    save_task_profile,
    serialize_task_profile,
)
from app.services.task_profile_portable import (
    clear_profile_annotations,
    sync_profile_annotations,
)


def _require_task_profile_db(table_id: str) -> str:
    if _resolve_backend(table_id) != "db":
        raise GraphQLError("Task profile requires database backend")
    return _resolve_db_table_id(table_id)


@strawberry.type
class TaskProfileMutations:
    @strawberry.mutation(name="updateTaskProfile")
    async def update_task_profile(
        self,
        info: Info,
        table_id: str,
        profile: JSON,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        resolved_table_id = _require_task_profile_db(table_id)
        await _require_item_permission(info, resolved_table_id, "edit")
        user = await _require_user(info)

        existing = await get_task_profile_record(db, resolved_table_id)
        before = copy.deepcopy(existing.config) if existing is not None else None
        try:
            saved_payload = await save_task_profile(
                db,
                table_id=resolved_table_id,
                config=profile,
                user_id=user.id,
                commit=False,
            )
            await sync_profile_annotations(
                db,
                table_id=resolved_table_id,
                config=saved_payload["config"],
            )
            saved = await get_task_profile_record(db, resolved_table_id)
            after = copy.deepcopy(saved.config) if saved is not None else None
            await append_change_set(
                db,
                table_id=resolved_table_id,
                actor_id=user.id,
                operation="update_task_profile",
                source="graphql",
                summary="Updated table business semantic profile",
                items=[
                    {
                        "entity_type": "task_profile",
                        "entity_id": resolved_table_id,
                        "before_data": before,
                        "after_data": after,
                    }
                ],
            )
            await db.commit()
        except TaskProfileValidationError as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc
        except Exception:
            await db.rollback()
            raise

        result = await serialize_task_profile(db, resolved_table_id)
        assert result is not None
        await publish_table_update(db, resolved_table_id)
        return result

    @strawberry.mutation(name="clearTaskProfile")
    async def clear_task_profile_mutation(
        self,
        info: Info,
        table_id: str,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        resolved_table_id = _require_task_profile_db(table_id)
        await _require_item_permission(info, resolved_table_id, "edit")
        user = await _require_user(info)

        existing = await get_task_profile_record(db, resolved_table_id)
        if existing is None:
            return False
        before = copy.deepcopy(existing.config)
        try:
            await clear_task_profile(
                db,
                table_id=resolved_table_id,
                commit=False,
            )
            await clear_profile_annotations(db, table_id=resolved_table_id)
            await append_change_set(
                db,
                table_id=resolved_table_id,
                actor_id=user.id,
                operation="clear_task_profile",
                source="graphql",
                summary="Cleared table business semantic profile",
                items=[
                    {
                        "entity_type": "task_profile",
                        "entity_id": resolved_table_id,
                        "before_data": before,
                        "after_data": None,
                    }
                ],
            )
            await db.commit()
        except TaskProfileValidationError as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc
        except Exception:
            await db.rollback()
            raise

        await publish_table_update(db, resolved_table_id)
        return True
