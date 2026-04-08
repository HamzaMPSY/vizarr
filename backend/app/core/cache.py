import hashlib
import json
import logging
from typing import Any

import redis.asyncio as redis


logger = logging.getLogger(__name__)


class CacheClient:
    def __init__(self, client: redis.Redis | None, ttl: int) -> None:
        self._client = client
        self._ttl = ttl

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def get(self, key: str) -> bytes | None:
        if not self._client:
            return None
        return await self._client.get(key)

    async def set(self, key: str, value: bytes) -> None:
        if not self._client:
            return
        await self._client.setex(key, self._ttl, value)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


async def connect_cache(redis_url: str, ttl: int) -> CacheClient:
    try:
        client = redis.from_url(redis_url, decode_responses=False)
        await client.ping()
        logger.info("Redis cache connected at %s", redis_url)
        return CacheClient(client=client, ttl=ttl)
    except Exception as exc:
        logger.warning("Redis unavailable, continuing without cache: %s", exc)
        return CacheClient(client=None, ttl=ttl)


def build_tile_cache_key(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:20]
    return f"tile:{digest}"

