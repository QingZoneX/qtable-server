# Maintainer governance

QTable uses pull-request based development for public contributions. The repository files in `.github/` define intake, ownership and dependency-update automation; GitHub branch/ruleset enforcement is enabled when the repository plan/visibility permits it.

## Required public-main protection

Immediately after the repository becomes public (or an upgraded private plan permits equivalent rules), configure `main` so that:

- direct pushes are disabled; changes land through pull requests;
- force-push and branch deletion are disabled;
- required status check is the real backend CI job from `.github/workflows/backend-ci.yml` (`Backend CI` / `test`), after verifying the exact check name exposed by GitHub on a successful run;
- Docker portability remains a release signal and may be made required after its exact check name is confirmed on the public repository;
- at least one approving maintainer review is required for external contributions;
- stale approvals are dismissed when new commits materially change the reviewed diff;
- CODEOWNERS review is required for security, API, migration, workflow and deployment surfaces when the GitHub plan supports it;
- administrators should follow the same PR and required-check path except for documented emergency recovery.

Do not configure a required check name by guesswork. First obtain a successful real run on the exact repository visibility/runner setup, then bind the ruleset to the check name GitHub actually reports.

## Merge discipline

A PR should be `behind=0`, mergeable and free of unresolved review threads before merge. Infrastructure-only GitHub Actions failures where no runner is assigned (`runner_id=0`, `steps=[]`) are not green evidence and must not be confused with an executed test failure. Executed test/security failures remain blocking.

## Security reports

Public issues must never contain vulnerability details. `SECURITY.md` is authoritative; the issue chooser redirects reporters to GitHub's private security reporting surface when available.

## Dependency updates

Dependabot opens reviewable dependency PRs. Backend release dependencies must remain reproducible under the lock/constraint policy tracked by #186; automated updates never bypass vulnerability, license, migration, test or release gates.
