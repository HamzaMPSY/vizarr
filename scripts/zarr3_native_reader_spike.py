#!/usr/bin/env python3
"""Compare native zarr-python v3 reads with Vizarr's custom sharded reader.

The spike intentionally uses a temporary local Zarr v3 store with indexed
sharding. It proves codec/read parity without requiring OCI credentials, while
preserving the key decision point: Vizarr's OCI reader exposes byte-range and
tile-debug counters that native zarr-python does not expose through this path.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import zarr


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.tile_observability import activate_tile_metrics  # noqa: E402
from app.core.tile_observability import record_object_read  # noqa: E402
from app.core.tile_observability import TileRequestMetrics  # noqa: E402
from app.core.zarr_v3 import clear_zarr_shard_index_cache  # noqa: E402
from app.core.zarr_v3 import load_4d_window  # noqa: E402
from app.core.zarr_v3 import parse_array_metadata  # noqa: E402
from app.core.zarr_v3 import read_store_metadata  # noqa: E402


@dataclass
class LocalObjectStoreConnector:
    root: Path
    text_reads: int = 0
    bytes_reads: int = 0
    byte_range_reads: int = 0
    byte_tail_reads: int = 0
    bytes_read: int = 0

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://local@fixture/{object_path.lstrip('/')}"

    def read_text(self, object_path: str, *, use_cache: bool = False) -> str:
        payload = self._resolve(object_path).read_text(encoding="utf-8")
        payload_bytes = payload.encode("utf-8")
        self.text_reads += 1
        self.bytes_read += len(payload_bytes)
        record_object_read(bytes_read=len(payload_bytes), byte_range=False)
        return payload

    def read_bytes(self, object_path: str, *, use_cache: bool = False) -> bytes:
        payload = self._resolve(object_path).read_bytes()
        self.bytes_reads += 1
        self.bytes_read += len(payload)
        record_object_read(bytes_read=len(payload), byte_range=False)
        return payload

    def read_byte_range(
        self,
        object_path: str,
        *,
        start: int | None = None,
        end: int | None = None,
        use_cache: bool = False,
    ) -> bytes:
        data = self._resolve(object_path).read_bytes()
        payload = data[start:end]
        self.byte_range_reads += 1
        self.bytes_read += len(payload)
        record_object_read(bytes_read=len(payload), byte_range=True)
        return payload

    def read_byte_tail(self, object_path: str, *, length: int, use_cache: bool = False) -> bytes:
        data = self._resolve(object_path).read_bytes()
        payload = data[-length:]
        self.byte_tail_reads += 1
        self.bytes_read += len(payload)
        record_object_read(bytes_read=len(payload), byte_range=True)
        return payload

    def list_prefixes(self, prefix: str) -> list[str]:
        directory = self.root / prefix.rstrip("/")
        if not directory.exists():
            return []
        return [
            f"{prefix.rstrip('/')}/{child.name}/"
            for child in sorted(directory.iterdir())
            if child.is_dir()
        ]

    def _resolve(self, object_path: str) -> Path:
        resolved = object_path.removeprefix("oci://").removeprefix("local@fixture/")
        return self.root / resolved

    def counters(self) -> dict[str, int]:
        return {
            "text_reads": self.text_reads,
            "bytes_reads": self.bytes_reads,
            "byte_range_reads": self.byte_range_reads,
            "byte_tail_reads": self.byte_tail_reads,
            "bytes_read": self.bytes_read,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the priority 043 native Zarr 3 reader migration spike.",
    )
    parser.add_argument(
        "--print-fixture-path",
        action="store_true",
        help="Include the temporary fixture path in the JSON report.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="vizarr-zarr3-spike-") as temp_dir:
        temp_path = Path(temp_dir)
        fixture_path = temp_path / "fixture.zarr"
        source = create_fixture(fixture_path)
        report = compare_native_and_custom(fixture_path=fixture_path, source=source)
        if args.print_fixture_path:
            report["fixture_path"] = str(fixture_path)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def create_fixture(fixture_path: Path) -> np.ndarray:
    data = np.arange(1 * 1 * 4 * 4, dtype=np.uint16).reshape((1, 1, 4, 4))
    root = zarr.open_group(str(fixture_path), mode="w", zarr_format=3)
    root.create_array(
        "bands",
        data=data,
        chunks=(1, 1, 2, 2),
        shards=(1, 1, 4, 4),
        compressors=None,
        dimension_names=("time", "band", "y", "x"),
    )
    root.attrs["title"] = "Vizarr Zarr 3 native reader migration spike fixture"
    return data


def compare_native_and_custom(*, fixture_path: Path, source: np.ndarray) -> dict[str, Any]:
    selection = (0, 0, slice(1, 4), slice(1, 4))
    expected = source[selection]

    native_started = time.perf_counter()
    native_group = zarr.open_group(str(fixture_path), mode="r", zarr_format=3)
    native_array = native_group["bands"]
    native_window = np.asarray(native_array.get_basic_selection(selection))
    native_elapsed_ms = (time.perf_counter() - native_started) * 1000.0

    clear_zarr_shard_index_cache()
    connector = LocalObjectStoreConnector(root=fixture_path.parent)
    custom_started = time.perf_counter()
    _, metadata_nodes = read_store_metadata(connector=connector, store_path=fixture_path.name)  # type: ignore[arg-type]
    array_metadata = parse_array_metadata(metadata_nodes["bands"])
    metrics = TileRequestMetrics()
    with activate_tile_metrics(metrics):
        custom_window = load_4d_window(
            connector=connector,  # type: ignore[arg-type]
            store_path=fixture_path.name,
            array_name="bands",
            metadata=array_metadata,
            time_index=0,
            band_index=0,
            y_start=1,
            y_stop=4,
            x_start=1,
            x_stop=4,
            max_parallel_chunk_reads=1,
        )
    metrics.finish()
    custom_elapsed_ms = (time.perf_counter() - custom_started) * 1000.0

    return {
        "zarr_python_version": zarr.__version__,
        "fixture": {
            "format": 3,
            "array": "bands",
            "shape": list(source.shape),
            "outer_chunk_shape": list(array_metadata.chunk_shape),
            "effective_inner_chunk_shape": list(array_metadata.effective_chunk_shape),
            "is_sharded": array_metadata.sharding is not None,
        },
        "selection": {
            "time": 0,
            "band": 0,
            "y": [1, 4],
            "x": [1, 4],
        },
        "parity": {
            "native_matches_expected": bool(np.array_equal(native_window, expected)),
            "custom_matches_expected": bool(np.array_equal(custom_window, expected)),
            "native_matches_custom": bool(np.array_equal(native_window, custom_window)),
            "window": native_window.tolist(),
        },
        "native_zarr_python": {
            "elapsed_ms": round(native_elapsed_ms, 3),
            "metrics_visible_to_vizarr_tile_debug": False,
            "byte_range_or_object_get_counts_available": False,
        },
        "vizarr_custom_reader": {
            "elapsed_ms": round(custom_elapsed_ms, 3),
            "connector_counters": connector.counters(),
            "tile_metrics": metrics.snapshot(),
        },
        "recommendation": "replace_selected_metadata_reads_only; keep_custom_chunk_and_shard_reads",
    }


if __name__ == "__main__":
    raise SystemExit(main())
