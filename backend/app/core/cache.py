import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import TypeVar

import redis.asyncio as redis


logger = logging.getLogger(__name__)
T = TypeVar("T")


class CacheClient:
    def __init__(self, client: redis.Redis | None, ttl: int) -> None:
        self._client = client
        self._ttl = ttl
        self._memory_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._memory_max_entries = 512
        self._memory_dataset_index: dict[str, set[str]] = {}
        self._memory_dataset_versions: dict[str, int] = {}

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

    async def set(self, key: str, value: bytes, *, dataset_id: str | None = None) -> None:
        if self._client:
            await self._client.setex(key, self._ttl, value)
            if dataset_id is not None:
                index_key = tile_dataset_index_key(dataset_id)
                await self._client.sadd(index_key, key)
                await self._client.expire(index_key, self._ttl)

        self._evict_expired_memory_items()
        self._memory_cache[key] = (time.monotonic() + self._ttl, value)
        self._memory_cache.move_to_end(key)
        if dataset_id is not None:
            self._memory_dataset_index.setdefault(dataset_id, set()).add(key)
        while len(self._memory_cache) > self._memory_max_entries:
            evicted_key, _ = self._memory_cache.popitem(last=False)
            self._remove_memory_index_key(evicted_key)

    async def get_dataset_version(self, dataset_id: str) -> str:
        if self._client:
            value = await self._client.get(tile_dataset_version_key(dataset_id))
            if value is not None:
                return value.decode("utf-8") if isinstance(value, bytes) else str(value)
        return str(self._memory_dataset_versions.get(dataset_id, 0))

    async def invalidate_dataset_tiles(self, dataset_id: str) -> dict[str, int | str]:
        deleted = 0
        if self._client:
            index_key = tile_dataset_index_key(dataset_id)
            members = await self._client.smembers(index_key)
            redis_keys = [
                member.decode("utf-8") if isinstance(member, bytes) else str(member)
                for member in members
            ]
            if redis_keys:
                deleted += int(await self._client.delete(*redis_keys))
            await self._client.delete(index_key)
            version = int(await self._client.incr(tile_dataset_version_key(dataset_id)))
        else:
            version = self._memory_dataset_versions.get(dataset_id, 0) + 1

        memory_keys = self._memory_dataset_index.pop(dataset_id, set())
        for key in memory_keys:
            if self._memory_cache.pop(key, None) is not None:
                deleted += 1
        self._memory_dataset_versions[dataset_id] = version
        logger.info(
            "tile_cache_dataset_invalidated dataset_id=%s deleted=%d version=%d",
            dataset_id,
            deleted,
            version,
        )
        return {"dataset_id": dataset_id, "deleted": deleted, "version": str(version)}

    def invalidate_dataset_tiles_blocking(self, dataset_id: str) -> dict[str, int | str]:
        return asyncio.run(self.invalidate_dataset_tiles(dataset_id))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        self._memory_cache.clear()
        self._memory_dataset_index.clear()
        self._memory_dataset_versions.clear()

    def _remove_memory_index_key(self, key: str) -> None:
        empty_dataset_ids: list[str] = []
        for dataset_id, keys in self._memory_dataset_index.items():
            keys.discard(key)
            if not keys:
                empty_dataset_ids.append(dataset_id)
        for dataset_id in empty_dataset_ids:
            self._memory_dataset_index.pop(dataset_id, None)


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


def tile_dataset_index_key(dataset_id: str) -> str:
    return f"tile-index:{_dataset_digest(dataset_id)}"


def tile_dataset_version_key(dataset_id: str) -> str:
    return f"tile-version:{_dataset_digest(dataset_id)}"


def _dataset_digest(dataset_id: str) -> str:
    return hashlib.sha1(dataset_id.encode("utf-8")).hexdigest()[:20]


class InFlightRequestCoalescer:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> tuple[T, str]:
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[key] = task
                state = "leader"
            else:
                state = "follower"
        try:
            result = await asyncio.shield(task)
        finally:
            if state == "leader":
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        return result, state
