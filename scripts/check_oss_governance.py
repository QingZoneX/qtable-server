from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    "docs/maintainer-governance.md",
]

for path in REQUIRED:
    if not (ROOT / path).is_file():
        raise SystemExit(f"[oss-governance] missing required file: {path}")

config = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
if "blank_issues_enabled: false" not in config or "/security" not in config:
    raise SystemExit("[oss-governance] issue chooser must disable blank issues and route security privately")

pr = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
for token in ["## Validation", "## Security / permissions", "## Data / migrations / dependencies", "## Release impact"]:
    if token not in pr:
        raise SystemExit(f"[oss-governance] PR template missing section: {token}")

owners = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
if "* @boychina" not in owners or "/app/api/" not in owners or "/.github/workflows/" not in owners:
    raise SystemExit("[oss-governance] CODEOWNERS does not cover default and high-risk backend surfaces")

dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
for ecosystem in ["pip", "github-actions", "docker"]:
    if f"package-ecosystem: {ecosystem}" not in dependabot:
        raise SystemExit(f"[oss-governance] dependabot missing ecosystem: {ecosystem}")

governance = (ROOT / "docs/maintainer-governance.md").read_text(encoding="utf-8")
for token in ["direct pushes are disabled", "force-push", "required status check", "CODEOWNERS", "runner_id=0", "#186"]:
    if token not in governance:
        raise SystemExit(f"[oss-governance] governance doc missing policy token: {token}")

print("[oss-governance] OK")
