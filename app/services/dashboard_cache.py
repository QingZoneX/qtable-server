from __future__ import annotations

import hashlib
import json
import logging
import time
import fnmatch
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class DashboardCacheTTL:
    WIDGET_DATA = 300


class DashboardCache:
    KEY_PREFIX = "qtable:dashboard"

    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client: redis.Redis | None = client
        self._local: dict[str, tuple[float, str]] = {}
        self._redis_disabled_until: float = 0.0

    def _prune_local(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._local.items() if exp <= now]
        for k in expired:
            self._local.pop(k, None)

    async def _get_client(self) -> redis.Redis | None:
        if self._client is not None:
            return self._client
        now = time.time()
        if now < self._redis_disabled_until:
            return None
        try:
            self._client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await asyncio.wait_for(self._client.ping(), timeout=0.6)
            return self._client
        except Exception:
            self._client = None
            self._redis_disabled_until = now + 30.0
            logger.debug("Redis 不可用，跳过 Dashboard 缓存")
            return None

    def _hash_params(self, params: dict[str, Any]) -> str:
        raw = json.dumps(params, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def widget_key(self, table_id: str, widget_id: str, params: dict[str, Any]) -> str:
        safe_table_id = table_id or "_none"
        return f"{self.KEY_PREFIX}:table:{safe_table_id}:widget:{widget_id}:{self._hash_params(params)}"

    async def get(self, key: str) -> dict[str, Any] | None:
        client = await self._get_client()
        if client is None:
            now = time.time()
            self._prune_local(now)
            item = self._local.get(key)
            if not item:
                return None
            exp, payload = item
            if exp <= now:
                self._local.pop(key, None)
                return None
            try:
                return json.loads(payload) if payload else None
            except Exception:
                return None
        try:
            payload = await client.get(key)
            return json.loads(payload) if payload else None
        except Exception:
            self._client = None
            self._redis_disabled_until = time.time() + 30.0
            return None

    async def set(self, key: str, data: dict[str, Any], ttl: int) -> None:
        client = await self._get_client()
        try:
            wrapped = {
                **data,
                "_cached_at": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps(wrapped, ensure_ascii=False, default=str)
            if client is None:
                now = time.time()
                self._prune_local(now)
                self._local[key] = (now + max(1, int(ttl)), payload)
                return
            await client.setex(key, ttl, payload)
        except Exception:
            self._client = None
            self._redis_disabled_until = time.time() + 30.0
            now = time.time()
            self._prune_local(now)
            self._local[key] = (now + max(1, int(ttl)), payload if "payload" in locals() else json.dumps(data, ensure_ascii=False, default=str))
            return

    async def delete_pattern(self, pattern: str) -> int:
        client = await self._get_client()
        if client is None:
            now = time.time()
            self._prune_local(now)
            keys = [k for k in self._local.keys() if fnmatch.fnmatch(k, pattern)]
            for k in keys:
                self._local.pop(k, None)
            return len(keys)
        deleted = 0
        cursor = 0
        try:
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    deleted += await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            return deleted
        return deleted

    async def invalidate_widget(self, widget_id: str) -> int:
        return await self.delete_pattern(f"{self.KEY_PREFIX}:table:*:widget:{widget_id}:*")

    async def invalidate_table(self, table_id: str) -> int:
        safe_table_id = table_id or "_none"
        return await self.delete_pattern(f"{self.KEY_PREFIX}:table:{safe_table_id}:widget:*")


_dashboard_cache: DashboardCache | None = None


def get_dashboard_cache() -> DashboardCache:
    global _dashboard_cache
    if _dashboard_cache is None:
        _dashboard_cache = DashboardCache()
    return _dashboard_cache
