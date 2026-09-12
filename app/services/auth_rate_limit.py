from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


@dataclass(frozen=True)
class RateLimitRule:
    namespace: str
    subject: str
    limit: int
    window_seconds: int


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limit exceeded")
        self.retry_after = max(int(retry_after), 1)


class RateLimitBackendUnavailable(Exception):
    pass


def _is_production() -> bool:
    return (settings.APP_ENV or "").strip().lower() in {"prod", "production"}


def _hashed_subject(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def forgot_password_rate_limit_rules(email: str) -> list[RateLimitRule]:
    # Canonical QTableUI currently proxies /auth without a trusted-client-IP
    # contract. Do not treat the reverse proxy socket address as a user IP or
    # trust spoofable forwarding headers. Per-account throttling is shared in
    # Redis and prevents reset-email abuse without creating a proxy-wide DoS.
    return [
        RateLimitRule(
            namespace="forgot-email",
            subject=email.strip().lower(),
            limit=settings.PASSWORD_RESET_FORGOT_EMAIL_LIMIT,
            window_seconds=settings.PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
        )
    ]


def reset_password_rate_limit_rules(token_hash: str) -> list[RateLimitRule]:
    return [
        RateLimitRule(
            namespace="reset-token",
            subject=token_hash,
            limit=settings.PASSWORD_RESET_SUBMIT_TOKEN_LIMIT,
            window_seconds=settings.PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
        )
    ]


def _redis_key(rule: RateLimitRule) -> str:
    return f"qtable:auth-rate:{rule.namespace}:{_hashed_subject(rule.subject)}"


async def _enforce_redis(rules: list[RateLimitRule]) -> None:
    client = Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    try:
        for rule in rules:
            result = await client.eval(
                _RATE_LIMIT_SCRIPT,
                1,
                _redis_key(rule),
                int(rule.window_seconds),
            )
            count = int(result[0])
            ttl = max(int(result[1]), 1)
            if count > rule.limit:
                raise RateLimitExceeded(ttl)
    finally:
        # Connection cleanup must never mask an already-established allow/deny
        # decision. Runtime Redis errors from the actual limiter remain visible.
        try:
            await client.aclose()
        except RedisError:
            pass


_memory_lock = asyncio.Lock()
_memory_counters: dict[str, tuple[int, float]] = {}


async def _enforce_memory(rules: list[RateLimitRule]) -> None:
    # Development/test fallback only. Production requires the shared Redis
    # backend so limits cannot be bypassed by spreading requests across workers.
    now = time.monotonic()
    async with _memory_lock:
        if len(_memory_counters) > 10_000:
            expired = [key for key, (_, deadline) in _memory_counters.items() if deadline <= now]
            for key in expired:
                _memory_counters.pop(key, None)
        for rule in rules:
            key = _redis_key(rule)
            count, deadline = _memory_counters.get(
                key,
                (0, now + rule.window_seconds),
            )
            if deadline <= now:
                count = 0
                deadline = now + rule.window_seconds
            count += 1
            _memory_counters[key] = (count, deadline)
            if count > rule.limit:
                raise RateLimitExceeded(max(int(deadline - now), 1))


async def enforce_password_reset_rate_limits(rules: list[RateLimitRule]) -> None:
    if not settings.PASSWORD_RESET_RATE_LIMIT_ENABLED:
        if _is_production():
            raise RateLimitBackendUnavailable()
        return
    try:
        await _enforce_redis(rules)
    except RateLimitExceeded:
        raise
    except RedisError as exc:
        if _is_production():
            logger.error("Password reset rate-limit backend is unavailable")
            raise RateLimitBackendUnavailable() from exc
        logger.warning("Redis unavailable for password reset rate limit; using process-local development fallback")
        await _enforce_memory(rules)


async def reset_in_memory_rate_limits_for_tests() -> None:
    async with _memory_lock:
        _memory_counters.clear()


__all__ = [
    "RateLimitBackendUnavailable",
    "RateLimitExceeded",
    "RateLimitRule",
    "enforce_password_reset_rate_limits",
    "forgot_password_rate_limit_rules",
    "reset_password_rate_limit_rules",
]
