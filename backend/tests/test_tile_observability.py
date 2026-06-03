from app.core.tile_observability import TileRequestMetrics
from app.core.tile_observability import TileBudgetExceeded
from app.core.tile_observability import TileComputeBudget
from app.core.tile_observability import activate_tile_metrics
from app.core.tile_observability import build_tile_debug_headers
from app.core.tile_observability import enforce_tile_compute_budget
from app.core.tile_observability import record_object_read
from app.core.tile_observability import record_zarr_chunk_read
from app.core.tile_observability import record_zarr_shard_index_read


def test_tile_metrics_are_noop_without_active_context() -> None:
    record_object_read(bytes_read=128, byte_range=True)
    record_zarr_chunk_read()
    record_zarr_shard_index_read()


def test_tile_metrics_snapshot_and_headers() -> None:
    metrics = TileRequestMetrics()
    with activate_tile_metrics(metrics):
        with metrics.time_block("planner"):
            pass
        record_object_read(bytes_read=128, byte_range=True)
        record_zarr_chunk_read()
        record_zarr_shard_index_read()

    metrics.finish()
    snapshot = metrics.snapshot()
    assert snapshot["object_get_count"] == 1
    assert snapshot["byte_range_get_count"] == 1
    assert snapshot["object_bytes_read"] == 128
    assert snapshot["chunk_reads"] == 1
    assert snapshot["shard_index_reads"] == 1

    headers = build_tile_debug_headers(metrics)
    assert float(headers["X-Tile-Time-Ms"]) >= 0
    assert float(headers["X-Tile-Planner-Ms"]) >= 0
    assert headers["X-Object-Get-Count"] == "1"
    assert headers["X-Object-Byte-Range-Get-Count"] == "1"
    assert headers["X-Object-Bytes-Read"] == "128"
    assert headers["X-Zarr-Chunk-Count"] == "1"
    assert headers["X-Tile-Budget-Status"] == "not_evaluated"


def test_tile_compute_budget_records_allowed_decision() -> None:
    metrics = TileRequestMetrics()
    metrics.record_object_read(bytes_read=128, byte_range=True)
    metrics.record_chunk_read()

    enforce_tile_compute_budget(
        metrics,
        TileComputeBudget(
            max_object_gets=2,
            max_byte_range_gets=2,
            max_object_bytes=256,
            max_zarr_chunks=2,
        ),
    )

    snapshot = metrics.snapshot()
    assert snapshot["budget_status"] == "allowed"
    assert snapshot["budget_reason"] == "within direct tile compute budget"


def test_tile_compute_budget_records_exceeded_decision() -> None:
    metrics = TileRequestMetrics()
    metrics.record_object_read(bytes_read=512, byte_range=True)

    try:
        enforce_tile_compute_budget(metrics, TileComputeBudget(max_object_bytes=256))
        raise AssertionError("budget should have been exceeded")
    except TileBudgetExceeded as exc:
        assert exc.detail() == {
            "error": "direct_tile_compute_budget_exceeded",
            "reason": "object_bytes_read 512 exceeded limit 256",
            "metric": "object_bytes_read",
            "actual": 512,
            "limit": 256,
        }

    snapshot = metrics.snapshot()
    assert snapshot["budget_status"] == "exceeded"
    assert snapshot["budget_metric"] == "object_bytes_read"
    assert snapshot["budget_actual"] == 512
    assert snapshot["budget_limit"] == 256
