import asyncio

from app.core.cache import CacheClient
from app.core.cache import InFlightRequestCoalescer
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


def test_cache_client_indexes_and_invalidates_dataset_tiles() -> None:
    cache = CacheClient(client=None, ttl=60)

    async def exercise() -> tuple[bytes | None, bytes | None, str, str, dict[str, int | str]]:
        before_version = await cache.get_dataset_version("dataset-1")
        await cache.set("tile:dataset-1:a", b"first", dataset_id="dataset-1")
        await cache.set("tile:dataset-1:b", b"second", dataset_id="dataset-1")
        await cache.set("tile:dataset-2:a", b"other", dataset_id="dataset-2")
        invalidation = await cache.invalidate_dataset_tiles("dataset-1")
        after_version = await cache.get_dataset_version("dataset-1")
        return (
            await cache.get("tile:dataset-1:a"),
            await cache.get("tile:dataset-2:a"),
            before_version,
            after_version,
            invalidation,
        )

    dataset_value, other_value, before_version, after_version, invalidation = asyncio.run(exercise())

    assert dataset_value is None
    assert other_value == b"other"
    assert before_version == "0"
    assert after_version == "1"
    assert invalidation == {"dataset_id": "dataset-1", "deleted": 2, "version": "1"}


def test_inflight_request_coalescer_shares_successful_result() -> None:
    coalescer = InFlightRequestCoalescer()
    calls = 0

    async def exercise() -> list[tuple[bytes, str]]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory() -> bytes:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return b"tile"

        first = asyncio.create_task(coalescer.run("tile:key", factory))
        await started.wait()
        second = asyncio.create_task(coalescer.run("tile:key", factory))
        await asyncio.sleep(0)
        release.set()
        return list(await asyncio.gather(first, second))

    results = asyncio.run(exercise())

    assert calls == 1
    assert sorted(status for _payload, status in results) == ["follower", "leader"]
    assert [payload for payload, _status in results] == [b"tile", b"tile"]


def test_inflight_request_coalescer_shares_failures() -> None:
    coalescer = InFlightRequestCoalescer()
    calls = 0

    async def exercise() -> list[str]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory() -> bytes:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            raise RuntimeError("render failed")

        async def capture() -> str:
            try:
                await coalescer.run("tile:key", factory)
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        first = asyncio.create_task(capture())
        await started.wait()
        second = asyncio.create_task(capture())
        await asyncio.sleep(0)
        release.set()
        return list(await asyncio.gather(first, second))

    results = asyncio.run(exercise())

    assert calls == 1
    assert results == ["render failed", "render failed"]
