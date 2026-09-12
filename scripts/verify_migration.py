from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def expected_revision() -> str:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    head = script.get_current_head()
    if not head:
        raise RuntimeError("Alembic migration history has no single head")
    return head


EXPECTED_TABLES = {
    "users", "workspaces", "workspace_members", "workspace_items",
    "table_fields", "table_records", "table_views", "dashboards",
    "dashboard_widgets", "change_sets", "ai_visual_design_plans",
    "source_inbox_items", "board_card_orders", "record_comments",
    "record_comment_mentions", "user_notifications",
}

EXPECTED_COLUMNS = {
    "workspaces": {"experience_mode"},
    "workspace_members": {"experience_mode"},
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/verify_migration.py <sqlite-db>")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"[migration-check] database not found: {path}")
        return 1
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = EXPECTED_TABLES - tables
        if missing:
            print("[migration-check] missing tables:", sorted(missing))
            return 1

        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }
            missing_columns = expected_columns - columns
            if missing_columns:
                print(
                    f"[migration-check] missing columns on {table_name}:",
                    sorted(missing_columns),
                )
                return 1

        expected = expected_revision()
        row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        if not row or row[0] != expected:
            print(
                "[migration-check] unexpected revision:",
                row,
                "expected:",
                expected,
            )
            return 1
    finally:
        connection.close()
    print(f"[migration-check] OK: {len(tables)} tables, revision {expected_revision()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
