# Security Policy

QTable handles authentication, workspace permissions, row-level permissions, AI credentials and public dashboard sharing. Security reports are treated as private until a fix is available.

## Supported versions

During the Alpha period, security fixes are provided for the latest commit on `main` and the latest tagged Alpha release only.

## Reporting a vulnerability

**Do not open a public GitHub Issue for a vulnerability.**

Use GitHub's **Report a vulnerability / Private vulnerability reporting** feature when it is enabled for this repository. If that feature is unavailable, contact the QingZoneX repository owners through the GitHub organization profile and request a private reporting channel.

Please include:

- affected version or commit;
- impact and attack preconditions;
- minimal reproduction steps;
- whether authentication is required;
- relevant logs with credentials and personal data removed.

## Sensitive areas

Reports involving these areas are especially important:

- OAuth2 / PKCE and token validation;
- cross-workspace access;
- row-level permission bypass;
- public dashboard information disclosure;
- AI prompt/context leaking hidden rows;
- API-key or encryption-key exposure;
- Source Inbox / connector authorization;
- ChangeSet / undo authorization;
- SSRF or arbitrary external requests.

## Secrets

Never commit real credentials. CI runs a high-confidence secret scanner against tracked content and Git history. If a real secret was ever committed, deleting the file is not sufficient: rotate/revoke the credential and rewrite history before making the repository public.

## Disclosure

We ask reporters to allow reasonable time for a fix before public disclosure. Maintainers will document security-relevant release notes without exposing active exploit details prematurely.
