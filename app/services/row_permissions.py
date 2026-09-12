"""Row-level permission policy and record visibility helpers.

Row-level permissions only restrict an existing table permission; they never
grant access that the workspace/item permission layer denied.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, TableRecord, TableRowPermissionPolicy
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)
from app.services.relation_engine import (
    RelationValidationError,
    get_target_table_id,
    is_relation_field,
    relation_allows_multiple,
    relation_record_ids,
)


ROW_PERMISSION_MODES = {"all", "creator", "member_field"}
DEFAULT_ROW_PERMISSION_POLICY: Dict[str, Any] = {
    "mode": "all",
    "memberFieldId": None,
    "enabled": False,
}


def _policy_dict(policy: Optional[TableRowPermissionPolicy]) -> Dict[str, Any]:
    if not policy:
        return dict(DEFAULT_ROW_PERMISSION_POLICY)
    mode = policy.mode if policy.mode in ROW_PERMISSION_MODES else "all"
    member_field_id = policy.member_field_id if mode == "member_field" else None
    return {
        "mode": mode,
        "memberFieldId": member_field_id,
        "enabled": mode != "all",
    }


async def get_row_permission_policy(
    db: AsyncSession,
    table_id: str,
) -> Dict[str, Any]:
    result = await db.execute(
        select(TableRowPermissionPolicy)
        .where(TableRowPermissionPolicy.table_id == table_id)
        .limit(1)
    )
    return _policy_dict(result.scalars().first())


async def set_row_permission_policy(
    db: AsyncSession,
    table_id: str,
    *,
    mode: str,
    member_field_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ROW_PERMISSION_MODES:
        raise ValueError("Invalid row permission mode")

    normalized_field_id: Optional[str] = None
    if normalized_mode == "member_field":
        if not member_field_id:
            raise ValueError("Member field is required")
        result_field = await db.execute(
            select(TableField).where(
                TableField.table_id == table_id,
                TableField.id == str(member_field_id),
            )
        )
        field = result_field.scalars().first()
        if not field:
            raise ValueError("Member field does not exist")
        if field.type != "member":
            raise ValueError("Row permission field must be a member field")
        normalized_field_id = field.id

    result = await db.execute(
        select(TableRowPermissionPolicy)
        .where(TableRowPermissionPolicy.table_id == table_id)
        .limit(1)
    )
    current = result.scalars().first()
    if current:
        current.mode = normalized_mode
        current.member_field_id = normalized_field_id
    else:
        current = TableRowPermissionPolicy(
            table_id=table_id,
            mode=normalized_mode,
            member_field_id=normalized_field_id,
        )
        db.add(current)

    await db.commit()
    return _policy_dict(current)


async def delete_row_permission_policy(db: AsyncSession, table_id: str) -> None:
    result = await db.execute(
        select(TableRowPermissionPolicy)
        .where(TableRowPermissionPolicy.table_id == table_id)
        .limit(1)
    )
    current = result.scalars().first()
    if current:
        await db.delete(current)
        await db.commit()


def _member_value_contains_user(value: Any, user_id: int) -> bool:
    expected = str(user_id)

    if isinstance(value, list):
        return any(_member_value_contains_user(item, user_id) for item in value)

    if isinstance(value, dict):
        for key in ("id", "userId", "user_id", "value"):
            candidate = value.get(key)
            if candidate is not None and str(candidate) == expected:
                return True
        return False

    if value is None or isinstance(value, bool):
        return False

    return str(value) == expected


def record_is_visible(
    *,
    policy: Dict[str, Any],
    user_id: int,
    table_permission: str,
    created_by_user_id: Optional[int],
    data: Optional[Dict[str, Any]],
) -> bool:
    # Item managers always retain full-table visibility so a restrictive rule
    # cannot lock administrators out of the table.
    if permission_allows(table_permission, "manage"):
        return True

    mode = str(policy.get("mode") or "all")
    if mode == "all":
        return True

    if created_by_user_id is not None and int(created_by_user_id) == int(user_id):
        return True

    if mode == "creator":
        return False

    if mode == "member_field":
        field_id = policy.get("memberFieldId")
        if not isinstance(field_id, str) or not field_id:
            # Invalid/stale policies fail closed for non-managers.
            return False
        return _member_value_contains_user((data or {}).get(field_id), user_id)

    # Unknown policy modes fail closed for non-managers.
    return False


async def allowed_record_ids(
    db: AsyncSession,
    table_id: str,
    *,
    user_id: int,
    table_permission: str,
    policy: Optional[Dict[str, Any]] = None,
) -> Set[str]:
    effective_policy = policy or await get_row_permission_policy(db, table_id)
    if (
        effective_policy.get("mode") == "all"
        or permission_allows(table_permission, "manage")
    ):
        result = await db.execute(
            select(TableRecord.id).where(TableRecord.table_id == table_id)
        )
        return {str(value) for value in result.scalars().all()}

    result = await db.execute(
        select(TableRecord).where(TableRecord.table_id == table_id)
    )
    return {
        str(record.id)
        for record in result.scalars().all()
        if record_is_visible(
            policy=effective_policy,
            user_id=user_id,
            table_permission=table_permission,
            created_by_user_id=record.created_by_user_id,
            data=dict(record.data or {}),
        )
    }


async def sanitize_relation_values_for_user(
    db: AsyncSession,
    fields: Sequence[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    *,
    user_id: int,
) -> List[Dict[str, Any]]:
    """Remove relation record IDs the current user cannot read.

    The source row may legitimately remain visible while a linked target row is
    hidden by the target table's row policy. Returning the raw target ID would
    still leak a record reference, so inaccessible links are masked.
    """
    relation_fields = [
        dict(field)
        for field in fields
        if isinstance(field, dict) and is_relation_field(field)
    ]
    if not relation_fields or not records:
        return [dict(record) for record in records]

    allowed_by_target: Dict[str, Optional[Set[str]]] = {}
    for field in relation_fields:
        target_table_id = get_target_table_id(field)
        if not target_table_id or target_table_id in allowed_by_target:
            continue

        permission = await get_effective_permission_for_item(
            db,
            user_id,
            target_table_id,
        )
        if not permission_allows(permission, "read"):
            allowed_by_target[target_table_id] = set()
            continue

        target_policy = await get_row_permission_policy(db, target_table_id)
        if row_permission_restricts_user(target_policy, permission):
            allowed_by_target[target_table_id] = await allowed_record_ids(
                db,
                target_table_id,
                user_id=user_id,
                table_permission=permission,
                policy=target_policy,
            )
        else:
            # None means every target record is readable; avoid loading IDs.
            allowed_by_target[target_table_id] = None

    sanitized_records: List[Dict[str, Any]] = []
    for record in records:
        next_record = dict(record)
        for field in relation_fields:
            field_id = str(field.get("id") or "")
            if not field_id or field_id not in next_record:
                continue
            target_table_id = get_target_table_id(field)
            allowed = allowed_by_target.get(target_table_id)
            if allowed is None:
                continue

            try:
                linked_ids = relation_record_ids(field, next_record.get(field_id))
            except RelationValidationError:
                # Legacy malformed relation values fail closed at read time.
                linked_ids = []

            visible_ids = [
                record_id
                for record_id in linked_ids
                if str(record_id) in (allowed or set())
            ]
            next_record[field_id] = (
                visible_ids
                if relation_allows_multiple(field)
                else (visible_ids[0] if visible_ids else None)
            )
        sanitized_records.append(next_record)

    return sanitized_records


async def filter_store_for_user(
    db: AsyncSession,
    table_id: str,
    store: Dict[str, Any],
    *,
    user_id: int,
    table_permission: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    policy = await get_row_permission_policy(db, table_id)
    source_records = list(store.get("records", []))

    if row_permission_restricts_user(policy, table_permission):
        allowed = await allowed_record_ids(
            db,
            table_id,
            user_id=user_id,
            table_permission=table_permission,
            policy=policy,
        )
        source_records = [
            record
            for record in source_records
            if str(record.get("id")) in allowed
        ]

    sanitized_records = await sanitize_relation_values_for_user(
        db,
        list(store.get("fields", [])),
        source_records,
        user_id=user_id,
    )
    next_store = dict(store)
    next_store["records"] = sanitized_records
    return next_store, policy


async def require_record_access(
    db: AsyncSession,
    table_id: str,
    record_id: str,
    *,
    user_id: int,
    table_permission: str,
) -> TableRecord:
    result = await db.execute(
        select(TableRecord).where(
            TableRecord.table_id == table_id,
            TableRecord.id == str(record_id),
        )
    )
    record = result.scalars().first()
    if not record:
        # Deliberately use the same error for missing and inaccessible records
        # so callers cannot probe hidden record IDs.
        raise PermissionError("Record not found or no access")

    policy = await get_row_permission_policy(db, table_id)
    if not record_is_visible(
        policy=policy,
        user_id=user_id,
        table_permission=table_permission,
        created_by_user_id=record.created_by_user_id,
        data=dict(record.data or {}),
    ):
        raise PermissionError("Record not found or no access")
    return record


async def ensure_permission_field_change_safe(
    db: AsyncSession,
    table_id: str,
    field_id: str,
    *,
    deleting: bool = False,
    next_type: Optional[str] = None,
) -> None:
    policy = await get_row_permission_policy(db, table_id)
    if (
        policy.get("mode") != "member_field"
        or policy.get("memberFieldId") != field_id
    ):
        return

    if deleting:
        raise ValueError(
            "This member field is used by row permissions. Change the row permission rule first."
        )
    if next_type is not None and next_type != "member":
        raise ValueError(
            "This member field is used by row permissions and must remain a member field."
        )


def row_permission_restricts_user(
    policy: Dict[str, Any],
    table_permission: str,
) -> bool:
    return (
        policy.get("mode") != "all"
        and not permission_allows(table_permission, "manage")
    )


def row_scope_cache_key(
    policy: Dict[str, Any],
    *,
    user_id: int,
    table_permission: str,
) -> str:
    if policy.get("mode") == "all" or permission_allows(table_permission, "manage"):
        return "all"
    return f"user:{user_id}:{policy.get('mode')}:{policy.get('memberFieldId') or '-'}"
