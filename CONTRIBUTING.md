# Contributing to QTable

Thank you for helping improve QTable.

## Before you start

1. Search existing Issues and Pull Requests.
2. For a non-trivial feature, open or comment on an Issue first so product and data-model direction is clear.
3. Security vulnerabilities must follow [SECURITY.md](SECURITY.md), not a public Issue.

## Local setup

PostgreSQL is the default development database. Start PostgreSQL and Redis first, then run the backend:

```bash
cp .env.example .env
docker compose up -d db redis

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

alembic upgrade head
```

Run the application against PostgreSQL during normal development. The automated test suite uses an explicit isolated SQLite database so tests do not mutate a developer's PostgreSQL data:

```bash
rm -f /tmp/qtable-pytest.db
DATABASE_MODE=sqlite SQLITE_PATH=/tmp/qtable-pytest.db pytest -q
```

If PostgreSQL is intentionally unavailable for application development, use `.env.sqlite.example` as an explicit lightweight fallback. Do not add automatic PostgreSQL-to-SQLite failover.

The frontend lives in `QingZoneX/QTableUI`.

## Engineering invariants

Changes must preserve these rules:

- Do not build a second AI-only table/task/view/dashboard model.
- Do not expose rows or fields the current user cannot read.
- Preview is read-only.
- AI or connector writes require the product's confirmation boundary where applicable.
- Apply must re-check permissions and stale/concurrent state.
- Member fields store workspace user identities; candidate lists come from current WorkspaceMember data.
- Do not bypass ChangeSet/audit semantics for user-visible business writes when an existing safe path exists.
- Do not replace server-side paging/aggregation with full-table browser scans.
- Do not weaken OAuth, public-dashboard or row-permission checks to make tests pass.

## Pull requests

Prefer one coherent commit per independently reviewable feature. If CI finds a defect before merge, amend/rewrite the feature commit instead of accumulating repair commits when practical.

A PR is ready when:

- `pytest -q` passes;
- open-source readiness and secret checks pass;
- migration smoke test passes if schema-related;
- new behavior has focused tests;
- documentation is updated;
- the branch is not behind `main`;
- unresolved review threads are zero.

## Database changes

New schema changes must include an Alembic revision. Do not rely only on `Base.metadata.create_all()` or runtime schema repair for new public releases.

## Testing

Add regression tests for permission boundaries, idempotency, stale-state protection and no-write Preview behavior when those concepts apply.

## Style

Follow the surrounding Python style and keep service boundaries explicit. Prefer small validation helpers over duplicating permission logic.

## License of contributions

By submitting a contribution, you agree that it is licensed under the repository's Apache License 2.0.
