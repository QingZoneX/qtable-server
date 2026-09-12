#!/usr/bin/env python3
"""Generate and validate QTable's standardized PEP 751 Python lock file.

The command intentionally uses pip's native ``pip lock`` implementation so the
output is the ecosystem-standard ``pylock.toml`` format instead of a
project-specific lock dialect.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "requirements.in"
LOCK = ROOT / "pylock.toml"
# `pip lock` was introduced in pip 26.1. Keep this aligned with pip's command
# availability rather than merely the release that introduced --only-final.
MINIMUM_PIP = (26, 1)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"[python-lock] {message}")


def pip_version_tuple() -> tuple[int, int]:
    raw = importlib.metadata.version("pip")
    parts = raw.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        fail(f"cannot parse pip version {raw!r}")


def validate_lock(path: Path) -> dict:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot read generated lock: {exc}")

    if payload.get("lock-version") != "1.0":
        fail("pylock.toml must use PEP 751 lock-version = '1.0'")

    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        fail("pylock.toml must contain a non-empty [[packages]] resolution")

    seen: set[tuple[str, str]] = set()
    for package in packages:
        if not isinstance(package, dict):
            fail("every pylock package entry must be a table")
        name = str(package.get("name") or "").strip().lower()
        version = str(package.get("version") or "").strip()
        if not name or not version:
            fail("every pylock package entry must include name and version")
        identity = (name, version)
        if identity in seen:
            fail(f"duplicate pylock package identity: {name}=={version}")
        seen.add(identity)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--uploaded-prior-to",
        default=os.environ.get("QTABLE_LOCK_UPLOADED_PRIOR_TO"),
        help=(
            "Optional ISO-8601 cutoff passed to pip lock. Use it when a release "
            "must be recreated from packages that existed before a fixed time."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=LOCK,
        help="Lock output path (default: repository pylock.toml)",
    )
    args = parser.parse_args()

    if not INPUT.is_file():
        fail(f"missing lock input: {INPUT.relative_to(ROOT)}")

    version = pip_version_tuple()
    if version < MINIMUM_PIP:
        fail(
            "pip lock requires pip >= 26.1; run "
            f"{sys.executable} -m pip install --upgrade 'pip>=26.1' first"
        )

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="qtable-pylock-",
        dir=output.parent,
    ) as temporary_dir:
        # PEP 751 requires the standardized filename form. Keeping the
        # temporary name valid also avoids pip warning/error differences
        # between versions.
        temporary = Path(temporary_dir) / "pylock.toml"
        command = [
            sys.executable,
            "-m",
            "pip",
            "lock",
            "--requirement",
            str(INPUT),
            "--output",
            str(temporary),
            "--only-final",
            ":all:",
        ]
        if args.uploaded_prior_to:
            command.extend(["--uploaded-prior-to", args.uploaded_prior_to])

        subprocess.run(command, cwd=ROOT, check=True)
        payload = validate_lock(temporary)
        staging = output.with_name(f".{output.name}.tmp")
        try:
            staging.write_bytes(temporary.read_bytes())
            staging.replace(output)
        finally:
            staging.unlink(missing_ok=True)

    print(
        "[python-lock] wrote "
        f"{output.relative_to(ROOT) if output.is_relative_to(ROOT) else output} "
        f"with {len(payload['packages'])} resolved package entries"
    )


if __name__ == "__main__":
    main()
