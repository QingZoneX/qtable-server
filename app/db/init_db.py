import json
import os
import logging
import re
import asyncpg
from sqlalchemy import select, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.base import Base
from app.core.config import settings
from app.db.session import engine, AsyncSessionLocal
try:
    from sqlmodel import SQLModel
except ModuleNotFoundError:
    SQLModel = None
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableView,
    TableFilter,
    TableSort,
    TableGroup,
    WorkspaceItem,
)

# Import models to ensure they are registered
from app.models import smart_table
from app.models import user as user_models
from app.models import workspace_member as workspace_member_models
from app.models import password_reset as password_reset_models
from app.models import oauth as oauth_models
from app.models import context_session as context_session_models
from app.models import context_snapshot as context_snapshot_models
from app.models import skill_registry as skill_registry_models
from app.models import estimate_workload as estimate_workload_models
from app.models import task_split as task_split_models
from app.models import tool_chain as tool_chain_models
from app.models import agent_workflow as agent_workflow_models
from app.models import dashboard as dashboard_models
from app.models import clipper_integration as clipper_integration_models
from app.models import table_template as table_template_models
from app.models import task_profile as task_profile_models
from app.models import my_work as my_work_models
from app.models import change_history as change_history_models
from app.models import workspace_generation as workspace_generation_models
from app.models import workload_planning as workload_planning_models
from app.models import project_steward as project_steward_models
from app.models import member_assignment as member_assignment_models
from app.models import ai_action_plan as ai_action_plan_models
from app.models import ai_visual_design as ai_visual_design_models
from app.services.skill_registry import skill_registry_service

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FILE_PATH = os.path.join(DATA_DIR, "smart_table.json")
LEGACY_FILE_PATH = os.path.join(BASE_DIR, "app", "data", "smart_table.json")
TABLES_DIR = os.path.join(DATA_DIR, "tables")
VIEW_NAME_RENAMES = {
    "all tasks": "Grid",
    "timeline": "Gantt",
    "time line": "Gantt",
    "board": "Kanban",
}

def _normalize_view_name(name: str) -> str:
    normalized = " ".join((name or "").strip().lower().split())
    return VIEW_NAME_RENAMES.get(normalized, name)

def _migrate_file_view_names(file_path: str) -> bool:
    if not os.path.exists(file_path):
        return False
    with open(file_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    views = payload.get("views")
    if not isinstance(views, list):
        return False
    changed = False
    for view in views:
        if not isinstance(view, dict):
            continue
        name = view.get("name")
        if not isinstance(name, str):
            continue
        next_name = _normalize_view_name(name)
        if next_name != name:
            view["name"] = next_name
            changed = True
    if not changed:
        return False
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return True

def _migrate_all_file_view_names() -> None:
    changed_count = 0
    for path in [FILE_PATH, LEGACY_FILE_PATH]:
        if _migrate_file_view_names(path):
            changed_count += 1
    if os.path.isdir(TABLES_DIR):
        for file_name in os.listdir(TABLES_DIR):
            if not file_name.endswith(".json"):
                continue
            table_file = os.path.join(TABLES_DIR, file_name)
            if _migrate_file_view_names(table_file):
                changed_count += 1
    if changed_count:
        logger.info("Migrated legacy view names in %s file(s).", changed_count)

async def _migrate_db_view_names(session: AsyncSession) -> None:
    result = await session.execute(select(TableView))
    views = result.scalars().all()
    changed = False
    for view in views:
        if not isinstance(view.name, str):
            continue
        next_name = _normalize_view_name(view.name)
        if next_name != view.name:
            view.name = next_name
            changed = True
    if changed:
        await session.commit()
        logger.info("Migrated legacy view names in database table_views.")

async def ensure_database_exists():
    if not settings.CREATE_DB_IF_MISSING:
        return
    db_uri = settings.SQLALCHEMY_DATABASE_URI or ""
    if not db_uri.startswith("postgresql"):
        return
    db_name = settings.POSTGRES_DB
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise ValueError("Invalid POSTGRES_DB")
    try:
        conn = await asyncpg.connect(
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            database=db_name,
            host=settings.POSTGRES_SERVER,
            port=settings.POSTGRES_PORT,
        )
        await conn.close()
        return
    except asyncpg.exceptions.InvalidCatalogNameError:
        pass
    conn = await asyncpg.connect(
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        database="postgres",
        host=settings.POSTGRES_SERVER,
        port=settings.POSTGRES_PORT,
    )
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

async def init_models():
    await ensure_database_exists()
    async with engine.begin() as conn:
        # await conn.run_sync(Base.metadata.drop_all) # For dev only, maybe comment out
        await conn.run_sync(Base.metadata.create_all)
        if SQLModel is not None:
            await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_sync_ensure_schema)

