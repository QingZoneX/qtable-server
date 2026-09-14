from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

REQUIRED = [
    "LICENSE", "NOTICE", "README.md", "CONTRIBUTING.md", "SECURITY.md",
    "CHANGELOG.md", "VERSION", ".env.example", ".env.sqlite.example", "Dockerfile",
    "docker-compose.yml", "docker-compose.registry.yml", "dockerhub.env.example",
    "alembic.ini", "alembic/env.py", "alembic/versions/0001_open_source_baseline.py",
    ".github/workflows/docker-publish.yml", "docs/docker-hub.md",
    "scripts/check_docker_portability.py", f"docs/releases/v{VERSION}.md",
]
FORBIDDEN_PREFIXES = [".codebuddy/", "deliverables/"]
FORBIDDEN_EXACT = {
    "test_oauth_manual.html",
    "scripts/test_ai_create_record.py",
    "scripts/test_insert_row_with_data.py",
    ".github/workflows/dockerhub-publish.yml",
}
ALLOWED_TABLE_FIXTURES = {"app/data/tables/dstDefault.json"}


def fail(message: str) -> None:
    raise SystemExit("[open-source-readiness] " + message)


def tracked() -> set[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"])
    return {p.decode("utf-8") for p in raw.split(b"\0") if p}


if not VERSION or any(ch.isspace() for ch in VERSION):
    fail(f"VERSION must be a non-empty single token, got {VERSION!r}")

files = tracked()
for path in REQUIRED:
    if path not in files:
        fail(f"required public file is missing: {path}")

for path in files:
    if any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        fail(f"internal development artifact is still tracked: {path}")
    if path in FORBIDDEN_EXACT:
        fail(f"forbidden or duplicate release artifact is tracked: {path}")

legacy_tables = {p for p in files if p.startswith("app/data/tables/")}
if legacy_tables != ALLOWED_TABLE_FIXTURES:
    fail(f"unexpected runtime table fixtures remain: {sorted(legacy_tables)}")

license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
if "Apache License" not in license_text or "Version 2.0" not in license_text:
    fail("LICENSE is not Apache License 2.0")

local_markers = ["/" + "Users" + "/", "\\" + "Users" + "\\", "gitee" + "/QSpace"]
for path in sorted(files):
    try:
        text = (ROOT / path).read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for forbidden in local_markers:
        if forbidden in text:
            fail(f"{path} contains internal/local path marker {forbidden!r}")

env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
if "DATABASE_MODE=postgres" not in env_example or "DATA_BACKEND=db" not in env_example:
    fail(".env.example must use the PostgreSQL database backend by default")
for token in ["QTABLE_INTERNAL_BIND_HOST=127.0.0.1", "QTABLE_UI_BIND_HOST=0.0.0.0"]:
    if token not in env_example:
        fail(f".env.example is missing safe Compose exposure setting: {token}")

sqlite_env_example = (ROOT / ".env.sqlite.example").read_text(encoding="utf-8")
if "DATABASE_MODE=sqlite" not in sqlite_env_example or "DATA_BACKEND=db" not in sqlite_env_example:
    fail(".env.sqlite.example must explicitly select the SQLite database backend")

compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
for service in ["db:", "redis:", "qtable:", "qtable-ui:"]:
    if service not in compose:
        fail(f"docker-compose.yml is missing service {service}")
if "QTABLE_UI_CONTEXT" not in compose:
    fail("docker-compose.yml must allow the sibling QTableUI context")
if "DATABASE_MODE: postgres" not in compose or "DATA_BACKEND: db" not in compose:
    fail("docker-compose.yml must use PostgreSQL with the canonical db backend")
for token in [
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${POSTGRES_HOST_PORT:-5432}:5432',
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${REDIS_HOST_PORT:-6379}:6379',
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${MINIO_API_HOST_PORT:-9001}:9000',
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${MINIO_CONSOLE_HOST_PORT:-9002}:9001',
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${QTABLE_API_PORT:-9000}:9000',
]:
    if token not in compose:
        fail(f"docker-compose.yml internal host exposure is not loopback-safe: {token}")
if '${QTABLE_UI_BIND_HOST:-0.0.0.0}:${QTABLE_UI_PORT:-9100}:9100' not in compose:
    fail("docker-compose.yml must keep the user-facing UI bind host explicit")

registry_compose = (ROOT / "docker-compose.registry.yml").read_text(encoding="utf-8")
for token in [
    f'image: ${{QTABLE_IMAGE:-qingzonex/qtable:{VERSION}}}',
    f'image: ${{QTABLE_UI_IMAGE:-qingzonex/qtable-ui:{VERSION}}}',
    "APP_ENV: production",
    'SECRET_KEY: ${SECRET_KEY:?SECRET_KEY must be set}',
    'ENCRYPTION_KEY: ${ENCRYPTION_KEY:?ENCRYPTION_KEY must be set}',
    'POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}',
    'PASSWORD_RESET_DEBUG_TOKEN_ENABLED: "false"',
    'PASSWORD_RESET_RATE_LIMIT_ENABLED: "true"',
    'JWT_ACCEPT_LEGACY_TOKENS: ${JWT_ACCEPT_LEGACY_TOKENS:-false}',
    '${QTABLE_INTERNAL_BIND_HOST:-127.0.0.1}:${QTABLE_API_PORT:-9000}:9000',
    '${QTABLE_UI_BIND_HOST:-0.0.0.0}:${QTABLE_UI_PORT:-9100}:9100',
]:
    if token not in registry_compose:
        fail(f"docker-compose.registry.yml is missing distribution safety token: {token}")
if "  qtable:\n    build:" in registry_compose:
    fail("registry Compose must pull the QTable application image instead of building it")
if "  qtable-ui:\n    build:" in registry_compose:
    fail("registry Compose must pull the QTableUI application image instead of building it")

dockerhub_env = (ROOT / "dockerhub.env.example").read_text(encoding="utf-8")
for token in [
    f"QTABLE_IMAGE=qingzonex/qtable:{VERSION}",
    f"QTABLE_UI_IMAGE=qingzonex/qtable-ui:{VERSION}",
    "POSTGRES_PASSWORD=CHANGE_ME_DATABASE_PASSWORD",
    "SECRET_KEY=CHANGE_ME_LONG_RANDOM_SECRET",
    "ENCRYPTION_KEY=CHANGE_ME_FERNET_KEY",
    "ATTACHMENT_S3_SECRET_KEY=CHANGE_ME_OBJECT_STORAGE_SECRET",
]:
    if token not in dockerhub_env:
        fail(f"dockerhub.env.example is missing safe consumer token: {token}")

dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
for token in [
    "FROM ${PYTHON_IMAGE} AS builder",
    "FROM ${PYTHON_IMAGE} AS runtime",
    'org.opencontainers.image.source="https://github.com/QingZoneX/qtable-server"',
    'org.opencontainers.image.licenses="Apache-2.0"',
    'org.opencontainers.image.version="${QTABLE_VERSION}"',
    'org.opencontainers.image.revision="${QTABLE_REVISION}"',
    'org.opencontainers.image.created="${QTABLE_CREATED}"',
    "COPY --chown=qtable:qtable LICENSE NOTICE /usr/share/licenses/qtable/",
    "USER qtable",
    "chown qtable:qtable /app",
    "HEALTHCHECK --interval=30s",
    "http://127.0.0.1:9000/",
]:
    if token not in dockerfile:
        fail(f"Dockerfile is missing release-image contract token: {token}")
runtime_text = dockerfile[dockerfile.index("FROM ${PYTHON_IMAGE} AS runtime"):]
if "build-essential" in runtime_text:
    fail("runtime Docker stage must not contain build-essential")

publish = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
for token in [
    'docker/login-action@v4', 'docker/setup-qemu-action@v4', 'docker/setup-buildx-action@v4',
    'docker/metadata-action@v6', 'docker/build-push-action@v7', 'aquasecurity/trivy-action@v0.35.0',
    "severity: 'CRITICAL,HIGH'", "exit-code: '1'", "vuln-type: 'os,library'",
    'platforms: linux/amd64,linux/arm64', 'provenance: mode=max', 'sbom: true',
    'DOCKERHUB_TOKEN', 'DOCKERHUB_PUBLISH_ENABLED', "DOCKERHUB_NAMESPACE || 'qingzonex'",
    'latest=false', "!contains(steps.identity.outputs.version, '-')",
    'tag_version="${GITHUB_REF_NAME#v}"', 'QTABLE_REVISION=${{ github.sha }}',
    'QTABLE_CREATED=${{ steps.identity.outputs.created }}',
]:
    if token not in publish:
        fail(f"Docker publish workflow is missing release contract token: {token}")

workspace = json.loads((ROOT / "app/data/workspace.json").read_text(encoding="utf-8"))
children = workspace.get("root", {}).get("children", [])
if len(children) != 1 or children[0].get("id") != "dstDefault":
    fail("workspace.json must contain only the clean default table")

for path in sorted(p for p in files if p.startswith("app/data/") and p.endswith(".json")):
    payload = json.loads((ROOT / path).read_text(encoding="utf-8"))
    if path.startswith("app/data/tables/") and payload.get("records"):
        fail(f"{path}: public runtime fixture must not contain business records")
    fields = payload.get("fields", []) if isinstance(payload, dict) else []
    for field in fields:
        if not isinstance(field, dict) or field.get("type") != "member":
            continue
        if field.get("options"):
            fail(f"{path}: member field must not persist workspace-member options")
        prop = field.get("property")
        if prop is not None and not isinstance(prop.get("multiple"), bool):
            fail(f"{path}: member property.multiple must be boolean when present")

readme = (ROOT / "README.md").read_text(encoding="utf-8")
for token in [
    VERSION,
    "docker compose up --build -d",
    "docker-compose.registry.yml",
    f"qingzonex/qtable:{VERSION}",
    f"qingzonex/qtable-ui:{VERSION}",
    "alembic upgrade head",
    "SECURITY.md",
    "CONTRIBUTING.md",
]:
    if token not in readme:
        fail(f"README is missing required release/setup token: {token}")

print(f"[open-source-readiness] OK - public contract verified for {VERSION}")
