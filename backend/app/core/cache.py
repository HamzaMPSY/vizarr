import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

import redis.asyncio as redis


logger = logging.getLogger(__name__)


class CacheClient:
    def __init__(self, client: redis.Redis | None, ttl: int) -> None:
        self._client = client
        self._ttl = ttl
        self._memory_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._memory_max_entries = 512

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(self._memory_cache)

    def _evict_expired_memory_items(self) -> None:
        now = time.monotonic()
        expired_keys = [key for key, (expires_at, _value) in self._memory_cache.items() if expires_at <= now]
        for key in expired_keys:
            self._memory_cache.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        if self._client:
            value = await self._client.get(key)
            if value is not None:
                return value

        self._evict_expired_memory_items()
        entry = self._memory_cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._memory_cache.pop(key, None)
            return None
        self._memory_cache.move_to_end(key)
        return value

    async def set(self, key: str, value: bytes) -> None:
        if self._client:
            await self._client.setex(key, self._ttl, value)

        self._evict_expired_memory_items()
        self._memory_cache[key] = (time.monotonic() + self._ttl, value)
        self._memory_cache.move_to_end(key)
        while len(self._memory_cache) > self._memory_max_entries:
            self._memory_cache.popitem(last=False)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        self._memory_cache.clear()


async def connect_cache(redis_url: str, ttl: int) -> CacheClient:
    try:
        client = redis.from_url(redis_url, decode_responses=False)
        await client.ping()
        logger.info("Redis cache connected at %s", redis_url)
        return CacheClient(client=client, ttl=ttl)
    except Exception as exc:
        logger.warning("Redis unavailable, continuing without cache: %s", exc)
        return CacheClient(client=None, ttl=ttl)


def build_tile_cache_key(parts: dict[str, Any], *, display_range_decimals: int = 3) -> str:
    normalized_parts = normalize_tile_cache_parts(parts, display_range_decimals=display_range_decimals)
    payload = json.dumps(normalized_parts, sort_keys=True).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:20]
    return f"tile:{digest}"


def normalize_tile_cache_parts(parts: dict[str, Any], *, display_range_decimals: int = 3) -> dict[str, Any]:
    normalized = dict(parts)
    for key in ("vmin", "vmax"):
        value = normalized.get(key)
        if isinstance(value, float):
            rounded = round(value, max(display_range_decimals, 0))
            normalized[key] = 0.0 if rounded == 0 else rounded
    return normalized
