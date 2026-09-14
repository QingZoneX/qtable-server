from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
registry_compose = (ROOT / "docker-compose.registry.yml").read_text(encoding="utf-8")
env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
dockerhub_env = (ROOT / "dockerhub.env.example").read_text(encoding="utf-8")
dockerhub_doc = (ROOT / "docs/docker-hub.md").read_text(encoding="utf-8")
publish_workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

assert VERSION and not any(ch.isspace() for ch in VERSION), VERSION
assert dockerfile.startswith("ARG PYTHON_IMAGE=python:3.11-slim\n\nFROM ${PYTHON_IMAGE} AS builder\n"), dockerfile
assert "FROM ${PYTHON_IMAGE} AS runtime" in dockerfile
assert "ARG APT_MIRROR=" in dockerfile
assert "ARG PIP_INDEX_URL=https://pypi.org/simple" in dockerfile
assert 'if [ -n "$APT_MIRROR" ]' in dockerfile
assert '"$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" -r requirements.txt' in dockerfile
assert "docker.m.daocloud.io" not in dockerfile
assert "mirrors.aliyun.com" not in dockerfile

requirements_index = dockerfile.index("COPY requirements.txt ./")
requirements_install = dockerfile.index('"$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" -r requirements.txt')
runtime_stage = dockerfile.index("FROM ${PYTHON_IMAGE} AS runtime")
app_copy = dockerfile.index("COPY --chown=qtable:qtable app ./app")
assert requirements_index < requirements_install < runtime_stage < app_copy

runtime_text = dockerfile[runtime_stage:]
assert "build-essential" not in runtime_text
assert "USER qtable" in runtime_text
assert "chown qtable:qtable /app" in runtime_text
assert "HEALTHCHECK" in runtime_text
assert 'org.opencontainers.image.source="https://github.com/QingZoneX/qtable-server"' in runtime_text
assert 'org.opencontainers.image.licenses="Apache-2.0"' in runtime_text
assert 'org.opencontainers.image.created="${QTABLE_CREATED}"' in runtime_text
assert "COPY --chown=qtable:qtable LICENSE NOTICE /usr/share/licenses/qtable/" in runtime_text

for expected in (
    "PYTHON_IMAGE: ${PYTHON_IMAGE:-python:3.11-slim}",
    "APT_MIRROR: ${APT_MIRROR:-}",
    "PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}",
):
    assert expected in compose, expected

for expected in (
    "PYTHON_IMAGE=python:3.11-slim",
    "APT_MIRROR=",
    "PIP_INDEX_URL=https://pypi.org/simple",
    "docker.m.daocloud.io/library/python:3.11-slim",
    "mirrors.aliyun.com/pypi/simple/",
):
    assert expected in env_example, expected

for expected in (
    f"image: ${{QTABLE_IMAGE:-qingzonex/qtable:{VERSION}}}",
    f"image: ${{QTABLE_UI_IMAGE:-qingzonex/qtable-ui:{VERSION}}}",
    "POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set",
    "SECRET_KEY:?SECRET_KEY must be set",
    "ENCRYPTION_KEY:?ENCRYPTION_KEY must be set",
    "APP_ENV: production",
):
    assert expected in registry_compose, expected

for expected in (
    f"QTABLE_IMAGE=qingzonex/qtable:{VERSION}",
    f"QTABLE_UI_IMAGE=qingzonex/qtable-ui:{VERSION}",
    "POSTGRES_PASSWORD=CHANGE_ME_DATABASE_PASSWORD",
    "SECRET_KEY=CHANGE_ME_LONG_RANDOM_SECRET",
    "ENCRYPTION_KEY=CHANGE_ME_FERNET_KEY",
):
    assert expected in dockerhub_env, expected

for expected in (
    "linux/amd64,linux/arm64",
    "docker/setup-qemu-action@v4",
    "docker/setup-buildx-action@v4",
    "docker/login-action@v4",
    "docker/metadata-action@v6",
    "docker/build-push-action@v7",
    "aquasecurity/trivy-action@v0.35.0",
    "severity: 'CRITICAL,HIGH'",
    "exit-code: '1'",
    "vuln-type: 'os,library'",
    "provenance: mode=max",
    "sbom: true",
    "latest=false",
    "DOCKERHUB_USERNAME",
    "DOCKERHUB_TOKEN",
    "DOCKERHUB_PUBLISH_ENABLED",
    'tag_version="${GITHUB_REF_NAME#v}"',
    "QTABLE_CREATED=${{ steps.identity.outputs.created }}",
):
    assert expected in publish_workflow, expected

for expected in (
    "Git tag",
    "Docker Hub",
    "docker-compose.registry.yml",
    "DOCKERHUB_PUBLISH_ENABLED",
    ".github/workflows/docker-publish.yml",
    "non-root `qtable`",
):
    assert expected in dockerhub_doc, expected

print(f"[docker-portability] source portability, hardened runtime and Docker Hub distribution contracts verified for {VERSION}")
