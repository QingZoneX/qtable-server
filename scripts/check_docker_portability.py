from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
assert VERSION and not any(ch.isspace() for ch in VERSION), VERSION
dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
registry_compose = (ROOT / "docker-compose.registry.yml").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
dockerhub_env = (ROOT / "dockerhub.env.example").read_text(encoding="utf-8")
dockerhub_doc = (ROOT / "docs/docker-hub.md").read_text(encoding="utf-8")
publish_workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

# 两条构建路径都必须被固定住：
# 1) 受限网络（Rainbond 源码构建）：它执行的是不带任何 --build-arg 的
#    `docker build`，且无法在构建源里选择其它 Dockerfile，所以默认值必须是
#    公网可达的镜像源；
# 2) 可移植的官方上游路径：必须仍能由构建参数显式恢复，发布流程与 OSS 本地
#    构建走这条，Dockerfile 头部注释是它的文档入口。
assert dockerfile.startswith("# 镜像源默认值面向受限网络"), dockerfile
assert "ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim" in dockerfile
assert "ARG APT_MIRROR=mirrors.aliyun.com" in dockerfile
assert "ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/" in dockerfile
assert "FROM ${PYTHON_IMAGE} AS builder" in dockerfile
assert "FROM ${PYTHON_IMAGE} AS runtime" in dockerfile
assert dockerfile.index(
    "ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim"
) < dockerfile.index("FROM ${PYTHON_IMAGE} AS builder"), (
    "PYTHON_IMAGE 是全局 ARG，必须声明在第一个 FROM 之前，否则 FROM 取不到覆盖值"
)
assert "--build-arg PYTHON_IMAGE=python:3.11-slim" in dockerfile
assert "--build-arg PIP_INDEX_URL=https://pypi.org/simple" in dockerfile
assert 'if [ -n "$APT_MIRROR" ]' in dockerfile
assert '"$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" -r requirements.txt' in dockerfile

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

assert "## Docker build source portability" in readme
assert "reachable mirrors as the build default" in readme
assert "docker.m.daocloud.io/library/python:3.11-slim" in readme
assert "mirrors.aliyun.com/pypi/simple/" in readme
assert "--build-arg PYTHON_IMAGE=python:3.11-slim" in readme

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
assert re.search(r"aquasecurity/trivy-action@v\d+\.\d+\.\d+", publish_workflow), (
    "Trivy action must be pinned to an explicit release"
)

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
