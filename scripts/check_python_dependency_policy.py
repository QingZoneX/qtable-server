#!/usr/bin/env python3
"""Static dependency-policy gate for QTable's Python environment."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ABSTRACT = ROOT / "requirements.in"
COMPAT = ROOT / "requirements.txt"
AUDIT = ROOT / "requirements-audit.txt"
LOCK = ROOT / "pylock.toml"

REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?")
FORBIDDEN_SOURCE_MARKERS = (
    "--index-url",
    "--extra-index-url",
    "@ git+",
    "@ file:",
    "localhost",
    "127.0.0.1",
)


def fail(message: str) -> None:
    raise SystemExit(f"[python-dependency-policy] {message}")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_lines(path: Path) -> list[str]:
    if not path.is_file():
        fail(f"missing dependency file: {path.relative_to(ROOT)}")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def requirement_names(lines: list[str], source: str) -> set[str]:
    names: set[str] = set()
    for line in lines:
        match = REQUIREMENT_NAME.match(line)
        if not match:
            fail(f"{source}: unsupported requirement syntax {line!r}")
        name = canonical(match.group(1))
        if name in names:
            fail(f"{source}: duplicate direct dependency {name}")
        names.add(name)
    return names


def ensure_public_sources(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for marker in FORBIDDEN_SOURCE_MARKERS:
        if marker in text:
            fail(f"{path.name} contains forbidden source marker {marker!r}")


def validate_lock(direct_names: set[str]) -> None:
    try:
        payload = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse pylock.toml: {exc}")

    if payload.get("lock-version") != "1.0":
        fail("pylock.toml must use lock-version = '1.0'")
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        fail("pylock.toml must contain a non-empty [[packages]] resolution")

    resolved_names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            fail("pylock.toml contains a non-table package entry")
        name = canonical(str(package.get("name") or ""))
        version = str(package.get("version") or "").strip()
        if not name or not version:
            fail("every locked package must have name and version")
        if name in resolved_names:
            fail(f"pylock.toml resolves {name!r} more than once")
        resolved_names.add(name)

        if "directory" in package or "vcs" in package:
            fail(
                f"pylock.toml package {name!r} uses a local/VCS source; "
                "release dependencies must resolve from auditable archives/wheels"
            )

    missing = sorted(direct_names - resolved_names)
    if missing:
        fail(f"pylock.toml is missing direct dependencies: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-lock",
        action="store_true",
        help="Fail when pylock.toml has not been materialized yet.",
    )
    args = parser.parse_args()

    abstract_lines = requirement_lines(ABSTRACT)
    compat_lines = requirement_lines(COMPAT)
    audit_lines = requirement_lines(AUDIT)

    direct_names = requirement_names(abstract_lines, ABSTRACT.name)
    compat_names = requirement_names(compat_lines, COMPAT.name)
    if direct_names != compat_names:
        missing = sorted(direct_names - compat_names)
        extra = sorted(compat_names - direct_names)
        fail(
            "requirements.in and requirements.txt direct dependency sets drifted; "
            f"missing={missing}, extra={extra}"
        )

    # During the Alpha transition the compatibility manifest must retain the
    # same reviewed direct constraints, not merely the same package names.
    if sorted(abstract_lines) != sorted(compat_lines):
        fail("requirements.in and requirements.txt direct constraints must stay identical")

    for line in audit_lines:
        if "==" not in line:
            fail(f"{AUDIT.name}: audit tool must be exact-pinned: {line!r}")

    for path in (ABSTRACT, COMPAT, AUDIT):
        ensure_public_sources(path)

    if LOCK.is_file():
        validate_lock(direct_names)
        lock_status = "validated"
    elif args.require_lock:
        fail(
            "pylock.toml is required for this gate; run "
            "python scripts/refresh_python_lock.py on a networked development machine"
        )
    else:
        lock_status = "not materialized (policy ready; use --require-lock for release)"

    print(
        "[python-dependency-policy] OK - "
        f"{len(direct_names)} direct dependencies, lock {lock_status}"
    )


if __name__ == "__main__":
    main()
