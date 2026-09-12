ARG PYTHON_IMAGE=python:3.11-slim

FROM ${PYTHON_IMAGE} AS builder

ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple

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
COPY requirements.txt ./
RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --index-url "$PIP_INDEX_URL" -r requirements.txt

FROM ${PYTHON_IMAGE} AS runtime

ARG APT_MIRROR=
ARG QTABLE_VERSION=0.0.0-dev
ARG QTABLE_REVISION=unknown
ARG QTABLE_CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.title="QTable" \
      org.opencontainers.image.description="AI-native open-source project and work management built on multidimensional tables" \
      org.opencontainers.image.source="https://github.com/QingZoneX/QTable" \
      org.opencontainers.image.url="https://github.com/QingZoneX/QTable" \
      org.opencontainers.image.documentation="https://github.com/QingZoneX/QTable#readme" \
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
    apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
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
