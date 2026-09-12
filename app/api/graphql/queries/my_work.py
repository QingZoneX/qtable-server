import logging
from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.my_work import MyWorkValidationError, build_my_work
from app.services.my_work_kpi_fallback import build_my_work_kpi_fallback


logger = logging.getLogger(__name__)


@strawberry.type
class MyWorkQueries:
    @strawberry.field(name="myWork")
    async def my_work(
        self,
        info: Info,
        sections: Optional[List[str]] = None,
        limit: int = 20,
        cursors: Optional[JSON] = None,
        timezone: str = "UTC",
        task_state: str = "all",
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("My Work requires database backend")
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            payload = await build_my_work(
                db,
                user_id=user.id,
                sections=sections,
                limit=limit,
                cursors=cursors,
                timezone_name=timezone,
                task_state=task_state,
            )
        except MyWorkValidationError as exc:
            raise GraphQLError(str(exc)) from exc

        kpi_section = payload.get("sections", {}).get("kpi")
        if isinstance(kpi_section, dict) and kpi_section.get("status") == "error":
            try:
                # Keep the normal SQL path as the fast path. The fallback is
                # deliberately invoked only after section isolation reported a
                # data/runtime failure and continues to enforce current row
                # permissions and configured Task Profile semantics.
                async with db.begin_nested():
                    fallback = await build_my_work_kpi_fallback(
                        db,
                        user_id=user.id,
                        timezone_name=timezone,
                    )
                payload["sections"]["kpi"] = {
                    "status": "ok",
                    "data": fallback,
                }
            except Exception:
                logger.exception("My Work KPI fallback failed")
                # Preserve the original isolated error rather than returning
                # fabricated/partial KPI values.

        return payload
