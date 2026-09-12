# Python dependency reproducibility and audit policy

QTable keeps application dependency intent separate from the resolved release environment.

## Files

- `requirements.in` is the human-maintained set of direct dependency constraints.
- `requirements.txt` is the Alpha compatibility install manifest. Its direct constraints must stay semantically identical to `requirements.in` while existing deployment documentation transitions to the standardized lock.
- `pylock.toml` is the standardized PEP 751 resolved lock. Generate it with `python scripts/refresh_python_lock.py` on a networked Python 3.12 development machine using pip 26.1+ (`pip lock` was introduced in pip 26.1).
- `requirements-audit.txt` exact-pins the security, license and SBOM tooling used by the project.

Do not hand-edit a resolved `pylock.toml`. Change the reviewed direct constraint in `requirements.in` / `requirements.txt`, regenerate the lock, inspect the diff, run the dependency audit, and commit all affected files together.

## Generate or refresh the lock

```bash
python -m pip install --upgrade 'pip>=26.1'
python scripts/check_python_dependency_policy.py
python scripts/refresh_python_lock.py
python scripts/check_python_dependency_policy.py --require-lock
```

For a release that must resolve only artifacts that existed before a fixed timestamp, pass a UTC cutoff:

```bash
python scripts/refresh_python_lock.py \
  --uploaded-prior-to 2026-09-11T00:00:00Z
```

`pip lock` resolves for the current Python version and platform. QTable's canonical release environment is Python 3.12 on Linux; if additional platform-specific locks are introduced later, use PEP 751 named lock files and make the supported matrix explicit rather than pretending one platform lock covers all environments.

## Audit the installed environment

Install the application and exact-pinned audit tools, then run:

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-audit.txt
python scripts/audit_python_dependencies.py
```

The audit performs four independent checks:

1. `pip check` rejects an internally inconsistent installed dependency graph.
2. `pip-audit --strict` rejects known vulnerable installed distributions and writes a machine-readable JSON report.
3. `pip-licenses` writes the dependency license inventory and blocks declarations that require an explicit policy review. The matcher distinguishes LGPL from bare GPL so LGPL metadata is not rejected by substring accident.
4. `cyclonedx-py environment` emits a reproducible CycloneDX JSON SBOM for the installed environment.

Artifacts are written under `artifacts/python-dependencies/` by default. CI uploads them when artifact storage is available.

## Source policy

Release manifests must not embed private package indexes, localhost indexes, local file dependencies or unreviewed VCS dependencies. Regional PyPI mirrors remain operator/build configuration, not dependency identity.

A missing `pylock.toml` is allowed only during the current transition while the repository has no executable networked release runner. The normal policy check reports that state clearly. **Before a release candidate is tagged, `python scripts/check_python_dependency_policy.py --require-lock` must pass and the resulting lock diff must be reviewed.**

The repository still gains useful deterministic controls before that final materialization step: direct dependency intent is checked for drift, audit tooling is exact-pinned, source locations are constrained, and vulnerability/license/SBOM evidence is generated from the concrete installed environment.
