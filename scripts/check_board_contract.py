from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(text: str, needle: str, message: str) -> None:
    if needle not in text:
        raise SystemExit(f"[board-contract] {message}")


service = read("app/services/board.py")
queries = read("app/api/graphql/queries/board.py")
mutations = read("app/api/graphql/mutations/board.py")
subscriptions = read("app/api/graphql/subscriptions.py")
model = read("app/models/board.py")
migration = read("alembic/versions/0005_board_card_order.py")

for needle, message in [
    ("expected_record_version", "record concurrency guard missing"),
    ("expected_order_revision", "order concurrency guard missing"),
    ("MAX_COMPAT_CANDIDATES", "bounded compatibility path missing"),
    ("row_permission_restricts_user", "row permission integration missing"),
    ("databasePaged", "board paging observability missing"),
    ("Kanban member swimlanes require a single-select member field", "deterministic member lane validation missing"),
]:
    require(service, needle, message)

for needle, message in [
    ("class BoardCardOrder", "per-view order model missing"),
    ("Numeric(50, 20)", "fractional rank precision missing"),
    ('ondelete="CASCADE"', "database cleanup cascade missing"),
]:
    require(model, needle, message)

compat_start = service.find("async def _compat_records(")
compat_end = service.find("\n\nasync def query_board(", compat_start)
if compat_start < 0 or compat_end < 0:
    raise SystemExit("[board-contract] compatibility query helper missing")
compat_service = service[compat_start:compat_end]
require(compat_service, "view_id: str", "compatibility order query is not view-scoped")
require(
    compat_service,
    "BoardCardOrder.view_id == view_id",
    "compatibility order query can leak order from another view",
)

for needle, message in [
    ('name="boardView"', "boardView GraphQL query missing"),
    ("_row_permission_context", "board query permission context missing"),
]:
    require(queries, needle, message)

for needle, message in [
    ('name="moveBoardCard"', "atomic move mutation missing"),
    ('name="updateBoardViewConfig"', "validated board config mutation missing"),
    ("_require_record_permission", "move row permission check missing"),
    ("publish_board_update", "targeted realtime publish missing"),
    ("publish_table_update", "legacy table invalidation missing"),
]:
    require(mutations, needle, message)

require(subscriptions, 'name="boardUpdates"', "boardUpdates subscription missing")
require(subscriptions, "_require_record_permission", "realtime row permission re-check missing")
require(migration, 'revision: str = "0005_board_card_order"', "board migration missing")

print("[board-contract] OK")
