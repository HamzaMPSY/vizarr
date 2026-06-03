import asyncio

from app.core.cache import CacheClient
from app.core.cache import build_tile_cache_key
from app.core.cache import normalize_tile_cache_parts


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


def test_tile_cache_key_normalizes_display_range_floats() -> None:
    base = {
        "dataset_id": "dataset",
        "variable": "NDVI",
        "z": 10,
        "x": 512,
        "y": 511,
        "vmin": 0.12344,
        "vmax": 0.98764,
    }
    equivalent = {**base, "vmin": 0.12343, "vmax": 0.98763}
    distinct = {**base, "vmin": 0.1249, "vmax": 0.98764}

    assert normalize_tile_cache_parts(base, display_range_decimals=3)["vmin"] == 0.123
    assert build_tile_cache_key(base, display_range_decimals=3) == build_tile_cache_key(
        equivalent,
        display_range_decimals=3,
    )
    assert build_tile_cache_key(base, display_range_decimals=3) != build_tile_cache_key(
        distinct,
        display_range_decimals=3,
    )
