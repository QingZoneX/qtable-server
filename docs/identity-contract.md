# QingZone Identity Contract v1

QTable is the first-phase **Identity Authority** for the QingZone product family.
QNote, QNoteServer, and QNoteClipper consume QTable identity; they do not create a
second production identity domain.

This document defines the v1 cross-product contract. It intentionally separates
identity from workspace/business authorization: a valid token identifies the
user, while each service still checks current workspace/table/row permissions.

## 1. Roles

| Component | Identity role | Production behavior |
| --- | --- | --- |
| QTable | Authorization Server / Identity Authority | Authenticates users and issues OAuth access/refresh sessions |
| QNote | OAuth public client | Uses Authorization Code + S256 PKCE; never stores a client secret |
| QNoteClipper | Browser-extension OAuth public client | Uses Authorization Code + S256 PKCE and an exact extension redirect URI |
| QNoteServer | Resource server / token consumer | Validates QTable access tokens and synchronizes the local user projection |
| QTable APIs | Resource server | Validates QTable access tokens and current business permissions |

QNoteServer's standalone login is a development-only mode. Production must use
the QTable-issued identity contract.

## 2. Access-token claims

QTable access JWTs carry the following stable contract:

| Claim | Meaning |
| --- | --- |
| `iss` | Identity authority. v1 value: `qtable` |
| `aud` | QingZone resource audience. v1 value: `qingzone` |
| `sub` | Stable string representation of the QTable user id |
| `user_id` | Numeric QTable user id kept for current service compatibility |
| `email` | Current account email when available |
| `name` | Current display name when available |
| `iat` | Token issue time |
| `exp` | Token expiry time |
| `token_type` | `access` or `refresh`; consumers must enforce the expected type |
| `scope` | Space-delimited OAuth scopes |
| `sid` | Logical login/session id, stable across OAuth refresh-token rotation |
| `jti` | Unique id for this individual JWT |
| `client_id` | OAuth client id for OAuth-issued access tokens |

Workspace role, table permission, and row visibility are **not** long-lived token
claims. Services resolve those permissions from current business state.

## 3. Validation contract

Every production resource server must:

1. verify the JWT signature;
2. reject an unexpected `iss`;
3. reject a token whose `aud` does not include `qingzone`;
4. require the expected `token_type`;
5. require a valid `exp` and consistent `sub` / `user_id`;
6. check current application authorization after identity validation.

`JWT_ACCEPT_LEGACY_TOKENS` exists only for migration from older QTable tokens
that were missing some contract claims. It never permits a token with an
explicitly wrong issuer, audience, or subject. New production deployments should
set it to `false` after old sessions have expired.

## 4. OAuth public-client flow

QNote and QNoteClipper are public clients and do not ship a long-lived client
secret.

The required flow is:

1. generate a high-entropy `state`;
2. generate an RFC 7636 verifier;
3. send its SHA-256 challenge with `code_challenge_method=S256`;
4. use a redirect URI registered exactly for that client;
5. validate the returned `state`;
6. exchange the one-time code plus verifier at `POST /oauth/token`;
7. call `/oauth/me` or the target resource server to prove the access token is
   accepted;
8. rotate the refresh token on every successful refresh.

Plain PKCE is disabled by default and must remain disabled in production.
Wildcard redirect URIs are not accepted.

Loopback desktop redirects may vary only by port when the registered URI and
runtime URI otherwise match the same loopback host/path/query contract.

## 5. Session and logout contract

An OAuth login creates one logical `sid`.

- Access-token refresh creates a new `jti` but preserves the same `sid`.
- Refresh tokens are one-time-use and rotate.
- `POST /oauth/revoke` idempotently revokes a supplied refresh token for its
  public client.
- `POST /oauth/logout` with `all_sessions=false` revokes refresh capability
  for the current `sid`.
- `POST /oauth/logout` with `all_sessions=true` revokes all active OAuth
  refresh sessions for the authenticated user.

In v1, already-issued stateless access JWTs remain usable until their short
expiry. Logout/revoke prevents future refresh. Clients must always clear their
local access/refresh credentials on logout, even if the revoke request fails.

A future centralized session/denylist service can provide immediate access-token
revocation without changing the `sid` contract.

## 6. `GET /oauth/me`

`/oauth/me` is the stable identity/session inspection endpoint. It returns
non-secret metadata only:

- id / sub;
- name / email / avatar;
- issuer / audience;
- scopes / token type;
- session id / jti / client id;
- issued-at / expires-at;
- authentication source.

It never returns bearer tokens, refresh tokens, signing keys, passwords, or
workspace permission snapshots.

## 7. QNoteServer production mode

QNoteServer must run with:

```ini
AUTH_MODE=shared_qtable
JWT_ISSUER=qtable
JWT_AUDIENCE=qingzone
IDENTITY_SYNC_ON_AUTH=true
```

During the v1 HS256 phase, QTable and QNoteServer use the same signing secret.
QNoteServer is a verifier/consumer only in production; `/auth/register`,
`/auth/login`, and local refresh issuance are disabled outside explicit
standalone development mode.

## 8. Key rotation / JWKS direction

HS256 shared-secret verification is an interim compatibility stage. The identity
contract is designed to migrate without changing application identity semantics:

1. QTable moves to asymmetric signing;
2. access tokens gain a `kid` header;
3. QTable publishes a JWKS endpoint containing active verification keys;
4. resource servers cache JWKS with bounded refresh;
5. rotation overlaps old/new public keys for at least the maximum access-token
   lifetime;
6. signing private keys remain only at the Identity Authority.

Consumers should depend on `iss/aud/sub/sid/jti/token_type/scope`, not on the
specific signing algorithm, so the HS256 → asymmetric/JWKS migration remains a
verification-layer change.

## 9. Operational rules

- Never log authorization codes, PKCE verifiers, access tokens, refresh tokens,
  signing secrets, or full Authorization headers.
- Refresh failures caused by confirmed invalid/revoked credentials return the
  client to login; transient network failures should not destroy a still-valid
  refresh token.
- Redirect URI and OAuth client configuration are deployment configuration, not
  user-provided arbitrary destinations.
- Identity validation never replaces workspace/table/row permission checks.
