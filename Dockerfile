# 镜像源默认值面向受限网络：Rainbond 源码构建执行的是**不带任何 --build-arg**
# 的 `docker build`，且它的组件「构建源」不提供选择 Dockerfile 文件的入口，
# 所以官方上游源不能作为默认值，否则构建会在 load metadata 阶段直接超时。
# 需要可移植的官方上游源（OSS 路径）时，用构建参数显式恢复：
#   --build-arg PYTHON_IMAGE=python:3.11-slim
#   --build-arg APT_MIRROR=
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim

FROM ${PYTHON_IMAGE} AS builder

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i \
        -e "s|deb.debian.org|$APT_MIRROR|g" \
        -e "s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
      sed -i \
        -e "s|deb.debian.org|$APT_MIRROR|g" \
        -e "s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.in requirements.txt ./
RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" --upgrade \
      pip==26.2.1 setuptools==84.0.0 wheel==0.46.3 \
    && "$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" -r requirements.txt \
    && "$VIRTUAL_ENV/bin/python" -m pip uninstall -y pip

FROM ${PYTHON_IMAGE} AS runtime

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG QTABLE_VERSION=0.0.0-dev
ARG QTABLE_REVISION=unknown
ARG QTABLE_CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.title="QTable" \
      org.opencontainers.image.description="AI-native open-source project and work management built on multidimensional tables" \
      org.opencontainers.image.source="https://github.com/QingZoneX/qtable-server" \
      org.opencontainers.image.url="https://github.com/QingZoneX/qtable-server" \
      org.opencontainers.image.documentation="https://github.com/QingZoneX/qtable-server#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${QTABLE_VERSION}" \
      org.opencontainers.image.revision="${QTABLE_REVISION}" \
      org.opencontainers.image.created="${QTABLE_CREATED}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i \
        -e "s|deb.debian.org|$APT_MIRROR|g" \
        -e "s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
      sed -i \
        -e "s|deb.debian.org|$APT_MIRROR|g" \
        -e "s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --upgrade \
      pip==26.2.1 setuptools==84.0.0 wheel==0.46.3 \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && addgroup --system qtable \
    && adduser --system --ingroup qtable --home /home/qtable qtable \
    && mkdir -p /app /usr/share/licenses/qtable \
    && chown qtable:qtable /app /usr/share/licenses/qtable

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=qtable:qtable app ./app
COPY --chown=qtable:qtable alembic.ini ./
COPY --chown=qtable:qtable alembic ./alembic
COPY --chown=qtable:qtable VERSION ./
COPY --chown=qtable:qtable docker-entrypoint.sh ./docker-entrypoint.sh
COPY --chown=qtable:qtable LICENSE NOTICE /usr/share/licenses/qtable/
RUN chmod +x ./docker-entrypoint.sh

USER qtable

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:9000/ >/dev/null || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
