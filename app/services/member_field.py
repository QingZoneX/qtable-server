from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import WorkspaceMember


class MemberFieldValidationError(ValueError):
    pass


def member_allows_multiple(field_or_property: Any) -> bool:
    if isinstance(field_or_property, TableField):
        prop = field_or_property.property
    elif isinstance(field_or_property, Mapping):
        prop = field_or_property.get("property", field_or_property)
    else:
        prop = None
    if not isinstance(prop, Mapping):
        return True
    raw = prop.get("multiple", True)
    if not isinstance(raw, bool):
        raise MemberFieldValidationError("Member field property.multiple must be boolean")
    return raw


def normalize_member_property(value: Any) -> Dict[str, Any]:
    prop = dict(value) if isinstance(value, Mapping) else {}
    raw = prop.get("multiple", True)
    if not isinstance(raw, bool):
        raise MemberFieldValidationError("Member field property.multiple must be boolean")
    prop["multiple"] = raw
    return prop


def normalize_member_field_definition(field_data: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(field_data)
    if str(result.get("type") or "") != "member":
        return result
    result["options"] = None
    result["property"] = normalize_member_property(result.get("property"))
    return result


async def _workspace_id_for_table(db: AsyncSession, table_id: str) -> str:
    result = await db.execute(
        select(WorkspaceItem.workspace_id).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.type == "table",
        )
    )
    workspace_id = result.scalar()
    if not workspace_id:
        raise MemberFieldValidationError("Table workspace not found")
    return str(workspace_id)


async def workspace_member_options_for_table(
    db: AsyncSession,
    table_id: str,
) -> list[Dict[str, Any]]:
    workspace_id = await _workspace_id_for_table(db, table_id)
    result = await db.execute(
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(User.name, User.email, User.id)
    )
    options: list[Dict[str, Any]] = []
    for user, member in result.all():
        label = str(user.name or user.email or user.id)
        role = member.role.value if hasattr(member.role, "value") else str(member.role)
        options.append(
            {
                "id": str(user.id),
                "label": label,
                "color": "#F3F4F6",
                "email": user.email,
                "role": role,
            }
        )
    return options


async def hydrate_member_fields_for_table(
    db: AsyncSession,
    table_id: str,
    fields: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    if not any(str(field.get("type") or "") == "member" for field in fields):
        return [dict(field) for field in fields]

    options = await workspace_member_options_for_table(db, table_id)
    output: list[Dict[str, Any]] = []
    for raw in fields:
        field = dict(raw)
        if str(field.get("type") or "") == "member":
            field["options"] = [dict(item) for item in options]
            field["property"] = normalize_member_property(field.get("property"))
        output.append(field)
    return output


def _extract_member_ids(value: Any) -> list[str]:
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_extract_member_ids(item))
        return list(dict.fromkeys(output))
    if isinstance(value, dict):
        candidate = (
            value.get("id")
            or value.get("userId")
            or value.get("user_id")
            or value.get("value")
        )
        return _extract_member_ids(candidate)
    if isinstance(value, bool):
        raise MemberFieldValidationError("Member value must reference a workspace user")
    candidate = str(value).strip()
    return [candidate] if candidate else []


def _normalize_member_value(
    field: TableField,
    value: Any,
    allowed_user_ids: set[str],
) -> Any:
    ids = _extract_member_ids(value)
    invalid = [user_id for user_id in ids if user_id not in allowed_user_ids]
    if invalid:
        raise MemberFieldValidationError(
            f"Member {invalid[0]} is not a current workspace member"
        )

    multiple = member_allows_multiple(field)
    if not multiple and len(ids) > 1:
        raise MemberFieldValidationError(
            f"Member field {field.name} allows only one user"
        )
    return ids if multiple else (ids[0] if ids else None)


async def normalize_member_values_in_payloads(
    db: AsyncSession,
    table_id: str,
    payloads: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    output = [dict(payload) for payload in payloads]
    field_ids = {
        str(key)
        for payload in output
        for key in payload.keys()
        if key != "id"
    }
    if not field_ids:
        return output

    result = await db.execute(
        select(TableField).where(
            TableField.table_id == table_id,
            TableField.type == "member",
            TableField.id.in_(field_ids),
        )
    )
    member_fields = {field.id: field for field in result.scalars().all()}
    if not member_fields:
        return output

    options = await workspace_member_options_for_table(db, table_id)
    allowed = {str(item["id"]) for item in options}
    for payload in output:
        for field_id, field in member_fields.items():
            if field_id not in payload:
                continue
            payload[field_id] = _normalize_member_value(
                field,
                payload.get(field_id),
                allowed,
            )
    return output


async def normalize_member_value_for_field(
    db: AsyncSession,
    table_id: str,
    field_id: str,
    value: Any,
) -> Any:
    payload = await normalize_member_values_in_payloads(
        db,
        table_id,
        [{field_id: value}],
    )
    return payload[0].get(field_id)


def _legacy_member_count(value: Any) -> int:
    return len(_extract_member_ids(value))


async def ensure_member_field_change_safe(
    db: AsyncSession,
    table_id: str,
    field_id: str,
    *,
    old_type: str,
    next_type: str,
    next_property: Any,
) -> None:
    if next_type != "member":
        return

    multiple = member_allows_multiple({"property": normalize_member_property(next_property)})
    result = await db.execute(
        select(TableRecord.id, TableRecord.data).where(
            TableRecord.table_id == table_id
        )
    )
    rows = result.all()

    if old_type != "member":
        non_empty = [
            str(record_id)
            for record_id, data in rows
            if isinstance(data, dict)
            and data.get(field_id) not in (None, "", [])
        ]
        if non_empty:
            raise MemberFieldValidationError(
                "Convert to member field requires existing values to be empty"
            )
        return

    if multiple:
        return

    for record_id, data in rows:
        value = data.get(field_id) if isinstance(data, dict) else None
        if _legacy_member_count(value) > 1:
            raise MemberFieldValidationError(
                f"Cannot switch member field to single-select while record {record_id} has multiple members"
            )
