"""Commercial table-template catalog, validation, and instantiation helpers."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import WorkspaceItem
from app.models.table_template import TableTemplate
from app.models.workspace_member import WorkspaceMember
from app.services.workspace.permissions import permission_allows, role_permission
from app.services.relation_engine import (
    get_target_table_id,
    is_relation_field,
    relation_allows_multiple,
)

SYSTEM_SCOPE = "system"
PERSONAL_SCOPE = "personal"
WORKSPACE_SCOPE = "workspace"
TEMPLATE_SCOPES = {SYSTEM_SCOPE, PERSONAL_SCOPE, WORKSPACE_SCOPE}
ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"
TEMPLATE_STATUSES = {ACTIVE_STATUS, ARCHIVED_STATUS}
SELF_RELATION_TARGET = "$SELF"
ALLOWED_VIEW_TYPES = {"grid", "board", "gantt", "calendar", "gallery", "dashboard"}
MAX_TEMPLATE_RECORDS = 1000
MAX_TEMPLATE_TAGS = 20

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "data", "templates")
TEMPLATE_INDEX_PATH = os.path.join(TEMPLATES_DIR, "index.json")


class TemplateValidationError(ValueError):
    """Raised when a template cannot be safely reused."""


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _content_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_list(value: Any, label: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TemplateValidationError(f"{label} must be a list")
    return value


def _unique_objects(items: Sequence[Any], label: str) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise TemplateValidationError(f"Every {label} entry must be an object")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise TemplateValidationError(f"Every {label} entry requires id")
        if item_id in seen:
            raise TemplateValidationError(f"Duplicate {label} id: {item_id}")
        seen.add(item_id)
        normalized.append(_clone(item))
    return normalized


def _validate_field_reference(
    value: Any,
    field_ids: set[str],
    label: str,
    *,
    optional: bool = True,
) -> None:
    if value is None or value == "":
        if optional:
            return
        raise TemplateValidationError(f"{label} requires a field")
    field_id = str(value)
    if field_id not in field_ids:
        raise TemplateValidationError(f"{label} references missing field: {field_id}")


def _validate_filter_sort_group(
    snapshot: Mapping[str, Any],
    field_ids: set[str],
) -> None:
    for condition in _require_list(snapshot.get("filters"), "filters"):
        if not isinstance(condition, dict):
            raise TemplateValidationError("Every filters entry must be an object")
        _validate_field_reference(
            condition.get("fieldId"),
            field_ids,
            "Filter",
            optional=False,
        )

    for sort in _require_list(snapshot.get("sorts"), "sorts"):
        if not isinstance(sort, dict):
            raise TemplateValidationError("Every sorts entry must be an object")
        _validate_field_reference(
            sort.get("fieldId"),
            field_ids,
            "Sort",
            optional=False,
        )

    group = snapshot.get("groupConfig")
    if group is not None and not isinstance(group, dict):
        raise TemplateValidationError("groupConfig must be an object")
    if isinstance(group, dict):
        _validate_field_reference(group.get("fieldId"), field_ids, "Group")


def _validate_view_config(view: Mapping[str, Any], field_ids: set[str]) -> None:
    view_type = str(view.get("type") or "").strip()
    if view_type not in ALLOWED_VIEW_TYPES:
        raise TemplateValidationError(f"Unsupported view type: {view_type or '<empty>'}")
    config = view.get("config")
    if config is None:
        return
    if not isinstance(config, dict):
        raise TemplateValidationError("View config must be an object")

    for condition in _require_list(config.get("filters"), "view filters"):
        if isinstance(condition, dict):
            _validate_field_reference(
                condition.get("fieldId"),
                field_ids,
                "View filter",
                optional=False,
            )
        else:
            raise TemplateValidationError("Every view filter must be an object")

    for sort in _require_list(config.get("sorts"), "view sorts"):
        if isinstance(sort, dict):
            _validate_field_reference(
                sort.get("fieldId"),
                field_ids,
                "View sort",
                optional=False,
            )
        else:
            raise TemplateValidationError("Every view sort must be an object")

    group = config.get("groupConfig")
    if group is not None and not isinstance(group, dict):
        raise TemplateValidationError("View groupConfig must be an object")
    if isinstance(group, dict):
        _validate_field_reference(group.get("fieldId"), field_ids, "View group")

    for field_id in _require_list(config.get("hiddenFieldIds"), "hiddenFieldIds"):
        _validate_field_reference(field_id, field_ids, "Hidden field", optional=False)

    gantt = config.get("ganttConfig")
    if gantt is not None:
        if not isinstance(gantt, dict):
            raise TemplateValidationError("ganttConfig must be an object")
        for key in ("startFieldId", "endFieldId", "progressFieldId"):
            _validate_field_reference(gantt.get(key), field_ids, f"Gantt {key}")

    calendar = config.get("calendarConfig")
    if calendar is not None:
        if not isinstance(calendar, dict):
            raise TemplateValidationError("calendarConfig must be an object")
        for key in ("dateFieldId", "startFieldId", "endFieldId"):
            _validate_field_reference(calendar.get(key), field_ids, f"Calendar {key}")

    gallery = config.get("galleryConfig")
    if gallery is not None:
        if not isinstance(gallery, dict):
            raise TemplateValidationError("galleryConfig must be an object")
        for key in ("coverFieldId", "titleFieldId"):
            _validate_field_reference(gallery.get(key), field_ids, f"Gallery {key}")


def normalize_template_snapshot(
    raw_snapshot: Mapping[str, Any],
    *,
    source_table_id: Optional[str] = None,
    include_records: bool = False,
) -> Dict[str, Any]:
    """Validate and sanitize a reusable single-table snapshot.

    Cross-table relations are intentionally rejected in v1. A relation that
    points back to the source table is converted into the portable $SELF token.
    Member options are removed because workspace membership is resolved in the
    destination workspace rather than copied from the source.
    """
    if not isinstance(raw_snapshot, Mapping):
        raise TemplateValidationError("Template snapshot must be an object")

    fields = _unique_objects(
        _require_list(raw_snapshot.get("fields"), "fields"),
        "field",
    )
    if not fields:
        raise TemplateValidationError("Template requires at least one field")

    field_ids = {str(field["id"]) for field in fields}
    normalized_fields: List[Dict[str, Any]] = []
    for field in fields:
        field_type = str(field.get("type") or "").strip()
        if not field_type:
            raise TemplateValidationError(f"Field {field['id']} requires type")
        field["name"] = str(field.get("name") or field["id"])

        if field_type == "member":
            # Static user choices are workspace-specific and therefore unsafe
            # to carry into a reusable template.
            field.pop("options", None)

        if is_relation_field(field):
            target = get_target_table_id(field)
            if target == SELF_RELATION_TARGET:
                pass
            elif source_table_id and target == source_table_id:
                prop = dict(field.get("property") or {})
                prop["targetTableId"] = SELF_RELATION_TARGET
                field["property"] = prop
            else:
                raise TemplateValidationError(
                    "Single-table templates cannot contain relations to another table"
                )
        normalized_fields.append(field)

    views = _unique_objects(
        _require_list(raw_snapshot.get("views"), "views"),
        "view",
    )
    if not views:
        raise TemplateValidationError("Template requires at least one view")
    for view in views:
        _validate_view_config(view, field_ids)

    normalized: Dict[str, Any] = {
        "fields": normalized_fields,
        "records": [],
        "views": views,
        "filters": _clone(_require_list(raw_snapshot.get("filters"), "filters")),
        "sorts": _clone(_require_list(raw_snapshot.get("sorts"), "sorts")),
        "groupConfig": _clone(
            raw_snapshot.get("groupConfig")
            or {"fieldId": None, "order": "asc"}
        ),
    }
    _validate_filter_sort_group(normalized, field_ids)

    hidden_ids = [
        str(field_id)
        for field_id in _require_list(
            raw_snapshot.get("hiddenFieldIds"),
            "hiddenFieldIds",
        )
    ]
    for hidden_id in hidden_ids:
        _validate_field_reference(hidden_id, field_ids, "Hidden field", optional=False)
    if hidden_ids:
        hidden_set = set(hidden_ids)
        for field in normalized["fields"]:
            if str(field["id"]) not in hidden_set:
                continue
            prop = dict(field.get("property") or {})
            prop["hidden"] = True
            field["property"] = prop

    if include_records:
        records = _unique_objects(
            _require_list(raw_snapshot.get("records"), "records"),
            "record",
        )
        normalized["records"] = records

    return normalized


def instantiate_template_snapshot(
    snapshot: Mapping[str, Any],
    target_table_id: str,
) -> Dict[str, Any]:
    """Resolve portable tokens and give example records fresh IDs."""
    instantiated = _clone(snapshot)
    fields = instantiated.get("fields") or []

    relation_field_ids: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or not is_relation_field(field):
            continue
        relation_field_ids.add(str(field.get("id")))
        prop = dict(field.get("property") or {})
        if prop.get("targetTableId") == SELF_RELATION_TARGET:
            prop["targetTableId"] = target_table_id
            field["property"] = prop

    records = instantiated.get("records") or []
    id_map: Dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        old_id = str(record.get("id") or "")
        if old_id:
            id_map[old_id] = f"r{uuid.uuid4().hex}"

    for record in records:
        if not isinstance(record, dict):
            continue
        old_id = str(record.get("id") or "")
        record["id"] = id_map.get(old_id, f"r{uuid.uuid4().hex}")
        for field in fields:
            if not isinstance(field, dict) or not is_relation_field(field):
                continue
            field_id = str(field.get("id") or "")
            if field_id not in relation_field_ids:
                continue
            target = get_target_table_id(field)
            if target != target_table_id:
                continue
            value = record.get(field_id)
            if relation_allows_multiple(field):
                if isinstance(value, list):
                    record[field_id] = [
                        id_map[item]
                        for item in value
                        if isinstance(item, str) and item in id_map
                    ]
            elif isinstance(value, str):
                record[field_id] = id_map.get(value)

    return instantiated


def _system_definition_payloads() -> List[Dict[str, Any]]:
    if not os.path.exists(TEMPLATE_INDEX_PATH):
        return []
    with open(TEMPLATE_INDEX_PATH, "r", encoding="utf-8") as handle:
        index = json.load(handle)
    if not isinstance(index, list):
        raise TemplateValidationError("System template index must be a list")

    definitions: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for metadata in index:
        if not isinstance(metadata, dict):
            raise TemplateValidationError("System template metadata must be an object")
        template_id = str(metadata.get("id") or "").strip()
        if not template_id or template_id in seen:
            raise TemplateValidationError("System template ids must be unique and non-empty")
        seen.add(template_id)
        filename = str(metadata.get("filename") or "").strip()
        if not filename or os.path.basename(filename) != filename:
            raise TemplateValidationError(f"Invalid system template filename: {filename}")
        path = os.path.join(TEMPLATES_DIR, filename)
        if not os.path.exists(path):
            raise TemplateValidationError(f"Missing system template file: {filename}")
        with open(path, "r", encoding="utf-8") as handle:
            raw_snapshot = json.load(handle)
        snapshot = normalize_template_snapshot(raw_snapshot, include_records=False)
        definitions.append(
            {
                "id": template_id,
                "name": str(metadata.get("name") or template_id),
                "description": metadata.get("description"),
                "category": str(metadata.get("category") or "general"),
                "tags": list(metadata.get("tags") or []),
                "icon": metadata.get("icon"),
                "featured": bool(metadata.get("featured", False)),
                "snapshot": snapshot,
            }
        )
    return definitions


async def sync_system_templates(db: AsyncSession) -> int:
    """Synchronize repository-owned system templates into the database.

    Returns the number of inserted/updated catalog rows. Re-running without
    content changes is a no-op and does not bump versions.
    """
    changed = 0
    for definition in _system_definition_payloads():
        fingerprint_payload = {
            "name": definition["name"],
            "description": definition["description"],
            "category": definition["category"],
            "tags": definition["tags"],
            "icon": definition["icon"],
            "featured": definition["featured"],
            "snapshot": definition["snapshot"],
        }
        digest = _content_hash(fingerprint_payload)
        result = await db.execute(
            select(TableTemplate).where(TableTemplate.id == definition["id"])
        )
        existing = result.scalars().first()
        if existing is None:
            db.add(
                TableTemplate(
                    id=definition["id"],
                    name=definition["name"],
                    description=definition["description"],
                    category=definition["category"],
                    tags=definition["tags"],
                    icon=definition["icon"],
                    scope=SYSTEM_SCOPE,
                    owner_user_id=None,
                    workspace_id=None,
                    status=ACTIVE_STATUS,
                    source_table_id=None,
                    include_records=False,
                    snapshot=definition["snapshot"],
                    content_hash=digest,
                    version=1,
                    usage_count=0,
                    featured=definition["featured"],
                )
            )
            changed += 1
            continue

        if existing.scope != SYSTEM_SCOPE:
            raise TemplateValidationError(
                f"System template id conflicts with custom template: {existing.id}"
            )
        if existing.content_hash == digest:
            continue
        existing.name = definition["name"]
        existing.description = definition["description"]
        existing.category = definition["category"]
        existing.tags = definition["tags"]
        existing.icon = definition["icon"]
        existing.snapshot = definition["snapshot"]
        existing.content_hash = digest
        existing.featured = definition["featured"]
        existing.status = ACTIVE_STATUS
        existing.version = int(existing.version or 0) + 1
        changed += 1

    if changed:
        await db.commit()
    return changed


async def _workspace_member_exists(
    db: AsyncSession,
    user_id: int,
    workspace_id: str,
) -> bool:
    result = await db.execute(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    return result.scalars().first() is not None


async def can_view_template(
    db: AsyncSession,
    template: TableTemplate,
    *,
    user_id: Optional[int],
    workspace_id: Optional[str] = None,
) -> bool:
    if template.status != ACTIVE_STATUS:
        return False
    if template.scope == SYSTEM_SCOPE:
        return True
    if user_id is None:
        return False
    if template.scope == PERSONAL_SCOPE:
        return template.owner_user_id == user_id
    if template.scope == WORKSPACE_SCOPE:
        target_workspace = template.workspace_id
        if not target_workspace:
            return False
        if workspace_id is not None and workspace_id != target_workspace:
            return False
        return await _workspace_member_exists(db, user_id, target_workspace)
    return False


def serialize_template(
    template: TableTemplate,
    *,
    include_snapshot: bool = False,
    can_manage: Optional[bool] = None,
) -> Dict[str, Any]:
    snapshot = template.snapshot if isinstance(template.snapshot, dict) else {}
    fields = snapshot.get("fields") if isinstance(snapshot.get("fields"), list) else []
    views = snapshot.get("views") if isinstance(snapshot.get("views"), list) else []
    records = snapshot.get("records") if isinstance(snapshot.get("records"), list) else []
    payload: Dict[str, Any] = {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "tags": template.tags or [],
        "icon": template.icon,
        "scope": template.scope,
        "ownerUserId": template.owner_user_id,
        "workspaceId": template.workspace_id,
        "status": template.status,
        "version": template.version,
        "usageCount": template.usage_count,
        "includeRecords": template.include_records,
        "featured": template.featured,
        "fieldCount": len(fields),
        "viewCount": len(views),
        "recordCount": len(records),
        "createdAt": template.created_at.isoformat() if template.created_at else None,
        "updatedAt": template.updated_at.isoformat() if template.updated_at else None,
        "canManage": bool(can_manage) if can_manage is not None else False,
    }
    if include_snapshot:
        payload["snapshot"] = _clone(snapshot)
    return payload


async def list_visible_templates(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    workspace_id: Optional[str] = None,
    scope: Optional[str] = None,
    search: Optional[str] = None,
    category: Optional[str] = None,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    await sync_system_templates(db)

    if scope is not None and scope not in TEMPLATE_SCOPES:
        raise TemplateValidationError("Invalid template scope")

    conditions = [
        or_(
            TableTemplate.scope == SYSTEM_SCOPE,
            (
                (TableTemplate.scope == PERSONAL_SCOPE)
                & (TableTemplate.owner_user_id == user_id)
            )
            if user_id is not None
            else TableTemplate.scope == "__never_personal__",
            (
                (TableTemplate.scope == WORKSPACE_SCOPE)
                & (TableTemplate.workspace_id == workspace_id)
            )
            if workspace_id
            else TableTemplate.scope == "__never_workspace__",
        ),
    ]
    if not include_archived:
        conditions.append(TableTemplate.status == ACTIVE_STATUS)
    if scope:
        conditions.append(TableTemplate.scope == scope)
    if category:
        conditions.append(TableTemplate.category == category)

    result = await db.execute(
        select(TableTemplate)
        .where(*conditions)
        .order_by(
            TableTemplate.featured.desc(),
            TableTemplate.scope.asc(),
            TableTemplate.name.asc(),
        )
    )
    templates = result.scalars().all()

    if workspace_id and user_id is not None:
        workspace_visible = await _workspace_member_exists(db, user_id, workspace_id)
        if not workspace_visible:
            templates = [
                template
                for template in templates
                if template.scope != WORKSPACE_SCOPE
            ]

    query = (search or "").strip().casefold()
    if query:
        templates = [
            template
            for template in templates
            if query
            in " ".join(
                [
                    template.name or "",
                    template.description or "",
                    template.category or "",
                    " ".join(str(tag) for tag in (template.tags or [])),
                ]
            ).casefold()
        ]

    payloads: List[Dict[str, Any]] = []
    for template in templates:
        can_manage = False
        if user_id is not None and template.scope != SYSTEM_SCOPE:
            can_manage = await can_manage_custom_template(
                db,
                template,
                user_id=user_id,
            )
        # Archived templates are a management concern. Never expose them to a
        # user who can only consume active workspace templates.
        if template.status == ARCHIVED_STATUS and not can_manage:
            continue
        payloads.append(
            serialize_template(
                template,
                can_manage=can_manage,
            )
        )
    return payloads


async def get_visible_template(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: Optional[int],
    workspace_id: Optional[str] = None,
    include_archived: bool = False,
) -> TableTemplate:
    await sync_system_templates(db)
    result = await db.execute(
        select(TableTemplate).where(TableTemplate.id == template_id)
    )
    template = result.scalars().first()
    if template is None:
        raise TemplateValidationError("Template not found or no access")

    if template.status == ACTIVE_STATUS and await can_view_template(
        db,
        template,
        user_id=user_id,
        workspace_id=workspace_id,
    ):
        return template

    if (
        include_archived
        and template.status == ARCHIVED_STATUS
        and user_id is not None
        and await can_manage_custom_template(db, template, user_id=user_id)
    ):
        return template

    # Keep existence and authorization indistinguishable.
    raise TemplateValidationError("Template not found or no access")


async def resolve_template_for_use(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: Optional[int],
    workspace_id: Optional[str],
    target_table_id: str,
) -> Tuple[TableTemplate, Dict[str, Any]]:
    template = await get_visible_template(
        db,
        template_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    snapshot = instantiate_template_snapshot(template.snapshot, target_table_id)
    # Increment in the current transaction. The table initializer commits both
    # the table schema and this usage counter together; rollback restores both.
    template.usage_count = int(template.usage_count or 0) + 1
    return template, snapshot


def _clean_template_metadata(
    *,
    name: str,
    description: Optional[str],
    category: Optional[str],
    tags: Optional[Sequence[str]],
    icon: Optional[str],
) -> Dict[str, Any]:
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise TemplateValidationError("Template name is required")
    if len(normalized_name) > 255:
        raise TemplateValidationError("Template name is too long")

    normalized_category = (category or "general").strip() or "general"
    if len(normalized_category) > 120:
        raise TemplateValidationError("Template category is too long")

    normalized_tags: List[str] = []
    seen: set[str] = set()
    for raw_tag in tags or []:
        tag = str(raw_tag).strip()
        if not tag or tag in seen:
            continue
        if len(tag) > 64:
            raise TemplateValidationError("Template tag is too long")
        seen.add(tag)
        normalized_tags.append(tag)
        if len(normalized_tags) > MAX_TEMPLATE_TAGS:
            raise TemplateValidationError(
                f"Template cannot have more than {MAX_TEMPLATE_TAGS} tags"
            )

    normalized_icon = (icon or "").strip() or None
    if normalized_icon and len(normalized_icon) > 120:
        raise TemplateValidationError("Template icon is too long")

    normalized_description = description.strip() if isinstance(description, str) else None
    return {
        "name": normalized_name,
        "description": normalized_description or None,
        "category": normalized_category,
        "tags": normalized_tags,
        "icon": normalized_icon,
    }


def _custom_template_hash(
    metadata: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    include_records: bool,
) -> str:
    return _content_hash(
        {
            "name": metadata.get("name"),
            "description": metadata.get("description"),
            "category": metadata.get("category"),
            "tags": metadata.get("tags") or [],
            "icon": metadata.get("icon"),
            "includeRecords": include_records,
            "snapshot": snapshot,
        }
    )


async def snapshot_table_for_template(
    db: AsyncSession,
    source_table_id: str,
    *,
    include_records: bool,
) -> Dict[str, Any]:
    """Capture one table into a portable reusable snapshot."""
    from app.services.smart_table_store import get_full_store

    item_result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == source_table_id,
            WorkspaceItem.type == "table",
        )
    )
    if item_result.scalars().first() is None:
        raise TemplateValidationError("Source table not found")

    store = await get_full_store(db, source_table_id)
    records = store.get("records") or []
    if include_records and len(records) > MAX_TEMPLATE_RECORDS:
        raise TemplateValidationError(
            f"Templates can include at most {MAX_TEMPLATE_RECORDS} example records"
        )
    return normalize_template_snapshot(
        store,
        source_table_id=source_table_id,
        include_records=include_records,
    )


async def _require_workspace_template_editor(
    db: AsyncSession,
    user_id: int,
    workspace_id: str,
) -> WorkspaceMember:
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    member = result.scalars().first()
    if member is None or not permission_allows(role_permission(member.role), "edit"):
        raise TemplateValidationError("No permission to manage workspace templates")
    return member


async def _source_table_item(
    db: AsyncSession,
    source_table_id: str,
) -> WorkspaceItem:
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == source_table_id,
            WorkspaceItem.type == "table",
        )
    )
    item = result.scalars().first()
    if item is None:
        raise TemplateValidationError("Source table not found")
    return item


async def create_custom_template(
    db: AsyncSession,
    *,
    source_table_id: str,
    name: str,
    scope: str,
    user_id: int,
    workspace_id: Optional[str] = None,
    description: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    icon: Optional[str] = None,
    include_records: bool = False,
) -> TableTemplate:
    """Save the current table as a personal or workspace template."""
    if scope not in {PERSONAL_SCOPE, WORKSPACE_SCOPE}:
        raise TemplateValidationError(
            "Custom template scope must be personal or workspace"
        )

    source_item = await _source_table_item(db, source_table_id)
    resolved_workspace_id: Optional[str] = None
    if scope == WORKSPACE_SCOPE:
        resolved_workspace_id = workspace_id or source_item.workspace_id
        if resolved_workspace_id != source_item.workspace_id:
            raise TemplateValidationError(
                "Workspace template must be created from a table in that workspace"
            )
        await _require_workspace_template_editor(
            db,
            user_id,
            resolved_workspace_id,
        )

    metadata = _clean_template_metadata(
        name=name,
        description=description,
        category=category,
        tags=tags,
        icon=icon,
    )
    snapshot = await snapshot_table_for_template(
        db,
        source_table_id,
        include_records=include_records,
    )
    digest = _custom_template_hash(
        metadata,
        snapshot,
        include_records=include_records,
    )
    template = TableTemplate(
        id=f"tpl_{uuid.uuid4().hex}",
        name=metadata["name"],
        description=metadata["description"],
        category=metadata["category"],
        tags=metadata["tags"],
        icon=metadata["icon"],
        scope=scope,
        owner_user_id=user_id,
        workspace_id=resolved_workspace_id,
        status=ACTIVE_STATUS,
        source_table_id=source_table_id,
        include_records=bool(include_records),
        snapshot=snapshot,
        content_hash=digest,
        version=1,
        usage_count=0,
        featured=False,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def can_manage_custom_template(
    db: AsyncSession,
    template: TableTemplate,
    *,
    user_id: int,
) -> bool:
    if template.scope == SYSTEM_SCOPE:
        return False
    if template.scope == PERSONAL_SCOPE:
        return template.owner_user_id == user_id
    if template.scope != WORKSPACE_SCOPE or not template.workspace_id:
        return False

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == template.workspace_id,
        )
    )
    member = result.scalars().first()
    if member is None:
        return False
    permission = role_permission(member.role)
    if permission_allows(permission, "manage"):
        return True
    return (
        template.owner_user_id == user_id
        and permission_allows(permission, "edit")
    )


async def get_manageable_custom_template(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: int,
) -> TableTemplate:
    result = await db.execute(
        select(TableTemplate).where(TableTemplate.id == template_id)
    )
    template = result.scalars().first()
    if template is None or not await can_manage_custom_template(
        db,
        template,
        user_id=user_id,
    ):
        raise TemplateValidationError("Template not found or no permission to manage")
    return template


async def update_custom_template(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    icon: Optional[str] = None,
    source_table_id: Optional[str] = None,
    include_records: Optional[bool] = None,
) -> TableTemplate:
    """Update custom template metadata and optionally refresh its content."""
    template = await get_manageable_custom_template(
        db,
        template_id,
        user_id=user_id,
    )
    metadata = _clean_template_metadata(
        name=name if name is not None else template.name,
        description=(
            description if description is not None else template.description
        ),
        category=category if category is not None else template.category,
        tags=tags if tags is not None else (template.tags or []),
        icon=icon if icon is not None else template.icon,
    )

    next_include_records = (
        bool(include_records)
        if include_records is not None
        else bool(template.include_records)
    )
    next_source_table_id = source_table_id or template.source_table_id
    next_snapshot = template.snapshot

    if source_table_id is not None or include_records is not None:
        if not next_source_table_id:
            raise TemplateValidationError("Source table is required to refresh template")
        source_item = await _source_table_item(db, next_source_table_id)
        if (
            template.scope == WORKSPACE_SCOPE
            and template.workspace_id != source_item.workspace_id
        ):
            raise TemplateValidationError(
                "Workspace template must use a source table from the same workspace"
            )
        next_snapshot = await snapshot_table_for_template(
            db,
            next_source_table_id,
            include_records=next_include_records,
        )

    digest = _custom_template_hash(
        metadata,
        next_snapshot,
        include_records=next_include_records,
    )
    if digest != template.content_hash:
        template.version = int(template.version or 0) + 1
        template.content_hash = digest

    template.name = metadata["name"]
    template.description = metadata["description"]
    template.category = metadata["category"]
    template.tags = metadata["tags"]
    template.icon = metadata["icon"]
    template.source_table_id = next_source_table_id
    template.include_records = next_include_records
    template.snapshot = next_snapshot
    await db.commit()
    await db.refresh(template)
    return template


async def set_custom_template_archived(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: int,
    archived: bool,
) -> TableTemplate:
    template = await get_manageable_custom_template(
        db,
        template_id,
        user_id=user_id,
    )
    next_status = ARCHIVED_STATUS if archived else ACTIVE_STATUS
    if template.status != next_status:
        template.status = next_status
        template.version = int(template.version or 0) + 1
        await db.commit()
        await db.refresh(template)
    return template


async def delete_custom_template(
    db: AsyncSession,
    template_id: str,
    *,
    user_id: int,
) -> bool:
    template = await get_manageable_custom_template(
        db,
        template_id,
        user_id=user_id,
    )
    await db.delete(template)
    await db.commit()
    return True
