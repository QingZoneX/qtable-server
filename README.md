# QTable

> AI-native, open-source project and work management built on multidimensional tables.

**Status:** `v0.1.0-alpha` release candidate — private release preparation, **not yet publicly released**.

QTable combines flexible tables, multiple project views, dashboards, permissions, and an AI workflow that can turn a goal into an executable project plan. AI changes are designed around **Preview → Confirm → Apply**, and reuse the same Table / View / Dashboard / Permission models as manual work.

Frontend: [QingZoneX/QTableUI](https://github.com/QingZoneX/QTableUI)

## Why QTable

Traditional multidimensional tables usually start with configuration. QTable aims for a shorter path:

```text
Describe a goal
      ↓
Generate workspace / tables
      ↓
Plan tasks
      ↓
Estimate workload and schedule
      ↓
Suggest assignees
      ↓
Diagnose project risks
      ↓
Preview and apply actions
```

The table remains fully editable at every step.

## Highlights

- Grid / Kanban / Gantt / Calendar / Gallery views.
- Filters, multi-field sorting, grouping and named views.
- Formula, relation, auto-number, member, select, date, attachment and other field types.
- Workspace membership, item permissions and row-level permissions.
- Durable private attachments backed by S3-compatible object storage; table rows persist stable object identities rather than expiring URLs.
- Dashboards with server-side aggregation and permission-safe public sharing.
- ChangeSet-based audit history and safe undo foundations.
- AI goal-driven workspace generation.
- AI task planning, workload estimation and member assignment.
- AI Project Steward diagnostics and question answering.
- AI action plans with diff preview, partial acceptance and permission revalidation.
- AI-generated Views and Dashboards using existing product models.
- QNote / Clipper Source Inbox with duplicate hints and explicit task conversion.
- OAuth 2.0 Authorization Code Flow with S256 PKCE.
- PostgreSQL + Redis as the default development and deployment stack; SQLite is available as an explicit lightweight fallback.

## Architecture

```text
QTableUI (React + TypeScript + VTable + Apollo)
               │
        HTTP / GraphQL / WS
               │
QTable API (FastAPI + Strawberry GraphQL)
       │                 │
 PostgreSQL/SQLite      Redis
       │
 Permission / ChangeSet / AI services
       │
 S3-compatible attachment storage (MinIO in canonical Compose)
```

AI providers are configured by the user. Core table functionality does not require an external AI service.

## Quick start: PostgreSQL (recommended)

Requirements:

- Python 3.12 recommended.
- Node.js 22 for QTableUI.
- Docker / Docker Compose for the recommended local PostgreSQL + Redis + MinIO dependencies.

Backend:

```bash
git clone https://github.com/QingZoneX/QTable.git
cd QTable

cp .env.example .env

# Start the recommended local infrastructure, including attachment storage.
docker compose up -d db redis minio

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

alembic upgrade head
uvicorn app.main:app --reload --port 9000
```

Frontend, in another terminal:

```bash
git clone https://github.com/QingZoneX/QTableUI.git
cd QTableUI
npm ci
npm run dev
```

Open <http://localhost:9100>. GraphQL is available at <http://localhost:9000/graphql>.

PostgreSQL is QTable's default database for normal development and deployment. If you already have PostgreSQL, edit the `POSTGRES_*` values in `.env` instead of starting the Compose database.

Attachment endpoints have two network contexts that intentionally map to the same backend runtime setting. When the QTable process runs directly on the host, `.env.example` uses `ATTACHMENT_S3_ENDPOINT=localhost:9001` to reach the MinIO port published by Compose. When QTable itself runs inside canonical Compose, `ATTACHMENT_S3_COMPOSE_ENDPOINT` is mapped into the container's runtime `ATTACHMENT_S3_ENDPOINT`; its default is the service-network address `minio:9000`. For an external S3-compatible provider under Compose, set `ATTACHMENT_S3_COMPOSE_ENDPOINT` and `ATTACHMENT_S3_COMPOSE_SECURE` together with the standard access key, secret, bucket and optional region values instead of editing `docker-compose.yml`.

## Attachment storage contract

Attachments are private application data, not public object URLs. A table cell stores only stable metadata:

```text
attachmentId + objectKey + name + size + contentType
```

QTable never persists presigned URLs in a record. Upload, download and delete are performed through authenticated QTable APIs and re-check the current table and row permission each time. Losing access to a row therefore also removes access to its attachments.

The canonical `.env.example`, backend settings and Docker Compose stack use one S3-compatible storage contract. Backend runtime settings are:

- `ATTACHMENT_STORAGE_ENABLED`
- `ATTACHMENT_S3_ENDPOINT`
- `ATTACHMENT_S3_ACCESS_KEY`
- `ATTACHMENT_S3_SECRET_KEY`
- `ATTACHMENT_S3_BUCKET`
- optional `ATTACHMENT_S3_REGION`
- `ATTACHMENT_S3_SECURE`
- `ATTACHMENT_MAX_BYTES`
- cleanup batch/interval settings
- `ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS` for abandoned upload-intent recovery

Compose additionally exposes two network-context mapping values:

- `ATTACHMENT_S3_COMPOSE_ENDPOINT` — defaults to `minio:9000` and is passed to the QTable container as runtime `ATTACHMENT_S3_ENDPOINT`
- `ATTACHMENT_S3_COMPOSE_SECURE` — defaults to `false` and is passed as runtime `ATTACHMENT_S3_SECURE`

For local development, the example `.env` credentials are consumed by both MinIO and QTable. Replace them before any production deployment. For TLS-enabled external storage set the appropriate endpoint/region and secure flag for the way QTable is being run: `ATTACHMENT_S3_SECURE=true` for a host-run backend or `ATTACHMENT_S3_COMPOSE_SECURE=true` for the canonical Compose backend.

Record deletion follows the application recycle lifecycle: soft-deleted rows keep their attachment objects so restore remains real; permanent purge makes those objects eligible for durable background cleanup. Explicitly removing an attachment from a live cell also removes its object, with failed storage cleanup retained as a retryable database state rather than reported as a false success.

Uploads use a durable intent before any object-store write. The intent is activated atomically with the record's stable attachment reference only after the object upload succeeds. If the API process is killed before activation, the cleanup worker can still see the `upload_pending` registry row and removes abandoned objects once the configured grace window has elapsed. This avoids a blind S3-only orphan window while keeping slow in-flight uploads safe from premature cleanup.

## SQLite fallback

SQLite is available for lightweight evaluation, offline development, or constrained single-instance use. It is not the recommended deployment database.

```bash
cp .env.sqlite.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 9000
```

QTable does not automatically fall back from PostgreSQL to SQLite. Switching database engines is always explicit through `DATABASE_MODE`.

## One-command self-hosted stack

Clone the two repositories as siblings:

```text
qingzone/
├── QTable/
└── QTableUI/
```

Then:

```bash
cd QTable
cp .env.example .env
docker compose up --build -d
```

Open <http://localhost:9100>.

The Compose stack contains:

- QTable API
- QTableUI
- PostgreSQL 16
- Redis 7
- MinIO S3-compatible private attachment storage

## Docker Hub distribution

After the corresponding verified release tags are published, the official application images are:

```text
qingzonex/qtable:0.1.0-alpha
qingzonex/qtable-ui:0.1.0-alpha
```

For consumers who do not want to build the two application repositories locally, use the registry Compose file:

```bash
cp .env.example .env

# Required before a production-mode registry stack can start:
# - set a strong POSTGRES_PASSWORD
# - replace SECRET_KEY with a long random value
# - set ENCRYPTION_KEY to a stable Fernet key
# - replace ATTACHMENT_S3_ACCESS_KEY / ATTACHMENT_S3_SECRET_KEY
# - review QTABLE_WEB_URL and RESET_PASSWORD_URL_BASE for your public URL

docker compose -f docker-compose.registry.yml pull db redis qtable qtable-ui
docker compose -f docker-compose.registry.yml up -d
```

`docker-compose.registry.yml` pulls QTable and QTableUI from Docker Hub rather than requiring a sibling QTableUI checkout. It deliberately forces `APP_ENV=production`, disables legacy JWT acceptance by default, keeps password-reset debug output disabled, and requires the database/application encryption secrets explicitly. PostgreSQL, Redis, the QTable API, and MinIO ports remain loopback-only by default; only QTableUI is intended to be exposed through your reverse proxy or gateway.

The pinned MinIO helper is still built from this QTable repository so the attachment-storage implementation stays aligned with the canonical stack. You can instead point QTable at an external S3-compatible object store by changing the attachment endpoint/secure settings and governing that storage outside this Compose file.

Published application images are designed for `linux/amd64` and `linux/arm64`, carry OCI source/version/revision/license/build-time metadata, include the Apache-2.0 `LICENSE` and `NOTICE`, and are published with BuildKit SBOM and provenance attestations. Prerelease versions such as `0.1.0-alpha` deliberately do not receive the `latest` tag.

For a single backend image, after the verified release is actually available:

```bash
docker pull qingzonex/qtable:0.1.0-alpha
```

The image exposes port `9000` and has an image-level healthcheck against the QTable root health endpoint. Normal multi-user deployment still requires PostgreSQL and Redis, plus S3-compatible storage when attachments are enabled.

Maintainers configure publishing through GitHub repository settings rather than source control: set `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and optionally `DOCKERHUB_NAMESPACE` (default `qingzonex`). A `v*` Git tag must match `VERSION` exactly before the Docker Hub workflow can push. The workflow also emits a SHA tag and records the manifest digest. The existence of that workflow is not release approval: the first official push remains downstream of the exact-revision release gates described below.

### Default network exposure

The canonical Compose file follows a least-privilege host-exposure model. By default, PostgreSQL, Redis, the MinIO S3 API and admin console, and the QTable API are published only on `127.0.0.1`. Containers communicate with those services over the private Compose network; they do not need Internet-facing host bindings. QTableUI is the user-facing service and defaults to `0.0.0.0:${QTABLE_UI_PORT:-9100}` so an operator can place it behind a reverse proxy.

`QTABLE_INTERNAL_BIND_HOST` and `QTABLE_UI_BIND_HOST` in `.env` make the distinction explicit. Do **not** change `QTABLE_INTERNAL_BIND_HOST` to `0.0.0.0` on an Internet-facing machine unless you intentionally provide equivalent firewall/private-network controls. Do not expose the PostgreSQL, Redis, MinIO admin/API, or backend API ports directly to the public Internet merely to make the UI reachable.

For production, keep PostgreSQL as the database, set `APP_ENV=production`, replace `SECRET_KEY`, use a stable `ENCRYPTION_KEY`, replace the example attachment storage credentials (or point the Compose mapping values at a managed S3-compatible provider), configure persistent PostgreSQL and object-storage backups, TLS and an appropriate reverse proxy. Do not use the SQLite fallback template for multi-user production deployments.

## Docker build source portability

The backend Docker image uses official upstream sources by default: `PYTHON_IMAGE=python:3.11-slim`, the Debian sources shipped by that image are left unchanged, and `PIP_INDEX_URL=https://pypi.org/simple`. Regional mirrors are explicit build-time overrides only; the OSS default path never rewrites package sources. `requirements.txt` is copied and installed before application source so source-only changes keep Docker's dependency layer cacheable.

For a constrained China network, mirrors can be enabled without editing the Dockerfile:

```bash
docker build \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  -t qtable:local .
```

The same three values can be set in `.env` for `docker compose build qtable` or the normal `docker compose up --build -d`. Leave them at their documented defaults/empty values for the portable official-source build path.

## Database migrations

QTable now carries an Alembic baseline.

Fresh database:

```bash
alembic upgrade head
```

Existing deployments remain compatible with the current startup schema-repair path while the project transitions fully to versioned migrations. Future schema changes should be delivered as Alembic revisions.

## AI configuration

The application supports OpenAI-compatible and DeepSeek-compatible AI paths in the current codebase. API keys are stored through the application's encrypted AI configuration flow rather than committed to source control.

When `ENCRYPTION_KEY` is explicitly configured, it must be a Fernet key generated by:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not use an arbitrary password, placeholder, or plain random string as `ENCRYPTION_KEY`. If multiple QTable instances share the same database, they must use the same stable `ENCRYPTION_KEY`; changing it makes existing saved AI credentials unreadable until those credentials are re-saved or re-encrypted.

When `ENCRYPTION_KEY` is empty in local development, QTable derives a deterministic development key from `SECRET_KEY`. For shared development/deployment databases, prefer an explicit stable Fernet key instead.

Never commit real API keys to `.env`, source files, fixtures or screenshots.

## Security model

Important invariants:

- AI context must respect current-user row visibility.
- Preview operations must not mutate business data.
- Apply operations re-check permission and optimistic/concurrent state.
- Member values must refer to current workspace members.
- Attachment record values must resolve to an active registry entry bound to the same table, row and attachment field; direct/presigned URLs are not valid persisted attachment data.
- Attachment reads re-check current table + row permission and are served `private, no-store`.
- Public Dashboard data is evaluated against the publisher's current readable scope.
- OAuth public clients use S256 PKCE.
- Secrets must not be written to ordinary table fields or logs.

See [SECURITY.md](SECURITY.md) and the [QingZone Identity Contract v1](docs/identity-contract.md).

## Development

Run the backend suite:

```bash
pytest -q
```

Run open-source readiness checks:

```bash
python scripts/check_secrets.py --history
python scripts/check_open_source_readiness.py
```

Run migration smoke test:

```bash
rm -f /tmp/qtable-migration.db
DATABASE_MODE=sqlite SQLITE_PATH=/tmp/qtable-migration.db alembic upgrade head
python scripts/verify_migration.py /tmp/qtable-migration.db
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Roadmap

The public roadmap is maintained in GitHub Issues. Major active areas include:

- large-table performance and incremental realtime;
- commercial-quality templates and dashboards;
- table-as-API developer capabilities;
- Skill / Connector integrations;
- semantic duplicate detection;
- self-hosted / BYO AI hardening;
- comments, mentions and notifications.

## Release status

`v0.1.0-alpha` is being prepared as QTable's first **Open Source Preview / Alpha**. The current `main` state is a private pre-release candidate, not a published release. Do not treat it as released until the exact QTable and QTableUI revisions pass their real CI and full-stack release gates, the final release verification issues are closed, the repositories are intentionally transitioned to public visibility, and the verified `v0.1.0-alpha` tag/release is created. Docker Hub images are part of the same release artifact set and must be built from those verified release tags, not from an unrelated branch or a manual local rebuild.

The draft release notes remain at [docs/releases/v0.1.0-alpha.md](docs/releases/v0.1.0-alpha.md) and must keep their draft marker until that final sequence is complete.

## License

Apache License 2.0. See [LICENSE](LICENSE).