def _sync_ensure_schema(conn) -> None:
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    preparer = conn.dialect.identifier_preparer
    _ = user_models.User
    _ = workspace_member_models.WorkspaceMember
    _ = task_profile_models.TableTaskProfile
    _ = my_work_models.MyWorkRecentTarget
    _ = change_history_models.ChangeSet
    _ = change_history_models.ChangeItem
    _ = change_history_models.RecycleBinRecord
    _ = workspace_generation_models.WorkspaceGenerationTrace
    _ = workload_planning_models.WorkloadPlanningBatch
    _ = project_steward_models.ProjectStewardDiagnosis
    _ = member_assignment_models.MemberAssignmentBatch
    _ = ai_action_plan_models.AiActionPlanBatch
    _ = ai_visual_design_models.AiVisualDesignPlan
    metadata_tables = {table.name: table for table in Base.metadata.sorted_tables}
    if SQLModel is not None:
        for table in SQLModel.metadata.sorted_tables:
            metadata_tables.setdefault(table.name, table)
    for table in metadata_tables.values():
        if table.name not in existing_tables:
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            default_clause = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                default_value = column.default.arg
                if isinstance(default_value, str):
                    default_sql = f"'{default_value}'"
                elif default_value is None:
                    default_sql = "NULL"
                else:
                    default_sql = str(default_value)
                default_clause = f" DEFAULT {default_sql}"
            nullable = "NULL"
            if not column.nullable and default_clause:
                nullable = "NOT NULL"
            table_name = preparer.format_table(table)
            column_name = preparer.quote(column.name)
            ddl = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {col_type}{default_clause} {nullable}"
            logger.info(f"Auto-repair: Adding column {column.name} to table {table.name}")
            conn.execute(text(ddl))
    _sync_fix_table_groups_pk(conn, inspector)
    if "table_fields" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_fields_table_id_order ON table_fields (table_id, order_index)"))
    if "table_records" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_records_table_id_order ON table_records (table_id, order_index)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_records_table_id_order_id ON table_records (table_id, order_index, id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_records_table_id_creator ON table_records (table_id, created_by_user_id)"))
    if "table_views" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_views_table_id ON table_views (table_id)"))
    if "table_filters" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_filters_table_id_order ON table_filters (table_id, order_index)"))
    if "table_sorts" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_sorts_table_id_order ON table_sorts (table_id, order_index)"))
    if "table_groups" in existing_tables:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_table_groups_table_id ON table_groups (table_id)"))

def _sync_fix_table_groups_pk(conn, inspector) -> None:
    if conn.dialect.name != "sqlite":
        return
    if "table_groups" not in inspector.get_table_names():
        return
    pk = inspector.get_pk_constraint("table_groups") or {}
    pk_columns = pk.get("constrained_columns") or []
    if pk_columns == ["id"]:
        return
    conn.execute(
        text(
            """
            CREATE TABLE table_groups_new (
                id VARCHAR NOT NULL,
                table_id VARCHAR NOT NULL,
                field_id VARCHAR,
                "order" VARCHAR NOT NULL,
                PRIMARY KEY (id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO table_groups_new (id, table_id, field_id, "order")
            SELECT g.id, g.table_id, g.field_id, g."order"
            FROM table_groups g
            INNER JOIN (
                SELECT id, MAX(rowid) AS rowid
                FROM table_groups
                GROUP BY id
            ) dedup ON g.rowid = dedup.rowid
            """
        )
    )
    conn.execute(text("DROP TABLE table_groups"))
    conn.execute(text("ALTER TABLE table_groups_new RENAME TO table_groups"))

async def _ensure_workspace_items(session: AsyncSession) -> None:
    result = await session.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id == "wkbDefault").limit(1)
    )
    if result.scalars().first():
        return
    root = WorkspaceItem(
        id="fldRoot",
        workspace_id="wkbDefault",
        type="folder",
        name="Root",
        parent_id=None,
        order_index=0,
        default_view_id=None,
    )
    table = WorkspaceItem(
        id="dstDefault",
        workspace_id="wkbDefault",
        type="table",
        name="Projects",
        parent_id="fldRoot",
        order_index=0,
        default_view_id="v1",
    )
    session.add(root)
    session.add(table)
    await session.commit()

