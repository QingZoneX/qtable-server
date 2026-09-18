# QTable

> AI-native, open-source project and work management built on multidimensional tables.

**Status:** public open-source Alpha (`0.1.2-alpha`). The repositories are public and ready for evaluation and contribution; the first verified release tag and release artifacts remain gated by CI and release verification.

QTable combines flexible tables, multiple project views, dashboards, permissions, automation and AI workflows. AI changes follow **Preview → Confirm → Apply** and reuse the same Table / View / Dashboard / Permission models as manual work.

- Backend: [QingZoneX/qtable-server](https://github.com/QingZoneX/qtable-server)
- Web frontend: [QingZoneX/qtable-web](https://github.com/QingZoneX/qtable-web)
- Project portal and documentation: [QingZoneX.github.io](https://qingzonex.github.io/)

## Why QTable

Traditional multidimensional tables usually start with configuration. QTable aims for a shorter path from a goal to executable work:

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
Preview changes
      ↓
Confirm and apply
```

The underlying tables remain editable throughout the workflow. AI is an optional execution layer on top of the same structured product model rather than a second data model.

## Highlights

- Grid / Kanban / Gantt / Calendar / Gallery views.
- Filters, multi-field sorting, grouping and named views.
- Formula, relation, auto-number, member, select, date, attachment and other field types.
- Workspace membership, item permissions and row-level permissions.
- Durable private attachments backed by S3-compatible object storage.
- Dashboards with server-side aggregation and permission-safe public sharing.
- ChangeSet-based audit history and safe undo foundations.
- AI goal-driven workspace generation, task planning, workload estimation and member assignment.
- AI Project Steward diagnostics and question answering.
- AI action plans with diff preview, partial acceptance and permission revalidation.
- AI-generated Views and Dashboards using existing product models.
- QNote / Clipper Source Inbox with duplicate hints and explicit task conversion.
- OAuth 2.0 Authorization Code Flow with S256 PKCE.
- PostgreSQL + Redis as the recommended development and deployment stack, with SQLite as an explicit lightweight fallback.

## Architecture

```text
QTable Web (React + TypeScript + VTable + Apollo)
               │
        HTTP / GraphQL / WS
               │
QTable API (FastAPI + Strawberry GraphQL)
       │                 │
 PostgreSQL/SQLite      Redis
       │
 Permission / ChangeSet / AI services
       │
 S3-compatible attachment storage
```

AI providers are configured by the user. Core table functionality does not require an external AI service.

## Quick start: PostgreSQL (recommended)

Requirements:

- Python 3.12 recommended.
- Node.js 22 for the web frontend.
- Docker / Docker Compose for the recommended local PostgreSQL + Redis + MinIO dependencies.

Clone the backend:

```bash
git clone https://github.com/QingZoneX/qtable-server.git
cd qtable-server
cp .env.example .env

docker compose up -d db redis minio

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

alembic upgrade head
uvicorn app.main:app --reload --port 9000
```

Run the web frontend in another terminal:

```bash
git clone https://github.com/QingZoneX/qtable-web.git
cd qtable-web
npm ci
npm run dev
```

Open <http://localhost:9100>. GraphQL is available at <http://localhost:9000/graphql>.

PostgreSQL is QTable's default database for normal development and deployment. If you already have PostgreSQL, edit the `POSTGRES_*` values in `.env` instead of starting the Compose database.

## Attachment storage contract

Attachments are private application data, not public object URLs. A table cell stores stable metadata such as:

```text
attachmentId + objectKey + name + size + contentType
```

QTable does not persist presigned URLs in records. Upload, download and delete operations go through authenticated QTable APIs and re-check current table and row permissions.

The canonical `.env.example`, backend settings and Docker Compose stack use the same S3-compatible storage contract. Important runtime settings include:

- `ATTACHMENT_STORAGE_ENABLED`
- `ATTACHMENT_S3_ENDPOINT`
- `ATTACHMENT_S3_ACCESS_KEY`
- `ATTACHMENT_S3_SECRET_KEY`
- `ATTACHMENT_S3_BUCKET`
- optional `ATTACHMENT_S3_REGION`
- `ATTACHMENT_S3_SECURE`
- `ATTACHMENT_MAX_BYTES`
- cleanup batch / interval settings
- `ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS`

Soft-deleted rows retain their attachment objects so restore remains real. Permanent purge makes those objects eligible for durable background cleanup. Uploads use a durable intent before the object-store write so interrupted uploads can be recovered and cleaned safely.

## SQLite fallback

SQLite is available for lightweight evaluation, offline development or constrained single-instance use. It is not the recommended deployment database.

```bash
cp .env.sqlite.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 9000
```

QTable never silently falls back from PostgreSQL to SQLite. Switching database engines is explicit through `DATABASE_MODE`.

## One-command self-hosted stack

Clone the two repositories as siblings:

```text
qingzone/
├── qtable-server/
└── qtable-web/
```

Then:

```bash
cd qtable-server
cp .env.example .env
docker compose up --build -d
```

The canonical stack contains:

- QTable API
- QTable web frontend
- PostgreSQL 16
- Redis 7
- MinIO S3-compatible private attachment storage

Open <http://localhost:9100>.

## Docker distribution

Release workflows are prepared for the following application images:

```text
qingzonex/qtable:0.1.2-alpha
qingzonex/qtable-ui:0.1.2-alpha
```

Treat an image as an official release artifact only after the corresponding verified tag has passed the release gates. Prerelease versions deliberately do not receive the `latest` tag.

The registry Compose path is available for deployments that prefer pulling application images rather than building both repositories locally:

```bash
cp .env.example .env
docker compose -f docker-compose.registry.yml pull db redis qtable qtable-ui
docker compose -f docker-compose.registry.yml up -d
```

Before production use, set strong database and application secrets, configure a stable Fernet `ENCRYPTION_KEY`, replace the example attachment credentials, review public URLs, configure persistent backups and terminate TLS through an appropriate gateway or reverse proxy.

## Default network exposure

The canonical Compose stack follows a least-privilege host-exposure model. PostgreSQL, Redis, MinIO and the QTable API are loopback-only by default. Containers communicate over the private Compose network; the web frontend is the intended user-facing entry point.

Do not expose PostgreSQL, Redis, MinIO administration/API ports or the backend API directly to the public Internet merely to make the UI reachable.

## Docker build source portability

The backend Docker image uses reachable mirrors as the build default: `PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim`, the Debian sources shipped by that image are rewritten to `mirrors.aliyun.com`, and `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`. The default path has to build with no build arguments at all because Rainbond source builds run a bare `docker build` and cannot select an alternate Dockerfile. `requirements.txt` is copied and installed before application source so source-only changes keep Docker's dependency layer cacheable.

The portable official-upstream path is one explicit override away, and it is what the published Docker Hub images use:

```bash
docker build \
  --build-arg PYTHON_IMAGE=python:3.11-slim \
  --build-arg APT_MIRROR= \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  -t qtable:local .
```

`.github/workflows/docker-publish.yml` and `.github/workflows/docker-portability.yml` pass exactly those arguments, so release images and the portability gate never depend on a regional mirror. `docker compose build qtable` forwards the same three values from `.env` (documented in `.env.example`), which keeps the official-source path for Compose users; on a constrained network set them to the mirror values above or remove them to fall back to the Dockerfile defaults.

## Database migrations

QTable carries Alembic migrations. For a fresh database:

```bash
alembic upgrade head
```

Future schema changes should be delivered as versioned Alembic revisions.

## AI configuration

The current codebase supports OpenAI-compatible and DeepSeek-compatible AI paths. API keys are stored through the encrypted AI configuration flow rather than committed to source control.

For an explicit encryption key, generate a valid Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Multiple QTable instances sharing one database must use the same stable encryption key. Never commit real API keys to `.env`, source files, fixtures or screenshots.

## Security model

Important invariants:

- AI context must respect current-user row visibility.
- Preview operations must not mutate business data.
- Apply operations re-check permission and optimistic/concurrent state.
- Member values must refer to current workspace members.
- Attachment values must resolve to active registry entries bound to the same table, row and attachment field.
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

Run migration smoke tests:

```bash
rm -f /tmp/qtable-migration.db
DATABASE_MODE=sqlite SQLITE_PATH=/tmp/qtable-migration.db alembic upgrade head
python scripts/verify_migration.py /tmp/qtable-migration.db
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Roadmap

The public roadmap is maintained through the project documentation and GitHub Issues. Major active areas include:

- large-table performance and incremental realtime;
- commercial-quality templates and dashboards;
- table-as-API developer capabilities;
- Skill / Connector integrations;
- semantic duplicate detection;
- self-hosted / BYO AI hardening;
- comments, mentions and notifications.

## Release status

QTable is now developed in public under the QingZoneX organization. `0.1.2-alpha` remains a prerelease line: repository visibility does not by itself make a commit, Docker image or tag an official release artifact.

A release becomes official only when the exact server and web revisions pass their CI and full-stack release gates and the corresponding verified tag/release is published. Draft release notes remain under [`docs/releases/`](docs/releases/).

## License

Apache License 2.0. See [LICENSE](LICENSE).
