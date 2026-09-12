"""PKCE and OAuth redirect policy helpers."""
import base64
import hashlib
import re
import secrets
from typing import List
from urllib.parse import urlparse


_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_S256_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def generate_code_verifier(length: int = 128) -> str:
    """Generate an RFC 7636 verifier (43-128 unreserved characters)."""
    normalized = max(43, min(128, int(length)))
    # token_urlsafe contains only RFC 3986 unreserved characters after
    # stripping padding; loop until the requested minimum is satisfied.
    value = ""
    while len(value) < normalized:
        value += secrets.token_urlsafe(normalized)
    return value[:normalized]


def is_valid_code_verifier(code_verifier: str) -> bool:
    return bool(_PKCE_VERIFIER.fullmatch(str(code_verifier or "")))


def generate_code_challenge(code_verifier: str, method: str = "S256") -> str:
    if not is_valid_code_verifier(code_verifier):
        raise ValueError("Invalid PKCE code verifier")
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    if method == "plain":
        return code_verifier
    raise ValueError(f"Unsupported code challenge method: {method}")


def is_valid_code_challenge(code_challenge: str, method: str = "S256") -> bool:
    value = str(code_challenge or "")
    if method == "S256":
        return bool(_S256_CHALLENGE.fullmatch(value))
    if method == "plain":
        return is_valid_code_verifier(value)
    return False


def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    if not is_valid_code_verifier(code_verifier):
        return False
    if not is_valid_code_challenge(code_challenge, method):
        return False
    try:
        generated = generate_code_challenge(code_verifier, method)
    except ValueError:
        return False
    return secrets.compare_digest(generated, code_challenge)


def is_redirect_uri_allowed(redirect_uri: str, allowed_uris: List[str]) -> bool:
    """Strict redirect matching with RFC-style loopback dynamic ports.

    Non-loopback redirects (custom schemes, HTTPS, browser-extension URLs)
    require an exact string match. Loopback HTTP(S) registrations may vary only
    by port; scheme, host, path and query must remain identical. Wildcards are
    intentionally not supported.
    """
    if redirect_uri in allowed_uris:
        return True
    if "*" in redirect_uri:
        return False

    try:
        requested = urlparse(redirect_uri)
    except Exception:
        return False
    if requested.fragment or requested.username or requested.password:
        return False

    requested_host = (requested.hostname or "").lower()
    if requested_host not in _LOOPBACK_HOSTS:
        return False

    for allowed in allowed_uris:
        if "*" in allowed:
            continue
        try:
            registered = urlparse(allowed)
        except Exception:
            continue
        registered_host = (registered.hostname or "").lower()
        if registered_host != requested_host:
            continue
        if registered_host not in _LOOPBACK_HOSTS:
            continue
        if (
            requested.scheme == registered.scheme
            and requested.path == registered.path
            and requested.query == registered.query
            and requested.params == registered.params
        ):
            return True
    return False