async def seed_data():
    _migrate_all_file_view_names()
    if not os.path.exists(FILE_PATH):
        logger.warning("No seed data file found.")
        return

    async with AsyncSessionLocal() as session:
        await _ensure_workspace_items(session)
        await _migrate_db_view_names(session)
        await skill_registry_service.sync_builtin_registry_entries(session)
        from app.services.table_templates import sync_system_templates
        await sync_system_templates(session)
        # Check if data exists
        result = await session.execute(select(TableField).where(TableField.table_id == "dstDefault"))
        first_field = result.scalars().first()
        
        if first_field:
            logger.info("Data already exists. Syncing field properties from seed.")
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            seed_fields = {
                field_data["id"]: field_data for field_data in data.get("fields", [])
            }
            result_fields = await session.execute(
                select(TableField).where(TableField.table_id == "dstDefault")
            )
            fields = result_fields.scalars().all()
            updated = False
            for field in fields:
                seed = seed_fields.get(field.id)
                if seed and "property" in seed:
                    if field.property != seed.get("property"):
                        field.property = seed.get("property")
                        updated = True
            if updated:
                await session.commit()
            return

        logger.info("Seeding data from smart_table.json...")
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Seed Fields
        for idx, field_data in enumerate(data.get("fields", [])):
            field = TableField(
                id=field_data["id"],
                table_id="dstDefault",
                name=field_data["name"],
                type=field_data["type"],
                options=field_data.get("options"),
                property=field_data.get("property"),
                order_index=idx
            )
            session.add(field)

        # Seed Records
        for idx, record_data in enumerate(data.get("records", [])):
            r_id = record_data.get("id")
            # Separate id from data if needed, or keep it in data too. 
            # Keeping it simple: store fields in data.
            content = {k: v for k, v in record_data.items() if k != "id"}
            record = TableRecord(
                id=r_id,
                table_id="dstDefault",
                data=content,
                order_index=idx
            )
            session.add(record)

        # Seed Views
        for idx, view_data in enumerate(data.get("views", [])):
            view = TableView(
                id=view_data["id"],
                table_id="dstDefault",
                name=view_data["name"],
                type=view_data["type"],
                config=view_data.get("config"), # Assuming config matches extra fields
            )
            session.add(view)

        for idx, filter_data in enumerate(data.get("filters", [])):
            new_filter = TableFilter(
                id=filter_data["id"],
                table_id="dstDefault",
                field_id=filter_data["fieldId"],
                operator=filter_data["operator"],
                value=filter_data.get("value"),
                logic=filter_data.get("logic", "and"),
                order_index=idx,
            )
            session.add(new_filter)

        for idx, sort_data in enumerate(data.get("sorts", [])):
            new_sort = TableSort(
                id=sort_data.get("id") or f"s{idx}",
                table_id="dstDefault",
                field_id=sort_data["fieldId"],
                order=sort_data["order"],
                order_index=idx,
            )
            session.add(new_sort)

        group_config = data.get("groupConfig")
        if group_config:
            group = TableGroup(
                id="grp_dstDefault",
                table_id="dstDefault",
                field_id=group_config.get("fieldId"),
                order=group_config.get("order", "asc"),
            )
            session.add(group)

        await session.commit()
        logger.info("Seeding completed.")
