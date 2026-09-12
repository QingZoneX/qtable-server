from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField
from app.services.task_profile_portable import ROLE_PROPERTY


async def remap_copied_task_profile_relations(
    db: AsyncSession,
    *,
    source_table_id: str,
    target_table_id: str,
) -> None:
    """Retarget explicitly semantic self-relations after a table copy.

    QTable's generic table-copy path historically copies field properties
    verbatim. For Task Profile parent/dependency fields, a self-relation must
    point to the copied table, not back to the source table. Restricting this
    repair to fields explicitly tagged with task semantic roles avoids changing
    unrelated cross-table relation behavior.
    """
    result = await db.execute(
        select(TableField).where(TableField.table_id == target_table_id)
    )
    changed = False
    for field in result.scalars().all():
        prop = dict(field.property or {})
        if prop.get(ROLE_PROPERTY) not in {"parent", "dependency"}:
            continue
        if prop.get("targetTableId") != source_table_id:
            continue
        prop["targetTableId"] = target_table_id
        field.property = prop
        changed = True
    if changed:
        await db.flush()
