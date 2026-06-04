from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "oci_performance_benchmark.py"
if not SCRIPT_PATH.exists():
    pytest.skip("repo-root scripts are not mounted in this test environment", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("oci_performance_benchmark", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules["oci_performance_benchmark"] = benchmark
SPEC.loader.exec_module(benchmark)


def http_json(payload: Any, headers: dict[str, str] | None = None):
    merged_headers = {"content-type": "application/json", **(headers or {})}
    return benchmark.HttpResult(
        status=200,
        headers={key.lower(): value for key, value in merged_headers.items()},
        body=json.dumps(payload).encode("utf-8"),
    )


def http_tile(headers: dict[str, str] | None = None):
    merged_headers = {
        "content-type": "image/webp",
        "x-cache-status": "MISS",
        "x-representation": "browse",
        "x-execution-path": "interactive",
        **(headers or {}),
    }
    return benchmark.HttpResult(
        status=200,
        headers={key.lower(): value for key, value in merged_headers.items()},
        body=b"RIFFxxxxWEBP",
    )


def test_select_oci_dataset_skips_synthetic_only_payload() -> None:
    with pytest.raises(benchmark.BenchmarkSkip, match="no OCI-backed dataset"):
        benchmark.select_oci_dataset(
            [{"id": "demo-global", "name": "Synthetic Global Weather"}],
            dataset_id=None,
        )


def test_normalize_tile_template_rewrites_container_backend_host() -> None:
    template = "http://backend:8000/api/tiles/d/v/{z}/{x}/{y}?time_index=0"

    normalized = benchmark.normalize_tile_template(template, "http://localhost:8001/api")

    assert normalized == "http://localhost:8001/api/tiles/d/v/{z}/{x}/{y}?time_index=0"


def test_load_matrix_entry_resolves_private_target_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps({
            "version": 1,
            "entries": [
                {
                    "id": "epsg4326-sharded",
                    "shape_class": "time/band/y/x",
                    "zarr_format": 3,
                    "consolidated": True,
                    "crs_authority": "EPSG:4326",
                    "chunk_layout": {"sharded": True},
                    "expected_variables": ["NDVI"],
                    "expected_composites": [],
                    "expected_representations_by_zoom": {"low": "browse"},
                    "benchmark": {
                        "dataset_id_env": "TEST_MATRIX_DATASET_ID",
                        "variable_env": "TEST_MATRIX_VARIABLE",
                        "expected_representation": "browse",
                        "forbid_serving": True,
                    },
                }
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MATRIX_DATASET_ID", "local-private-dataset")
    monkeypatch.setenv("TEST_MATRIX_VARIABLE", "NDVI")

    entry = benchmark.load_matrix_entry(str(matrix_path), "epsg4326-sharded")

    assert entry["id"] == "epsg4326-sharded"
    assert benchmark.resolve_matrix_env(entry, "dataset_id_env") == "local-private-dataset"
    assert benchmark.resolve_matrix_env(entry, "variable_env") == "NDVI"
    assert benchmark.matrix_expected_representation(entry) == "browse"
    assert benchmark.matrix_forbid_serving(entry) is True


def test_matrix_entry_skips_when_private_env_is_missing(tmp_path: Path) -> None:
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps({
            "entries": [
                {
                    "id": "entry",
                    "shape_class": "y/x",
                    "zarr_format": 3,
                    "crs_authority": "EPSG:32629",
                    "benchmark": {"dataset_id_env": "MISSING_DATASET_ID"},
                }
            ]
        }),
        encoding="utf-8",
    )
    entry = benchmark.load_matrix_entry(str(matrix_path), "entry")

    with pytest.raises(benchmark.BenchmarkSkip, match="requires env MISSING_DATASET_ID"):
        benchmark.resolve_matrix_env(entry, "dataset_id_env")


def test_build_tile_urls_generates_centered_viewport_set() -> None:
    urls = benchmark.build_tile_urls(
        "http://localhost:8001/api/tiles/d/v/{z}/{x}/{y}",
        z=2,
        center_x=1,
        center_y=1,
        radius=1,
    )

    assert len(urls) == 9
    assert "http://localhost:8001/api/tiles/d/v/2/1/1" in urls


def test_frontend_rendering_summary_reports_browser_gpu_probe() -> None:
    stdout_json = {
        "active_rendering_mode": "browser-gpu",
        "gpu_ready": True,
        "gpu_status": "native",
        "gpu_reason": "browser-gpu raw-float shader-colormap viewport-window 0",
        "gpu_renderer": "raw-float-shader-colormap",
        "failed_request_count": 0,
        "selected": {
            "dataset_id": "oci-cube",
            "variable_id": "B04",
            "time_index": 0,
            "zoom": 6.5,
        },
        "timings_ms": {"render_mode_observed_ms": 350},
    }

    rendering = benchmark.summarize_frontend_rendering(
        {},
        {"status": "passed", "stdout_json": stdout_json},
    )
    modes = benchmark.summarize_rendering_modes(
        {
            "supported_rendering_modes": ["dynamic_tiles", "multiscale_proxy", "browser_gpu"],
            "browser_multiscale_ready": True,
            "browser_gpu_ready": True,
        },
        rendering,
    )

    assert rendering["mode"] == "browser-gpu"
    assert rendering["gpu"]["status"] == "native"
    assert rendering["failed_request_count"] == 0
    assert rendering["selected"]["dataset_id"] == "oci-cube"
    assert modes["browser_gpu"]["active"] is True
    assert modes["browser_gpu"]["ready"] is True
    assert modes["browser_native"]["ready"] is True


def test_run_benchmark_reports_cold_and_warm_tile_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, int] = {}

    def fake_get_bytes(url: str, _timeout_seconds: float):
        calls[url] = calls.get(url, 0) + 1
        if url.endswith("/healthz"):
            return http_json({"status": "ok"})
        if url.endswith("/datasets"):
            return http_json([
                {
                    "id": "oci-cube",
                    "name": "OCI Cube",
                    "zarr_format": 3,
                    "zarr_consolidated": True,
                    "zarr_proxy_root": "/api/zarr/oci-cube",
                    "crs_authority": "EPSG:4326",
                    "composite_styles": [],
                }
            ], headers={
                "x-dataset-manifest-source": "object_manifest",
                "x-dataset-manifest-status": "loaded",
                "x-dataset-manifest-generated-at": "2026-06-03T00:00:00+00:00",
            })
        if url.endswith("/datasets/oci-cube/variables"):
            return http_json([{"id": "B04", "name": "Red"}])
        if url.endswith("/datasets/oci-cube/serving-profile"):
            return http_json({
                "browser_multiscale_ready": False,
                "seamless_rendering_ready": True,
                "seamless_rendering_gaps": ["missing_multiscale_pyramid"],
                "supported_rendering_modes": ["dynamic_tiles", "browse_overviews"],
                "browse_overview_zoom_levels": [0, 1],
                "browse_overview_max_zoom": 1,
                "chunk_layout": {"sharded": True},
            })
        if url.endswith("/tilejson/oci-cube/B04"):
            return http_json({
                "tilejson": "3.0.0",
                "name": "OCI Cube",
                "tiles": ["http://backend:8000/api/tiles/oci-cube/B04/{z}/{x}/{y}?time_index=0"],
                "bounds": [-1.0, -1.0, 1.0, 1.0],
                "minzoom": 1,
                "maxzoom": 1,
                "has_coarse_fallback": True,
            })
        if "/api/tiles/oci-cube/B04/" in url:
            cache_status = "MISS" if calls[url] == 1 else "HIT"
            return http_tile(
                {
                    "x-cache-status": cache_status,
                    "x-browse-source": "overview",
                    "x-tile-time-ms": "12.5",
                    "x-tile-render-ms": "7.25",
                    "x-tile-encode-ms": "1.5",
                    "x-object-get-count": "3" if cache_status == "MISS" else "0",
                    "x-object-byte-range-get-count": "2" if cache_status == "MISS" else "0",
                    "x-object-bytes-read": "4096" if cache_status == "MISS" else "0",
                    "x-zarr-chunk-count": "2" if cache_status == "MISS" else "0",
                    "x-zarr-shard-index-reads": "1" if cache_status == "MISS" else "0",
                }
            )
        if url == "http://localhost:5173":
            return benchmark.HttpResult(
                status=200,
                headers={"content-type": "text/html"},
                body=b'<html><body><div id="root"></div></body></html>',
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(benchmark, "get_bytes", fake_get_bytes)

    report = benchmark.run_benchmark(
        api_url="http://localhost:8001/api",
        frontend_url="http://localhost:5173",
        dataset_id=None,
        variable_id=None,
        timeout_seconds=5,
        tile_radius=0,
        skip_frontend_check=False,
        metadata_p95_budget_ms=0,
        cold_tile_p95_budget_ms=0,
        warm_tile_p95_budget_ms=0,
        expected_representation="browse",
        forbid_serving=True,
        playwright_command=None,
        matrix_entry={
            "id": "epsg4326-sharded",
            "shape_class": "time/band/y/x",
            "zarr_format": 3,
            "consolidated": True,
            "crs_authority": "EPSG:4326",
            "chunk_layout": {"sharded": True},
            "expected_variables": ["B04"],
            "expected_composites": [],
        },
    )

    assert report["dataset_id"] == "oci-cube"
    assert report["variable_id"] == "B04"
    assert report["tile_set"]["count"] == 1
    assert report["tiles"]["cold"]["cache_status_counts"] == {"MISS": 1}
    assert report["tiles"]["warm"]["cache_status_counts"] == {"HIT": 1}
    assert report["tiles"]["cold"]["representation_counts"] == {"browse": 1}
    assert report["tiles"]["cold"]["tile_time_p95_ms"] == 12.5
    assert report["tiles"]["cold"]["object_get_count"] == 3
    assert report["tiles"]["cold"]["object_byte_range_get_count"] == 2
    assert report["tiles"]["cold"]["object_bytes_read"] == 4096
    assert report["tiles"]["cold"]["zarr_chunk_count"] == 2
    assert report["tiles"]["cold"]["zarr_shard_index_reads"] == 1
    assert report["tiles"]["warm"]["object_get_count"] == 0
    assert report["frontend"]["content_type"] == "text/html"
    dataset_request = next(item for item in report["metadata"]["requests"] if item["name"] == "datasets")
    assert dataset_request["dataset_manifest_source"] == "object_manifest"
    assert dataset_request["dataset_manifest_status"] == "loaded"
    assert dataset_request["dataset_manifest_generated_at"] == "2026-06-03T00:00:00+00:00"


def test_run_benchmark_fails_when_expected_representation_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_bytes(url: str, _timeout_seconds: float):
        if url.endswith("/healthz"):
            return http_json({"status": "ok"})
        if url.endswith("/datasets"):
            return http_json([
                {"id": "oci-cube", "zarr_format": 3, "zarr_proxy_root": "/api/zarr/oci-cube"}
            ])
        if url.endswith("/datasets/oci-cube/variables"):
            return http_json([{"id": "B04"}])
        if url.endswith("/datasets/oci-cube/serving-profile"):
            return http_json({})
        if url.endswith("/tilejson/oci-cube/B04"):
            return http_json({
                "tiles": ["/api/tiles/oci-cube/B04/{z}/{x}/{y}"],
                "bounds": [-1.0, -1.0, 1.0, 1.0],
                "minzoom": 1,
                "maxzoom": 1,
            })
        if "/api/tiles/oci-cube/B04/" in url:
            return http_tile({"x-representation": "serving"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(benchmark, "get_bytes", fake_get_bytes)

    with pytest.raises(benchmark.BenchmarkFailure, match="did not report X-Representation"):
        benchmark.run_benchmark(
            api_url="http://localhost:8001/api",
            frontend_url="http://localhost:5173",
            dataset_id=None,
            variable_id=None,
            timeout_seconds=5,
            tile_radius=0,
            skip_frontend_check=True,
            metadata_p95_budget_ms=0,
            cold_tile_p95_budget_ms=0,
            warm_tile_p95_budget_ms=0,
            expected_representation="browse",
            forbid_serving=False,
            playwright_command=None,
        )


def test_run_benchmark_fails_when_covered_browse_zoom_falls_back_to_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_bytes(url: str, _timeout_seconds: float):
        if url.endswith("/healthz"):
            return http_json({"status": "ok"})
        if url.endswith("/datasets"):
            return http_json([
                {"id": "oci-cube", "zarr_format": 3, "zarr_proxy_root": "/api/zarr/oci-cube"}
            ])
        if url.endswith("/datasets/oci-cube/variables"):
            return http_json([{"id": "B04"}])
        if url.endswith("/datasets/oci-cube/serving-profile"):
            return http_json({
                "supported_rendering_modes": ["dynamic_tiles", "browse_overviews"],
                "browse_overview_zoom_levels": [1],
                "browse_coverage": {
                    "generation_status": "complete",
                    "available_zoom_levels": [1],
                },
            })
        if url.endswith("/tilejson/oci-cube/B04"):
            return http_json({
                "tiles": ["/api/tiles/oci-cube/B04/{z}/{x}/{y}"],
                "bounds": [-1.0, -1.0, 1.0, 1.0],
                "minzoom": 1,
                "maxzoom": 1,
            })
        if "/api/tiles/oci-cube/B04/" in url:
            return http_tile({"x-representation": "serving"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(benchmark, "get_bytes", fake_get_bytes)

    with pytest.raises(benchmark.BenchmarkFailure, match="covered browse zoom"):
        benchmark.run_benchmark(
            api_url="http://localhost:8001/api",
            frontend_url="http://localhost:5173",
            dataset_id=None,
            variable_id=None,
            timeout_seconds=5,
            tile_radius=0,
            skip_frontend_check=True,
            metadata_p95_budget_ms=0,
            cold_tile_p95_budget_ms=0,
            warm_tile_p95_budget_ms=0,
            expected_representation=None,
            forbid_serving=False,
            playwright_command=None,
        )


def test_run_benchmark_fails_when_matrix_metadata_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_bytes(url: str, _timeout_seconds: float):
        if url.endswith("/healthz"):
            return http_json({"status": "ok"})
        if url.endswith("/datasets"):
            return http_json([
                {
                    "id": "oci-cube",
                    "zarr_format": 3,
                    "zarr_consolidated": True,
                    "zarr_proxy_root": "/api/zarr/oci-cube",
                    "crs_authority": "EPSG:4326",
                    "composite_styles": [],
                }
            ])
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(benchmark, "get_bytes", fake_get_bytes)

    with pytest.raises(benchmark.BenchmarkFailure, match="crs_authority"):
        benchmark.run_benchmark(
            api_url="http://localhost:8001/api",
            frontend_url="http://localhost:5173",
            dataset_id=None,
            variable_id=None,
            timeout_seconds=5,
            tile_radius=0,
            skip_frontend_check=True,
            metadata_p95_budget_ms=0,
            cold_tile_p95_budget_ms=0,
            warm_tile_p95_budget_ms=0,
            expected_representation=None,
            forbid_serving=False,
            playwright_command=None,
            matrix_entry={
                "id": "projected",
                "shape_class": "time/band/y/x",
                "zarr_format": 3,
                "consolidated": True,
                "crs_authority": "EPSG:32629",
            },
        )
