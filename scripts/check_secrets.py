from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("OpenAI/compatible API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
]

SENSITIVE_FILENAMES = {".env", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def _tracked_files() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"])
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def _scan_text(text: str, location: str, findings: list[str]) -> None:
    for name, pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(f"{location}: {name} ({match.group(0)[:8]}…)")


def scan_current(findings: list[str]) -> None:
    for rel in _tracked_files():
        path = Path(rel)
        if path.name in SENSITIVE_FILENAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
            findings.append(f"{rel}: sensitive filename must not be tracked")
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        _scan_text(text, rel, findings)


def scan_history(findings: list[str]) -> None:
    process = subprocess.Popen(
        ["git", "log", "--all", "-p", "--no-ext-diff", "--text", "--format=commit:%H"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    assert process.stdout is not None
    commit = "unknown"
    line_number = 0
    for line in process.stdout:
        line_number += 1
        if line.startswith("commit:"):
            commit = line.strip().split(":", 1)[1]
            continue
        for name, pattern in PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    f"history {commit[:12]} line {line_number}: {name} ({match.group(0)[:8]}…)"
                )
                if len(findings) >= 50:
                    process.kill()
                    return
    stderr = process.stderr.read() if process.stderr is not None else ""
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"git history scan failed: {stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    findings: list[str] = []
    scan_current(findings)
    if args.history:
        scan_history(findings)
    if findings:
        print("[secret-scan] BLOCKED")
        for finding in findings:
            print(" -", finding)
        print("Rotate any real credential before rewriting/removing it from history.")
        return 1
    scope = "tracked files + Git history" if args.history else "tracked files"
    print(f"[secret-scan] OK: no high-confidence credentials found in {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
