import asyncio

from app.core.cache import CacheClient


def test_cache_client_falls_back_to_memory_when_redis_is_unavailable() -> None:
    cache = CacheClient(client=None, ttl=60)

    async def exercise() -> tuple[bytes | None, bytes | None]:
        before = await cache.get("tile:abc")
        await cache.set("tile:abc", b"payload")
        after = await cache.get("tile:abc")
        return before, after

    before, after = asyncio.run(exercise())

    assert before is None
    assert after == b"payload"
