#!/usr/bin/env python3
"""Run QTable's Python dependency health checks and emit reviewable artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Licenses in this set require an explicit policy review before a dependency can
# ship with QTable's Apache-2.0 distribution. Keep the matching deliberately
# conservative: LGPL must not be rejected merely because it contains the
# substring "GPL".
DENIED_LICENSE_MARKERS = (
    "agpl",
    "affero",
    "sspl",
    "server side public license",
    "business source license",
    "commons clause",
    "non-commercial",
    "proprietary",
)
GPL_PATTERN = re.compile(r"(?<![a-z])gpl(?:[-\s]?v?\d|\b)", re.IGNORECASE)


def fail(message: str) -> None:
    raise SystemExit(f"[python-dependency-audit] {message}")


def run(command: list[str], *, stdout_path: Path | None = None) -> None:
    if stdout_path is None:
        subprocess.run(command, cwd=ROOT, check=True)
        return
    with stdout_path.open("w", encoding="utf-8") as handle:
        subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            stdout=handle,
            text=True,
        )


def requires_license_review(license_name: str) -> bool:
    """Return True for clearly review-required license declarations.

    `pip-licenses` may emit SPDX-like expressions or human-readable classifier
    names. LGPL is intentionally excluded from the bare-GPL matcher so strings
    such as ``LGPL-3.0-only`` do not become false hard failures. A dependency
    with ambiguous or unknown metadata remains visible in the emitted artifact.
    """

    lowered = license_name.strip().lower()
    if any(marker in lowered for marker in DENIED_LICENSE_MARKERS):
        return True

    # Do not let "LGPL" satisfy the GPL pattern. AGPL is already handled above.
    without_lgpl = re.sub(r"\blgpl(?:[-\s]?v?\d(?:\.\d+)?)?\b", "", lowered)
    return bool(GPL_PATTERN.search(without_lgpl))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "artifacts" / "python-dependencies",
    )
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Broken/incompatible installed distributions are a release blocker even
    # when the vulnerability database itself is clean.
    run([sys.executable, "-m", "pip", "check"])

    audit_json = output_dir / "pip-audit.json"
    run(
        [
            "pip-audit",
            "--strict",
            "--progress-spinner",
            "off",
            "--format",
            "json",
            "--output",
            str(audit_json),
        ]
    )

    license_json = output_dir / "licenses.json"
    run(["pip-licenses", "--format=json"], stdout_path=license_json)
    try:
        licenses = json.loads(license_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot parse license inventory: {exc}")

    denied: list[str] = []
    unknown: list[str] = []
    for item in licenses:
        name = str(item.get("Name") or item.get("name") or "unknown")
        license_name = str(item.get("License") or item.get("license") or "").strip()
        lowered = license_name.lower()
        if requires_license_review(license_name):
            denied.append(f"{name}: {license_name}")
        if not license_name or lowered in {"unknown", "unknown license", "none"}:
            unknown.append(name)

    if denied:
        fail(
            "dependency licenses requiring explicit policy review: "
            + "; ".join(sorted(denied))
        )

    # Unknown metadata is kept visible in the artifact and stderr but does not
    # become a false hard failure: package metadata is sometimes incomplete.
    if unknown:
        print(
            "[python-dependency-audit] warning: unknown license metadata for "
            + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )

    sbom_json = output_dir / "cyclonedx-sbom.json"
    run(
        [
            "cyclonedx-py",
            "environment",
            "--output-format",
            "JSON",
            "--output-file",
            str(sbom_json),
            "--output-reproducible",
        ]
    )

    print(
        "[python-dependency-audit] OK - vulnerability audit, license inventory, "
        f"and CycloneDX SBOM written to {output_dir}"
    )


if __name__ == "__main__":
    main()
