## Summary

Describe what changes and why.

## Validation

- [ ] I ran the relevant tests/contracts locally, or documented why an environment-dependent gate could not execute.
- [ ] I did not weaken or skip an existing release/security assertion to make the change pass.
- [ ] New behavior has regression coverage where practical.

## Security / permissions

- [ ] Authentication, workspace/table/row permissions, public sharing, attachments, AI context/confirm/apply, and audit/ChangeSet semantics are unchanged, or the impact is explained below.
- [ ] No credential, token, private business data, personal path, or production secret is included.

## Data / migrations / dependencies

- [ ] Schema or persistent-data changes include a reviewed migration and upgrade/rollback considerations, or this PR has no persistent-data change.
- [ ] Dependency changes are intentional, reproducible, and include security/license impact, or this PR has no dependency change.

## UI / API evidence

- [ ] Screenshots/traces are attached for visible UI changes when useful, or not applicable.
- [ ] API/GraphQL compatibility implications are documented, or not applicable.

## Release impact

Describe any effect on self-hosting, configuration, release notes, or `v0.1.2-alpha` readiness.
