from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from threading import Lock
from time import perf_counter
from typing import Iterator


_CURRENT_TILE_METRICS: ContextVar["TileRequestMetrics | None"] = ContextVar(
    "current_tile_metrics",
    default=None,
)


@dataclass
class TileRequestMetrics:
    started_at: float = field(default_factory=perf_counter)
    timings_ms: dict[str, float] = field(default_factory=dict)
    object_get_count: int = 0
    byte_range_get_count: int = 0
    object_bytes_read: int = 0
    shard_index_reads: int = 0
    chunk_reads: int = 0
    budget_status: str = "not_evaluated"
    budget_reason: str = ""
    budget_metric: str = ""
    budget_limit: int | None = None
    budget_actual: int | None = None
    _lock: Lock = field(default_factory=Lock, repr=False)

    @contextmanager
    def time_block(self, name: str) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            self.add_timing(name, (perf_counter() - started) * 1000.0)

    def add_timing(self, name: str, elapsed_ms: float) -> None:
        with self._lock:
            self.timings_ms[name] = self.timings_ms.get(name, 0.0) + elapsed_ms

    def record_object_read(self, *, bytes_read: int, byte_range: bool = False) -> None:
        with self._lock:
            self.object_get_count += 1
            if byte_range:
                self.byte_range_get_count += 1
            self.object_bytes_read += max(int(bytes_read), 0)

    def record_shard_index_read(self) -> None:
        with self._lock:
            self.shard_index_reads += 1

    def record_chunk_read(self) -> None:
        with self._lock:
            self.chunk_reads += 1

    def record_budget_decision(
        self,
        *,
        status: str,
        reason: str,
        metric: str = "",
        limit: int | None = None,
        actual: int | None = None,
    ) -> None:
        with self._lock:
            self.budget_status = status
            self.budget_reason = reason
            self.budget_metric = metric
            self.budget_limit = limit
            self.budget_actual = actual

    def finish(self) -> None:
        with self._lock:
            self.timings_ms["total_request"] = (perf_counter() - self.started_at) * 1000.0

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "timings_ms": {
                    key: round(value, 3)
                    for key, value in sorted(self.timings_ms.items())
                },
                "object_get_count": self.object_get_count,
                "byte_range_get_count": self.byte_range_get_count,
                "object_bytes_read": self.object_bytes_read,
                "shard_index_reads": self.shard_index_reads,
                "chunk_reads": self.chunk_reads,
                "budget_status": self.budget_status,
                "budget_reason": self.budget_reason,
                "budget_metric": self.budget_metric,
                "budget_limit": self.budget_limit,
                "budget_actual": self.budget_actual,
            }


@dataclass(frozen=True)
class TileComputeBudget:
    max_object_gets: int = 0
    max_byte_range_gets: int = 0
    max_object_bytes: int = 0
    max_zarr_chunks: int = 0
    max_shard_index_reads: int = 0


class TileBudgetExceeded(RuntimeError):
    def __init__(self, *, metric: str, actual: int, limit: int) -> None:
        self.metric = metric
        self.actual = actual
        self.limit = limit
        super().__init__(f"{metric} {actual} exceeded limit {limit}")

    def detail(self) -> dict[str, object]:
        return {
            "error": "direct_tile_compute_budget_exceeded",
            "reason": str(self),
            "metric": self.metric,
            "actual": self.actual,
            "limit": self.limit,
        }


def enforce_tile_compute_budget(metrics: TileRequestMetrics, budget: TileComputeBudget) -> None:
    checks = (
        ("object_get_count", metrics.object_get_count, budget.max_object_gets),
        ("byte_range_get_count", metrics.byte_range_get_count, budget.max_byte_range_gets),
        ("object_bytes_read", metrics.object_bytes_read, budget.max_object_bytes),
        ("chunk_reads", metrics.chunk_reads, budget.max_zarr_chunks),
        ("shard_index_reads", metrics.shard_index_reads, budget.max_shard_index_reads),
    )
    for metric, actual, limit in checks:
        if limit > 0 and actual > limit:
            exc = TileBudgetExceeded(metric=metric, actual=actual, limit=limit)
            metrics.record_budget_decision(
                status="exceeded",
                reason=str(exc),
                metric=metric,
                actual=actual,
                limit=limit,
            )
            raise exc
    metrics.record_budget_decision(status="allowed", reason="within direct tile compute budget")


@contextmanager
def activate_tile_metrics(metrics: TileRequestMetrics) -> Iterator[TileRequestMetrics]:
    token = _CURRENT_TILE_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _CURRENT_TILE_METRICS.reset(token)


def current_tile_metrics() -> TileRequestMetrics | None:
    return _CURRENT_TILE_METRICS.get()


@contextmanager
def observe_tile_time(name: str) -> Iterator[None]:
    metrics = current_tile_metrics()
    if metrics is None:
        yield
        return
    with metrics.time_block(name):
        yield


def record_object_read(*, bytes_read: int, byte_range: bool = False) -> None:
    metrics = current_tile_metrics()
    if metrics is not None:
        metrics.record_object_read(bytes_read=bytes_read, byte_range=byte_range)


def record_zarr_shard_index_read() -> None:
    metrics = current_tile_metrics()
    if metrics is not None:
        metrics.record_shard_index_read()


def record_zarr_chunk_read() -> None:
    metrics = current_tile_metrics()
    if metrics is not None:
        metrics.record_chunk_read()


def build_tile_debug_headers(metrics: TileRequestMetrics) -> dict[str, str]:
    snapshot = metrics.snapshot()
    timings = snapshot["timings_ms"]
    assert isinstance(timings, dict)
    headers = {
        "X-Tile-Time-Ms": str(timings.get("total_request", 0.0)),
        "X-Tile-Planner-Ms": str(timings.get("planner", 0.0)),
        "X-Tile-Cache-Lookup-Ms": str(timings.get("cache_lookup", 0.0)),
        "X-Tile-Catalog-Ms": str(timings.get("catalog_metadata", 0.0)),
        "X-Tile-Render-Ms": str(timings.get("representation_generation", 0.0)),
        "X-Tile-Encode-Ms": str(timings.get("image_encoding", 0.0)),
        "X-Object-Get-Count": str(snapshot["object_get_count"]),
        "X-Object-Byte-Range-Get-Count": str(snapshot["byte_range_get_count"]),
        "X-Object-Bytes-Read": str(snapshot["object_bytes_read"]),
        "X-Zarr-Shard-Index-Reads": str(snapshot["shard_index_reads"]),
        "X-Zarr-Chunk-Count": str(snapshot["chunk_reads"]),
        "X-Tile-Budget-Status": str(snapshot["budget_status"]),
        "X-Tile-Budget-Reason": str(snapshot["budget_reason"]),
    }
    if snapshot["budget_metric"]:
        headers["X-Tile-Budget-Metric"] = str(snapshot["budget_metric"])
    if snapshot["budget_limit"] is not None:
        headers["X-Tile-Budget-Limit"] = str(snapshot["budget_limit"])
    if snapshot["budget_actual"] is not None:
        headers["X-Tile-Budget-Actual"] = str(snapshot["budget_actual"])
    return headers
