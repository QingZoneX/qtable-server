"""
Task Management - Redis 缓存方案

缓存策略:
1. 时间敏感数据: TTL=30s (当前时间相关)
2. Schema 数据: TTL=300s (表结构变化不频繁)
3. 分析结果: TTL=120s (统计分析结果)
4. 用户/工作空间信息: TTL=600s (基础环境信息)

缓存键命名规范:
    qtable:tm:{workspace_id}:{tool_name}:{cache_key}
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# ============ 缓存 TTL 策略 ============

class CacheTTL:
    """预定义的缓存 TTL 值（秒）"""
    DATETIME = 30          # 当前时间
    SCHEMA = 300           # 表结构
    PROGRESS = 120         # 进度统计
    WORKLOAD = 120         # 工作负载
    OVERDUE = 60           # 延期任务
    BLOCKING = 60          # 阻断任务
    PREDICTION = 180       # 预测结果
    ENVIRONMENT = 600      # 环境信息
    USER = 300             # 用户信息
    WORKSPACE = 600        # 工作空间信息


class CacheStrategy:
    """缓存策略枚举"""
    NONE = "none"
    SIMPLE = "simple"
    STALE_WHILE_REVALIDATE = "stale_while_revalidate"


# ============ Task Management Cache ============

class TaskManagementCache:
    """任务管理工具专用 Redis 缓存层"""

    KEY_PREFIX = "qtable:tm"

    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client: redis.Redis | None = client

    async def _get_client(self) -> redis.Redis | None:
        if self._client is not None:
            return self._client
        try:
            self._client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await self._client.ping()
            return self._client
        except Exception:
            logger.debug("Redis 不可用，跳过缓存")
            return None

    def _build_key(
        self,
        tool_name: str,
        workspace_id: str | None,
        *parts: str,
    ) -> str:
        """构建缓存键"""
        ws = workspace_id or "_global"
        suffix = ":".join(parts) if parts else ""
        return f"{self.KEY_PREFIX}:{ws}:{tool_name}:{suffix}"

    def _hash_params(self, params: dict[str, Any]) -> str:
        """对参数进行哈希，用于缓存键"""
        raw = json.dumps(params, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    async def get(self, key: str) -> dict[str, Any] | None:
        """读取缓存"""
        client = await self._get_client()
        if client is None:
            return None
        try:
            value = await client.get(key)
            if value is None:
                return None
            data = json.loads(value)
            # 检查 stale_while_revalidate
            cached_at = data.get("_cached_at")
            if cached_at:
                cache_time = datetime.fromisoformat(cached_at)
                age = (datetime.now(timezone.utc) - cache_time).total_seconds()
                ttl = data.get("_ttl", 60)
                if age > ttl:
                    # 数据过期，但仍返回（后台刷新由调用方处理）
                    data["_stale"] = True
            return data
        except Exception:
            return None

    async def set(
        self,
        key: str,
        data: dict[str, Any],
        ttl: int = 60,
        strategy: str = CacheStrategy.SIMPLE,
    ) -> None:
        """写入缓存"""
        client = await self._get_client()
        if client is None:
            return
        try:
            payload = {
                **data,
                "_cached_at": datetime.now(timezone.utc).isoformat(),
                "_ttl": ttl,
                "_strategy": strategy,
            }
            actual_ttl = ttl
            if strategy == CacheStrategy.STALE_WHILE_REVALIDATE:
                actual_ttl = ttl * 3  # 保留 3 倍 TTL，允许 stale 读取
            await client.setex(key, actual_ttl, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            return

    async def delete_pattern(self, pattern: str) -> int:
        """按模式删除缓存"""
        client = await self._get_client()
        if client is None:
            return 0
        deleted = 0
        cursor = 0
        try:
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    deleted += await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass
        return deleted

    async def invalidate_workspace(self, workspace_id: str) -> int:
        """使工作空间的所有缓存失效"""
        pattern = f"{self.KEY_PREFIX}:{workspace_id}:*"
        return await self.delete_pattern(pattern)

    async def invalidate_tool(
        self,
        workspace_id: str | None,
        tool_name: str,
    ) -> int:
        """使特定工具的缓存失效"""
        ws = workspace_id or "_global"
        pattern = f"{self.KEY_PREFIX}:{ws}:{tool_name}:*"
        return await self.delete_pattern(pattern)


# ============ 装饰器风格的缓存工具 ============

async def cached_tool_call(
    cache: TaskManagementCache,
    tool_name: str,
    workspace_id: str | None,
    params: dict[str, Any],
    ttl: int = 60,
    strategy: str = CacheStrategy.SIMPLE,
    fetcher=None,
) -> dict[str, Any]:
    """
    带缓存的工具调用包装器。

    用法:
        result = await cached_tool_call(
            cache=cache,
            tool_name="get_overdue_tasks",
            workspace_id=ws_id,
            params={"table_id": "tbl_xxx"},
            ttl=CacheTTL.OVERDUE,
            fetcher=lambda: actual_call(db, table_id),
        )
    """
    if strategy == CacheStrategy.NONE:
        return await fetcher()

    params_hash = cache._hash_params(params)
    key = cache._build_key(tool_name, workspace_id, params_hash)

    # 尝试读取缓存
    cached = await cache.get(key)
    if cached and not cached.get("_stale"):
        # 缓存命中，移除内部标记
        result = {k: v for k, v in cached.items() if not k.startswith("_")}
        result["_cache_hit"] = True
        return result

    # 缓存未命中或过期，执行实际调用
    result = await fetcher()
    result["_cache_hit"] = False

    # 写入缓存
    if result.get("success", True):
        await cache.set(key, result, ttl=ttl, strategy=strategy)

    return result


# ============ Schema 缓存 ============

async def get_cached_table_schema(
    cache: TaskManagementCache,
    workspace_id: str | None,
    table_id: str,
    fetcher,
    ttl: int = CacheTTL.SCHEMA,
) -> dict[str, Any]:
    """带缓存的表 Schema 查询"""
    return await cached_tool_call(
        cache=cache,
        tool_name="describe_table_schema",
        workspace_id=workspace_id,
        params={"table_id": table_id},
        ttl=ttl,
        strategy=CacheStrategy.STALE_WHILE_REVALIDATE,
        fetcher=fetcher,
    )


# ============ 全局缓存实例 ============

_task_management_cache: TaskManagementCache | None = None


def get_task_management_cache() -> TaskManagementCache:
    global _task_management_cache
    if _task_management_cache is None:
        _task_management_cache = TaskManagementCache()
    return _task_management_cache
