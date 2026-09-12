from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clipper import _require_clipper_table_permission, _task_url, _user_table_ids
from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.qnote_task_snapshot import build_qnote_task_snapshot
from app.services.row_permissions import require_record_access
from app.services.smart_table_store import get_full_store


router = APIRouter(prefix="/api/clipper", tags=["qnote-integration"])


@router.get("/tasks/{task_id}/snapshot")
async def get_qnote_task_snapshot(
    task_id: str,
    target_table_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the minimal task projection QNote is allowed to display.

    Both missing and inaccessible tables/rows resolve to 404 so callers cannot
    distinguish deletion from permission loss. The response is only assembled
    after table and row access checks have succeeded.
    """

    tables = await _user_table_ids(db, user.id)
    table = next((item for item in tables if item.id == target_table_id), None)
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="QTable task is unavailable",
        )

    try:
        table_permission = await _require_clipper_table_permission(
            db,
            user,
            table.id,
            "read",
        )
        record_model = await require_record_access(
            db,
            table.id,
            task_id,
            user_id=user.id,
            table_permission=table_permission,
        )
    except (PermissionError, HTTPException) as exc:
        if isinstance(exc, HTTPException) and exc.status_code not in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        }:
            raise
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="QTable task is unavailable",
        ) from exc

    store = await get_full_store(db, table.id)
    return await build_qnote_task_snapshot(
        db,
        table=table,
        record_model=record_model,
        fields=list(store.get("fields", [])),
        qtable_url=_task_url(table.id, record_model.id),
    )
