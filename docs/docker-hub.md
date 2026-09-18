# Docker Hub distribution

QTable's official container distribution is designed around two application images:

- `qingzonex/qtable`
- `qingzonex/qtable-ui`

The namespace can be overridden in the GitHub publishing workflows through the `DOCKERHUB_NAMESPACE` repository variable before the first public release.

## Release tags

For the alpha channel the canonical tags are:

- `0.1.2-alpha` from Git tag `v0.1.2-alpha`
- `sha-<short commit>`
- moving `alpha` channel tag

The canonical workflow is `.github/workflows/docker-publish.yml`. It deliberately does not publish `latest` for prereleases. Stable releases may publish `latest` only when the version has no prerelease suffix. A tag publish refuses to run when the Git tag does not exactly match `v$(cat VERSION)`.

## GitHub configuration

Before enabling publication, configure:

- repository variable `DOCKERHUB_NAMESPACE` (optional; defaults to `qingzonex`);
- repository variable `DOCKERHUB_USERNAME`;
- repository variable `DOCKERHUB_PUBLISH_ENABLED=true` only when release publication is authorized;
- secret `DOCKERHUB_TOKEN` using a dedicated least-privilege Docker Hub access token.

Keep `DOCKERHUB_PUBLISH_ENABLED` unset/false during private release preparation. Pull-request and manual `workflow_dispatch` runs are build-only verification paths and do not push. A `v*` tag can push only after the release gate explicitly enables publication.

## Consumer Compose stack

`docker-compose.registry.yml` pulls the published QTable/QTableUI application images instead of building those two repositories locally. PostgreSQL and Redis use upstream images. The currently pinned MinIO helper is built from this QTable repository so attachment behavior remains aligned with the canonical stack; operators can point QTable at an external S3-compatible service instead.

```bash
cp dockerhub.env.example .env
# Replace every CHANGE_ME value and generate ENCRYPTION_KEY as documented.
docker compose --env-file .env -f docker-compose.registry.yml pull db redis qtable qtable-ui
docker compose --env-file .env -f docker-compose.registry.yml up -d
```

The UI is exposed on port 9100 by default. PostgreSQL, Redis, MinIO and the QTable API stay loopback-bound by default.

Override the application image references with `QTABLE_IMAGE` and `QTABLE_UI_IMAGE` when testing another namespace, immutable SHA tag or registry mirror.

## Production secrets

The registry Compose file fails closed when these are missing:

- `POSTGRES_PASSWORD`
- `SECRET_KEY`
- `ENCRYPTION_KEY`
- `ATTACHMENT_S3_ACCESS_KEY`
- `ATTACHMENT_S3_SECRET_KEY`

`SECRET_KEY` must be a long random value. `ENCRYPTION_KEY` must be a stable Fernet key. Do not reuse the example values for an Internet-facing deployment.

## Runtime hardening

The QTable image uses a multi-stage build: compilation tooling is confined to the builder stage and the final runtime stage contains only the installed Python environment plus runtime packages. The API runs as the dedicated non-root `qtable` user. `/app` is owned by that user so the documented SQLite fallback can still create a relative database file when explicitly selected.

The final image carries an HTTP healthcheck on port 9000 and embeds Apache-2.0 `LICENSE` and `NOTICE` under `/usr/share/licenses/qtable/`.

## Image verification and provenance

Release builds target `linux/amd64` and `linux/arm64`, attach OCI source/version/revision/license/build-time metadata, and request BuildKit SBOM plus `mode=max` provenance attestations.

Before the multi-architecture image is published, the workflow builds the exact revision locally and runs a HIGH/CRITICAL container CVE gate. The backend Docker portability workflow separately builds and starts the real runtime image, validates the non-root user, healthcheck and license payload, and exercises the canonical Compose backend path.

Python dependency locking is governed separately; the current runtime hardening must not be mistaken for a deterministic lockfile until that work is complete.

Before an official release is published, the exact QTable and QTableUI revisions must complete the existing Backend CI, Frontend CI and full-stack release E2E gates. Docker Hub publication is a distribution step, not a replacement for QTable#170 / QTable#139 release verification.
