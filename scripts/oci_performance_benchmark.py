#!/usr/bin/env python3
"""Benchmark the live OCI-backed Vizarr browser path without storing secrets.

The harness intentionally uses only the Python standard library. It can run from
a host checkout, a CI shell, or a VM handoff environment without importing the
backend package.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import Request
from urllib.request import urlopen


DEFAULT_API_URL = "http://localhost:8001/api"
DEFAULT_FRONTEND_URL = "http://localhost:5173"
MAX_WEB_MERCATOR_LATITUDE = 85.05112878


class BenchmarkFailure(RuntimeError):
    """A benchmark requirement failed."""


class BenchmarkSkip(RuntimeError):
    """The live OCI benchmark path is not applicable in this environment."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BenchmarkFailure("response was not valid JSON") from exc


@dataclass(frozen=True)
class TimedHttpResult:
    url: str
    elapsed_ms: float
    response: HttpResult


def main() -> int:
    args = parse_args()
    api_url = normalize_base_url(args.api_url)
    frontend_url = normalize_base_url(args.frontend_url)

    try:
        matrix_entry = load_matrix_entry(args.matrix, args.matrix_entry)
        dataset_id = args.dataset_id or resolve_matrix_env(matrix_entry, "dataset_id_env")
        variable_id = args.variable or resolve_matrix_env(matrix_entry, "variable_env")
        expected_representation = args.expected_representation or matrix_expected_representation(matrix_entry)
        forbid_serving = args.forbid_serving or matrix_forbid_serving(matrix_entry)
        report = run_benchmark(
            api_url=api_url,
            frontend_url=frontend_url,
            dataset_id=dataset_id,
            variable_id=variable_id,
            timeout_seconds=args.timeout,
            tile_radius=args.tile_radius,
            skip_frontend_check=args.skip_frontend_check,
            metadata_p95_budget_ms=args.metadata_p95_budget_ms,
            cold_tile_p95_budget_ms=args.cold_tile_p95_budget_ms,
            warm_tile_p95_budget_ms=args.warm_tile_p95_budget_ms,
            expected_representation=expected_representation,
            forbid_serving=forbid_serving,
            playwright_command=args.playwright_command,
            matrix_entry=matrix_entry,
        )
    except BenchmarkSkip as exc:
        print(f"SKIP: {exc}")
        return 0
    except BenchmarkFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print_summary(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Report written: {output_path}")
    if args.print_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Vizarr OCI dataset discovery, tile serving, cache behavior, and frontend readiness.",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("VIZARR_API_URL", DEFAULT_API_URL),
        help=f"Base API URL, default: env VIZARR_API_URL or {DEFAULT_API_URL}",
    )
    parser.add_argument(
        "--frontend-url",
        default=os.environ.get("VIZARR_FRONTEND_URL", DEFAULT_FRONTEND_URL),
        help=f"Frontend URL, default: env VIZARR_FRONTEND_URL or {DEFAULT_FRONTEND_URL}",
    )
    parser.add_argument(
        "--dataset-id",
        default=os.environ.get("VIZARR_OCI_DATASET_ID"),
        help="Optional OCI dataset id to benchmark.",
    )
    parser.add_argument(
        "--variable",
        default=os.environ.get("VIZARR_OCI_VARIABLE"),
        help="Optional variable/band/composite id to benchmark.",
    )
    parser.add_argument(
        "--matrix",
        default=os.environ.get("VIZARR_OCI_CUBE_MATRIX"),
        help="Optional secret-free OCI cube matrix JSON file.",
    )
    parser.add_argument(
        "--matrix-entry",
        default=os.environ.get("VIZARR_OCI_CUBE_MATRIX_ENTRY"),
        help="Entry id inside --matrix. Uses entry env var names for private dataset/variable selection.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("VIZARR_BENCHMARK_TIMEOUT", "30")),
        help="HTTP timeout in seconds, default: 30.",
    )
    parser.add_argument(
        "--tile-radius",
        type=int,
        default=int(os.environ.get("VIZARR_BENCHMARK_TILE_RADIUS", "1")),
        help="Radius around the center XYZ tile; 1 benchmarks a 3x3 tile set.",
    )
    parser.add_argument(
        "--metadata-p95-budget-ms",
        type=float,
        default=float(os.environ.get("VIZARR_METADATA_P95_BUDGET_MS", "0")),
        help="Fail when metadata p95 exceeds this value. 0 disables the budget.",
    )
    parser.add_argument(
        "--cold-tile-p95-budget-ms",
        type=float,
        default=float(os.environ.get("VIZARR_COLD_TILE_P95_BUDGET_MS", "0")),
        help="Fail when cold tile p95 exceeds this value. 0 disables the budget.",
    )
    parser.add_argument(
        "--warm-tile-p95-budget-ms",
        type=float,
        default=float(os.environ.get("VIZARR_WARM_TILE_P95_BUDGET_MS", "0")),
        help="Fail when warm tile p95 exceeds this value. 0 disables the budget.",
    )
    parser.add_argument(
        "--expected-representation",
        choices=("browse", "pyramid", "serving"),
        default=os.environ.get("VIZARR_EXPECTED_REPRESENTATION"),
        help="Fail when any tile reports a different X-Representation.",
    )
    parser.add_argument(
        "--forbid-serving",
        action="store_true",
        help="Fail when any tile reports X-Representation: serving.",
    )
    parser.add_argument(
        "--skip-frontend-check",
        action="store_true",
        help="Only verify backend OCI catalog, TileJSON, and tiles.",
    )
    parser.add_argument(
        "--playwright-command",
        default=os.environ.get("VIZARR_PLAYWRIGHT_COMMAND"),
        help=(
            "Optional command for browser-visible readiness. Use {frontend_url} "
            "as a placeholder; command must exit 0 and may print JSON."
        ),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("VIZARR_BENCHMARK_OUTPUT"),
        help="Optional path for a machine-readable JSON report.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the full JSON report to stdout after the summary.",
    )
    return parser.parse_args()


def run_benchmark(
    *,
    api_url: str,
    frontend_url: str,
    dataset_id: str | None,
    variable_id: str | None,
    timeout_seconds: float,
    tile_radius: int,
    skip_frontend_check: bool,
    metadata_p95_budget_ms: float,
    cold_tile_p95_budget_ms: float,
    warm_tile_p95_budget_ms: float,
    expected_representation: str | None,
    forbid_serving: bool,
    playwright_command: str | None,
    matrix_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tile_radius < 0:
        raise BenchmarkFailure("--tile-radius must be >= 0")

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata_results: list[dict[str, Any]] = []

    health = measure_json("health", f"{api_url}/healthz", timeout_seconds)
    if health.response.json().get("status") != "ok":
        raise BenchmarkFailure(f"health check returned unexpected payload: {health.response.json()!r}")
    metadata_results.append(timed_json_record(health, "health"))

    datasets_result = measure_json("datasets", f"{api_url}/datasets", timeout_seconds)
    datasets = datasets_result.response.json()
    if not isinstance(datasets, list):
        raise BenchmarkFailure("/datasets did not return a list")
    metadata_results.append(timed_json_record(datasets_result, "datasets"))
    dataset = select_oci_dataset(datasets, dataset_id)
    validate_dataset_against_matrix(dataset, matrix_entry)

    encoded_dataset = quote(dataset["id"], safe="")
    variables_result = measure_json(
        "variables",
        f"{api_url}/datasets/{encoded_dataset}/variables",
        timeout_seconds,
    )
    variables = variables_result.response.json()
    if not isinstance(variables, list) or not variables:
        raise BenchmarkFailure(f"dataset {dataset['id']} has no variables")
    validate_variables_against_matrix(variables, matrix_entry)
    metadata_results.append(timed_json_record(variables_result, "variables"))
    variable = select_variable(variables, variable_id)

    profile_result = measure_json(
        "serving_profile",
        f"{api_url}/datasets/{encoded_dataset}/serving-profile",
        timeout_seconds,
    )
    serving_profile = profile_result.response.json()
    validate_serving_profile_against_matrix(serving_profile, matrix_entry)
    metadata_results.append(timed_json_record(profile_result, "serving_profile"))

    encoded_variable = quote(variable["id"], safe="")
    tilejson_result = measure_json(
        "tilejson",
        f"{api_url}/tilejson/{encoded_dataset}/{encoded_variable}",
        timeout_seconds,
    )
    tilejson = tilejson_result.response.json()
    metadata_results.append(timed_json_record(tilejson_result, "tilejson"))

    tile_template = normalize_tile_template(resolve_tile_template(tilejson), api_url)
    z = choose_zoom(tilejson)
    center_x, center_y = center_xyz(tilejson["bounds"], z)
    tile_urls = build_tile_urls(tile_template, z, center_x, center_y, tile_radius)
    if not tile_urls:
        raise BenchmarkFailure("no tile URLs were generated for benchmark")

    cold_tiles = run_tile_pass("cold", tile_urls, timeout_seconds)
    warm_tiles = run_tile_pass("warm", tile_urls, timeout_seconds)

    frontend_result: dict[str, Any] | None = None
    if not skip_frontend_check:
        frontend = measure_bytes(frontend_url, timeout_seconds)
        content_type = frontend.response.headers.get("content-type", "")
        if frontend.response.status != 200:
            raise BenchmarkFailure(f"frontend returned HTTP {frontend.response.status}: {frontend_url}")
        if "text/html" not in content_type.lower():
            raise BenchmarkFailure(f"frontend content-type was {content_type!r}, expected text/html")
        if b'id="root"' not in frontend.response.body and b"id='root'" not in frontend.response.body:
            raise BenchmarkFailure("frontend shell did not contain the React root node")
        frontend_result = {
            "url": frontend_url,
            "elapsed_ms": round(frontend.elapsed_ms, 3),
            "content_type": content_type,
            "body_bytes": len(frontend.response.body),
        }

    playwright_result = run_playwright_probe(playwright_command, frontend_url, timeout_seconds)
    frontend_rendering = summarize_frontend_rendering(serving_profile, playwright_result)
    rendering_modes = summarize_rendering_modes(serving_profile, frontend_rendering)

    report = {
        "status": "passed",
        "started_at": started_at,
        "api_url": api_url,
        "frontend_url": frontend_url,
        "dataset_id": dataset["id"],
        "variable_id": variable["id"],
        "matrix_entry": summarize_matrix_entry(matrix_entry),
        "serving_profile": summarize_serving_profile(serving_profile),
        "tile_set": {
            "z": z,
            "center_x": center_x,
            "center_y": center_y,
            "radius": tile_radius,
            "count": len(tile_urls),
        },
        "metadata": {
            "requests": metadata_results,
            "p95_ms": percentile([item["elapsed_ms"] for item in metadata_results], 95),
        },
        "tiles": {
            "cold": summarize_tile_pass(cold_tiles),
            "warm": summarize_tile_pass(warm_tiles),
        },
        "frontend": frontend_result,
        "playwright": playwright_result,
        "frontend_rendering": frontend_rendering,
        "rendering_modes": rendering_modes,
        "budgets": {
            "metadata_p95_ms": budget_value(metadata_p95_budget_ms),
            "cold_tile_p95_ms": budget_value(cold_tile_p95_budget_ms),
            "warm_tile_p95_ms": budget_value(warm_tile_p95_budget_ms),
            "expected_representation": expected_representation,
            "forbid_serving": forbid_serving,
        },
    }
    enforce_budgets(
        report,
        metadata_p95_budget_ms=metadata_p95_budget_ms,
        cold_tile_p95_budget_ms=cold_tile_p95_budget_ms,
        warm_tile_p95_budget_ms=warm_tile_p95_budget_ms,
        expected_representation=expected_representation,
        forbid_serving=forbid_serving,
    )
    return report


def select_oci_dataset(datasets: list[Any], dataset_id: str | None) -> dict[str, Any]:
    typed = [item for item in datasets if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if dataset_id is not None:
        for item in typed:
            if item["id"] == dataset_id:
                if not is_oci_dataset(item):
                    raise BenchmarkFailure(f"dataset {dataset_id!r} is present but does not look OCI-backed")
                return item
        raise BenchmarkFailure(f"requested dataset {dataset_id!r} was not returned by /datasets")

    for item in typed:
        if is_oci_dataset(item):
            return item
    raise BenchmarkSkip("no OCI-backed dataset metadata returned by /datasets")


def is_oci_dataset(dataset: dict[str, Any]) -> bool:
    return any(
        dataset.get(field) is not None
        for field in (
            "zarr_format",
            "zarr_proxy_root",
            "multiscale_zarr_format",
            "multiscale_proxy_root",
        )
    )


def select_variable(variables: list[Any], variable_id: str | None) -> dict[str, Any]:
    typed = [item for item in variables if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not typed:
        raise BenchmarkFailure("variables payload had no usable variable ids")
    if variable_id is None:
        return typed[0]
    for item in typed:
        if item["id"] == variable_id:
            return item
    raise BenchmarkFailure(f"requested variable {variable_id!r} was not returned by /variables")


def load_matrix_entry(matrix_path: str | None, entry_id: str | None) -> dict[str, Any] | None:
    if not matrix_path and not entry_id:
        return None
    if not matrix_path:
        raise BenchmarkFailure("--matrix-entry requires --matrix")
    path = Path(matrix_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkFailure(f"matrix file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkFailure(f"matrix file is not valid JSON: {path}") from exc

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise BenchmarkFailure("matrix file must contain an entries list")
    if not entry_id:
        raise BenchmarkFailure("--matrix requires --matrix-entry")

    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == entry_id:
            validate_matrix_entry(entry)
            return entry
    raise BenchmarkFailure(f"matrix entry {entry_id!r} was not found in {path}")


def validate_matrix_entry(entry: dict[str, Any]) -> None:
    required_strings = ("id", "shape_class", "crs_authority")
    missing = [field for field in required_strings if not isinstance(entry.get(field), str) or not entry.get(field)]
    if missing:
        raise BenchmarkFailure(f"matrix entry {entry.get('id', '<unknown>')!r} is missing fields: {', '.join(missing)}")
    if not isinstance(entry.get("zarr_format"), int):
        raise BenchmarkFailure(f"matrix entry {entry['id']!r} must include integer zarr_format")
    benchmark_config = entry.get("benchmark", {})
    if benchmark_config is not None and not isinstance(benchmark_config, dict):
        raise BenchmarkFailure(f"matrix entry {entry['id']!r} benchmark must be an object")


def validate_dataset_against_matrix(dataset: dict[str, Any], matrix_entry: dict[str, Any] | None) -> None:
    if matrix_entry is None:
        return
    expected_zarr_format = matrix_entry.get("zarr_format")
    if expected_zarr_format is not None and dataset.get("zarr_format") != expected_zarr_format:
        raise BenchmarkFailure(
            f"dataset {dataset['id']!r} zarr_format {dataset.get('zarr_format')!r} "
            f"did not match matrix {expected_zarr_format!r}"
        )
    if "consolidated" in matrix_entry and dataset.get("zarr_consolidated") != matrix_entry.get("consolidated"):
        raise BenchmarkFailure(
            f"dataset {dataset['id']!r} zarr_consolidated {dataset.get('zarr_consolidated')!r} "
            f"did not match matrix {matrix_entry.get('consolidated')!r}"
        )
    expected_crs = matrix_entry.get("crs_authority")
    if expected_crs and dataset.get("crs_authority") != expected_crs:
        raise BenchmarkFailure(
            f"dataset {dataset['id']!r} crs_authority {dataset.get('crs_authority')!r} "
            f"did not match matrix {expected_crs!r}"
        )
    expected_composites = matrix_entry.get("expected_composites")
    if isinstance(expected_composites, list):
        actual = {
            str(item.get("id"))
            for item in dataset.get("composite_styles", [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        missing = [str(item) for item in expected_composites if str(item) not in actual]
        if missing:
            raise BenchmarkFailure(
                f"dataset {dataset['id']!r} is missing expected composite style(s): {', '.join(missing)}"
            )


def validate_variables_against_matrix(variables: list[Any], matrix_entry: dict[str, Any] | None) -> None:
    if matrix_entry is None:
        return
    expected_variables = matrix_entry.get("expected_variables")
    if not isinstance(expected_variables, list) or not expected_variables:
        return
    actual = {
        str(item.get("id"))
        for item in variables
        if isinstance(item, dict) and item.get("id") is not None
    }
    missing = [str(item) for item in expected_variables if str(item) not in actual]
    if missing:
        raise BenchmarkFailure(f"dataset variables are missing expected id(s): {', '.join(missing)}")


def validate_serving_profile_against_matrix(profile: Any, matrix_entry: dict[str, Any] | None) -> None:
    if matrix_entry is None or not isinstance(profile, dict):
        return
    expected_layout = matrix_entry.get("chunk_layout")
    actual_layout = profile.get("chunk_layout")
    if isinstance(expected_layout, dict) and isinstance(actual_layout, dict):
        expected_sharded = expected_layout.get("sharded")
        if expected_sharded is not None and actual_layout.get("sharded") != expected_sharded:
            raise BenchmarkFailure(
                f"serving profile chunk_layout.sharded {actual_layout.get('sharded')!r} "
                f"did not match matrix {expected_sharded!r}"
            )
    expected_modes = matrix_entry.get("expected_rendering_modes")
    if isinstance(expected_modes, list):
        actual_modes = set(profile.get("supported_rendering_modes") or [])
        missing = [str(item) for item in expected_modes if str(item) not in actual_modes]
        if missing:
            raise BenchmarkFailure(f"serving profile is missing expected rendering mode(s): {', '.join(missing)}")


def resolve_matrix_env(matrix_entry: dict[str, Any] | None, field: str) -> str | None:
    if matrix_entry is None:
        return None
    benchmark_config = matrix_entry.get("benchmark", {})
    if not isinstance(benchmark_config, dict):
        return None
    env_name = benchmark_config.get(field)
    if not isinstance(env_name, str) or not env_name:
        return None
    value = os.environ.get(env_name)
    if not value:
        raise BenchmarkSkip(
            f"matrix entry {matrix_entry['id']!r} requires env {env_name} or an explicit CLI override"
        )
    return value


def matrix_expected_representation(matrix_entry: dict[str, Any] | None) -> str | None:
    if matrix_entry is None:
        return None
    benchmark_config = matrix_entry.get("benchmark", {})
    if not isinstance(benchmark_config, dict):
        return None
    value = benchmark_config.get("expected_representation")
    if value is None:
        return None
    if value not in {"browse", "pyramid", "serving"}:
        raise BenchmarkFailure(
            f"matrix entry {matrix_entry['id']!r} has invalid expected_representation {value!r}"
        )
    return str(value)


def matrix_forbid_serving(matrix_entry: dict[str, Any] | None) -> bool:
    if matrix_entry is None:
        return False
    benchmark_config = matrix_entry.get("benchmark", {})
    if not isinstance(benchmark_config, dict):
        return False
    return bool(benchmark_config.get("forbid_serving", False))


def summarize_matrix_entry(matrix_entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if matrix_entry is None:
        return None
    return {
        "id": matrix_entry.get("id"),
        "shape_class": matrix_entry.get("shape_class"),
        "zarr_format": matrix_entry.get("zarr_format"),
        "consolidated": matrix_entry.get("consolidated"),
        "crs_authority": matrix_entry.get("crs_authority"),
        "chunk_layout": matrix_entry.get("chunk_layout"),
        "expected_variables": matrix_entry.get("expected_variables"),
        "expected_composites": matrix_entry.get("expected_composites"),
        "expected_representations_by_zoom": matrix_entry.get("expected_representations_by_zoom"),
    }


def resolve_tile_template(tilejson: Any) -> str:
    if not isinstance(tilejson, dict):
        raise BenchmarkFailure("TileJSON response was not an object")
    bounds = tilejson.get("bounds")
    tiles = tilejson.get("tiles")
    if not is_valid_bounds(bounds):
        raise BenchmarkFailure(f"TileJSON bounds are invalid: {bounds!r}")
    if not isinstance(tiles, list) or not tiles or not isinstance(tiles[0], str):
        raise BenchmarkFailure("TileJSON did not include a tile URL template")
    return tiles[0]


def normalize_tile_template(tile_template: str, api_url: str) -> str:
    if not tile_template.startswith(("http://", "https://")):
        api_parts = urlsplit(api_url)
        path = tile_template if tile_template.startswith("/") else f"/{tile_template}"
        return urlunsplit((api_parts.scheme, api_parts.netloc, path, "", ""))

    parsed = urlsplit(tile_template)
    if parsed.path.startswith("/api/"):
        api_parts = urlsplit(api_url)
        return urlunsplit((api_parts.scheme, api_parts.netloc, parsed.path, parsed.query, ""))
    return tile_template


def choose_zoom(tilejson: dict[str, Any]) -> int:
    minzoom = int(tilejson.get("minzoom", 0))
    maxzoom = int(tilejson.get("maxzoom", minzoom))
    detail_minzoom = tilejson.get("detail_minzoom")
    candidate = minzoom if detail_minzoom is None else int(detail_minzoom)
    return min(max(candidate, minzoom), maxzoom)


def center_xyz(bounds: list[float], z: int) -> tuple[int, int]:
    west, south, east, north = bounds
    lon = (west + east) / 2.0
    lat = max(min((south + north) / 2.0, MAX_WEB_MERCATOR_LATITUDE), -MAX_WEB_MERCATOR_LATITUDE)
    scale = 2**z
    x = int((lon + 180.0) / 360.0 * scale)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * scale)
    return max(0, min(x, scale - 1)), max(0, min(y, scale - 1))


def build_tile_urls(template: str, z: int, center_x: int, center_y: int, radius: int) -> list[str]:
    limit = 2**z
    urls: list[str] = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x = center_x + dx
            y = center_y + dy
            if x < 0 or x >= limit or y < 0 or y >= limit:
                continue
            urls.append(
                template
                .replace("{z}", str(z))
                .replace("{x}", str(x))
                .replace("{y}", str(y))
            )
    return urls


def run_tile_pass(pass_name: str, tile_urls: list[str], timeout_seconds: float) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for url in tile_urls:
        result = measure_bytes(url, timeout_seconds, allow_http_errors=True)
        content_type = result.response.headers.get("content-type", "")
        budget_detail = decode_direct_tile_budget_detail(result.response.body)
        if result.response.status != 200 and budget_detail is not None:
            records.append(tile_error_record(result, budget_detail))
            continue
        if result.response.status != 200:
            raise BenchmarkFailure(
                f"{pass_name} tile returned HTTP {result.response.status}: {decode_error_detail(result.response.body)}: {url}"
            )
        if "image/webp" not in content_type.lower():
            raise BenchmarkFailure(f"{pass_name} tile content-type was {content_type!r}, expected image/webp: {url}")
        records.append(tile_success_record(result))
    return records


def tile_success_record(result: TimedHttpResult) -> dict[str, Any]:
    return {
        "url": result.url,
        "elapsed_ms": round(result.elapsed_ms, 3),
        "status": result.response.status,
        "content_type": result.response.headers.get("content-type", ""),
        "body_bytes": len(result.response.body),
        "cache_status": header_value(result, "x-cache-status"),
        "representation": header_value(result, "x-representation"),
        "execution_path": header_value(result, "x-execution-path"),
        "browse_source": header_value(result, "x-browse-source"),
        "budget_status": header_value(result, "x-tile-budget-status"),
        "budget_reason": header_value(result, "x-tile-budget-reason"),
        "tile_time_ms": header_float(result, "x-tile-time-ms"),
        "planner_ms": header_float(result, "x-tile-planner-ms"),
        "cache_lookup_ms": header_float(result, "x-tile-cache-lookup-ms"),
        "catalog_ms": header_float(result, "x-tile-catalog-ms"),
        "render_ms": header_float(result, "x-tile-render-ms"),
        "encode_ms": header_float(result, "x-tile-encode-ms"),
        "object_get_count": header_int(result, "x-object-get-count"),
        "object_byte_range_get_count": header_int(result, "x-object-byte-range-get-count"),
        "object_bytes_read": header_int(result, "x-object-bytes-read"),
        "zarr_chunk_count": header_int(result, "x-zarr-chunk-count"),
        "zarr_shard_index_reads": header_int(result, "x-zarr-shard-index-reads"),
    }


def tile_error_record(result: TimedHttpResult, budget_detail: dict[str, Any]) -> dict[str, Any]:
    record = tile_success_record(result)
    record.update(
        {
            "error": budget_detail.get("error"),
            "budget_status": "exceeded",
            "budget_reason": budget_detail.get("reason"),
            "budget_metric": budget_detail.get("metric"),
            "budget_actual": budget_detail.get("actual"),
            "budget_limit": budget_detail.get("limit"),
        }
    )
    return record


def summarize_tile_pass(records: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(item["elapsed_ms"]) for item in records]
    return {
        "count": len(records),
        "min_ms": round(min(elapsed), 3) if elapsed else None,
        "max_ms": round(max(elapsed), 3) if elapsed else None,
        "p50_ms": percentile(elapsed, 50),
        "p95_ms": percentile(elapsed, 95),
        "cache_status_counts": count_values(item.get("cache_status") for item in records),
        "representation_counts": count_values(item.get("representation") for item in records),
        "execution_path_counts": count_values(item.get("execution_path") for item in records),
        "budget_status_counts": count_values(item.get("budget_status") for item in records),
        "direct_budget_exceeded_count": sum(1 for item in records if item.get("budget_status") == "exceeded"),
        "tile_time_p95_ms": percentile(compact_numbers(item.get("tile_time_ms") for item in records), 95),
        "object_get_count": sum_ints(item.get("object_get_count") for item in records),
        "object_byte_range_get_count": sum_ints(item.get("object_byte_range_get_count") for item in records),
        "object_bytes_read": sum_ints(item.get("object_bytes_read") for item in records),
        "zarr_chunk_count": sum_ints(item.get("zarr_chunk_count") for item in records),
        "zarr_shard_index_reads": sum_ints(item.get("zarr_shard_index_reads") for item in records),
        "records": records,
    }


def summarize_serving_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return {}
    return {
        "browser_gpu_ready": profile.get("browser_gpu_ready"),
        "browser_gpu_reason": profile.get("browser_gpu_reason"),
        "browser_gpu_gaps": profile.get("browser_gpu_gaps"),
        "browser_multiscale_ready": profile.get("browser_multiscale_ready"),
        "seamless_rendering_ready": profile.get("seamless_rendering_ready"),
        "seamless_rendering_gaps": profile.get("seamless_rendering_gaps"),
        "supported_rendering_modes": profile.get("supported_rendering_modes"),
        "browse_overview_zoom_levels": profile.get("browse_overview_zoom_levels"),
        "browse_overview_max_zoom": profile.get("browse_overview_max_zoom"),
        "has_multiscale": profile.get("has_multiscale"),
        "multiscale_paths": profile.get("multiscale_paths"),
        "multiscale_population_strategy": profile.get("multiscale_population_strategy"),
        "multiscale_prepopulated_zoom_max": profile.get("multiscale_prepopulated_zoom_max"),
        "multiscale_max_zoom": profile.get("multiscale_max_zoom"),
        "browse_coverage": profile.get("browse_coverage"),
    }


def summarize_frontend_rendering(profile: Any, playwright_result: dict[str, Any]) -> dict[str, Any]:
    stdout_json = playwright_result.get("stdout_json") if isinstance(playwright_result, dict) else None
    if isinstance(stdout_json, dict):
        for key in ("active_rendering_mode", "renderMode", "render_mode", "data-render-mode"):
            value = stdout_json.get(key)
            if value in {"browser-gpu", "browser-native", "server-tiles"}:
                return {
                    "mode": value,
                    "source": "playwright",
                    "gpu": summarize_gpu_probe(stdout_json),
                    "selected": stdout_json.get("selected"),
                    "timings_ms": stdout_json.get("timings_ms"),
                    "failed_request_count": stdout_json.get("failed_request_count"),
                    "detail": stdout_json,
                }
        dataset = stdout_json.get("dataset")
        if isinstance(dataset, dict):
            value = dataset.get("renderMode") or dataset.get("render_mode")
            if value in {"browser-gpu", "browser-native", "server-tiles"}:
                return {
                    "mode": value,
                    "source": "playwright",
                    "gpu": summarize_gpu_probe(stdout_json),
                    "selected": stdout_json.get("selected"),
                    "timings_ms": stdout_json.get("timings_ms"),
                    "failed_request_count": stdout_json.get("failed_request_count"),
                    "detail": stdout_json,
                }

    if isinstance(profile, dict) and profile.get("browser_gpu_ready") is True:
        return {"mode": "browser-gpu-eligible", "source": "serving_profile"}
    if isinstance(profile, dict) and profile.get("browser_multiscale_ready") is True:
        return {"mode": "browser-native-eligible", "source": "serving_profile"}
    return {"mode": "server-tiles", "source": "serving_profile"}


def summarize_gpu_probe(stdout_json: dict[str, Any]) -> dict[str, Any]:
    attributes = stdout_json.get("attributes")
    attr_gpu = attributes if isinstance(attributes, dict) else {}
    return {
        "ready": stdout_json.get("gpu_ready", attr_gpu.get("browserGpuReady")),
        "status": stdout_json.get("gpu_status", attr_gpu.get("browserGpuStatus")),
        "reason": stdout_json.get("gpu_reason", attr_gpu.get("browserGpuReason")),
        "renderer": stdout_json.get("gpu_renderer", attr_gpu.get("browserGpuRenderer")),
        "level": attr_gpu.get("browserGpuLevel"),
        "mode": attr_gpu.get("browserGpuMode"),
        "failure_count": attr_gpu.get("browserGpuFailureCount"),
        "last_error": attr_gpu.get("browserGpuLastError"),
    }


def summarize_rendering_modes(profile: Any, frontend_rendering: dict[str, Any]) -> dict[str, Any]:
    supported = profile.get("supported_rendering_modes") if isinstance(profile, dict) else []
    if not isinstance(supported, list):
        supported = []
    active_mode = frontend_rendering.get("mode")
    gpu = frontend_rendering.get("gpu") if isinstance(frontend_rendering.get("gpu"), dict) else {}
    return {
        "active": active_mode,
        "server_tiles": {
            "supported": True,
            "active": active_mode == "server-tiles",
        },
        "browser_native": {
            "supported": "multiscale_proxy" in supported,
            "ready": bool(profile.get("browser_multiscale_ready")) if isinstance(profile, dict) else False,
            "active": active_mode == "browser-native",
        },
        "browser_gpu": {
            "supported": "browser_gpu" in supported,
            "ready": bool(profile.get("browser_gpu_ready")) if isinstance(profile, dict) else False,
            "active": active_mode == "browser-gpu",
            "status": gpu.get("status"),
            "reason": gpu.get("reason") or (profile.get("browser_gpu_reason") if isinstance(profile, dict) else None),
            "gaps": profile.get("browser_gpu_gaps") if isinstance(profile, dict) else None,
            "renderer": gpu.get("renderer"),
            "failure_count": gpu.get("failure_count"),
            "last_error": gpu.get("last_error"),
        },
    }


def run_playwright_probe(command_template: str | None, frontend_url: str, timeout_seconds: float) -> dict[str, Any]:
    if not command_template:
        return {"status": "not_configured"}

    command = command_template.format(frontend_url=frontend_url)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkFailure(f"Playwright probe timed out after {timeout_seconds}s: {command}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if completed.returncode != 0:
        raise BenchmarkFailure(
            "Playwright probe failed "
            f"(exit {completed.returncode}): {(completed.stderr or completed.stdout).strip()[:500]}"
        )
    parsed_stdout: Any | None = None
    stripped_stdout = completed.stdout.strip()
    if stripped_stdout:
        try:
            parsed_stdout = json.loads(stripped_stdout)
        except json.JSONDecodeError:
            parsed_stdout = None
    return {
        "status": "passed",
        "elapsed_ms": round(elapsed_ms, 3),
        "stdout_json": parsed_stdout,
        "stdout": None if parsed_stdout is not None else stripped_stdout[:1000],
        "stderr": completed.stderr.strip()[:1000],
    }


def enforce_budgets(
    report: dict[str, Any],
    *,
    metadata_p95_budget_ms: float,
    cold_tile_p95_budget_ms: float,
    warm_tile_p95_budget_ms: float,
    expected_representation: str | None,
    forbid_serving: bool,
) -> None:
    failures: list[str] = []
    metadata_p95 = float(report["metadata"]["p95_ms"])
    cold_p95 = float(report["tiles"]["cold"]["p95_ms"])
    warm_p95 = float(report["tiles"]["warm"]["p95_ms"])
    if metadata_p95_budget_ms > 0 and metadata_p95 > metadata_p95_budget_ms:
        failures.append(f"metadata p95 {metadata_p95:.1f}ms exceeded budget {metadata_p95_budget_ms:.1f}ms")
    if cold_tile_p95_budget_ms > 0 and cold_p95 > cold_tile_p95_budget_ms:
        failures.append(f"cold tile p95 {cold_p95:.1f}ms exceeded budget {cold_tile_p95_budget_ms:.1f}ms")
    if warm_tile_p95_budget_ms > 0 and warm_p95 > warm_tile_p95_budget_ms:
        failures.append(f"warm tile p95 {warm_p95:.1f}ms exceeded budget {warm_tile_p95_budget_ms:.1f}ms")

    tile_records = list(report["tiles"]["cold"]["records"]) + list(report["tiles"]["warm"]["records"])
    budget_hits = [item for item in tile_records if item.get("budget_status") == "exceeded"]
    if budget_hits:
        failures.append(f"{len(budget_hits)} tile(s) exceeded the direct tile compute budget")
    if expected_representation is not None:
        unexpected = [
            item
            for item in tile_records
            if item.get("representation") != expected_representation
        ]
        if unexpected:
            failures.append(
                f"{len(unexpected)} tile(s) did not report X-Representation: {expected_representation}"
            )
    if forbid_serving:
        serving_records = [item for item in tile_records if item.get("representation") == "serving"]
        if serving_records:
            failures.append(f"{len(serving_records)} tile(s) unexpectedly used direct serving")
    if _covered_browse_zoom_expected(report):
        serving_records = [item for item in tile_records if item.get("representation") == "serving"]
        if serving_records:
            failures.append(f"{len(serving_records)} tile(s) fell back to direct serving at a covered browse zoom")

    if failures:
        raise BenchmarkFailure("; ".join(failures))


def _covered_browse_zoom_expected(report: dict[str, Any]) -> bool:
    profile = report.get("serving_profile")
    tile_set = report.get("tile_set")
    if not isinstance(profile, dict) or not isinstance(tile_set, dict):
        return False
    zoom = tile_set.get("z")
    if not isinstance(zoom, int):
        return False

    coverage = profile.get("browse_coverage")
    if isinstance(coverage, dict):
        levels = coverage.get("available_zoom_levels")
        if coverage.get("generation_status") == "complete" and isinstance(levels, list):
            return zoom in levels

    levels = profile.get("browse_overview_zoom_levels")
    modes = profile.get("supported_rendering_modes")
    return isinstance(levels, list) and zoom in levels and isinstance(modes, list) and "browse_overviews" in modes


def measure_json(label: str, url: str, timeout_seconds: float) -> TimedHttpResult:
    result = measure_bytes(url, timeout_seconds)
    result.response.json()
    return result


def measure_bytes(url: str, timeout_seconds: float, *, allow_http_errors: bool = False) -> TimedHttpResult:
    started = time.perf_counter()
    try:
        response = get_bytes(url, timeout_seconds, allow_http_errors=allow_http_errors)
    except TypeError as exc:
        if "unexpected keyword argument 'allow_http_errors'" not in str(exc):
            raise
        response = get_bytes(url, timeout_seconds)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return TimedHttpResult(url=url, elapsed_ms=elapsed_ms, response=response)


def get_bytes(url: str, timeout_seconds: float, *, allow_http_errors: bool = False) -> HttpResult:
    request = Request(url, headers={"User-Agent": "vizarr-oci-performance-benchmark/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return HttpResult(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
            )
    except HTTPError as exc:
        body = exc.read()
        maybe_skip_for_oci_auth(exc.code, body, url)
        if allow_http_errors:
            return HttpResult(
                status=exc.code,
                headers={key.lower(): value for key, value in exc.headers.items()},
                body=body,
            )
        detail = decode_error_detail(body)
        raise BenchmarkFailure(f"HTTP {exc.code} from {url}: {detail}") from exc
    except URLError as exc:
        raise BenchmarkFailure(f"could not connect to {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BenchmarkFailure(f"timed out connecting to {url}") from exc


def maybe_skip_for_oci_auth(status: int, body: bytes, url: str) -> None:
    if status not in {401, 403, 503}:
        return
    detail = decode_error_detail(body).lower()
    if any(token in detail for token in ("oci", "session", "token", "credential", "auth")):
        raise BenchmarkSkip(f"OCI auth is not available for {url}: {decode_error_detail(body)}")


def decode_error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body[:500].decode("utf-8", errors="replace")
    if isinstance(payload, dict) and payload.get("detail") is not None:
        return str(payload["detail"])
    return str(payload)


def decode_direct_tile_budget_detail(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and detail.get("error") == "direct_tile_compute_budget_exceeded":
        return detail
    return None


def timed_json_record(result: TimedHttpResult, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": result.url,
        "elapsed_ms": round(result.elapsed_ms, 3),
        "status": result.response.status,
        "content_type": result.response.headers.get("content-type"),
        "body_bytes": len(result.response.body),
    }


def header_value(result: TimedHttpResult, name: str) -> str | None:
    value = result.response.headers.get(name.lower())
    return None if value == "" else value


def header_float(result: TimedHttpResult, name: str) -> float | None:
    value = header_value(result, name)
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except ValueError:
        return None


def header_int(result: TimedHttpResult, name: str) -> int | None:
    value = header_value(result, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def is_valid_bounds(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(isinstance(item, int | float) and math.isfinite(item) for item in value):
        return False
    west, south, east, north = value
    return -180 <= west < east <= 180 and -90 <= south < north <= 90


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def percentile(values: list[float], percentile_value: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((percentile_value / 100.0) * len(ordered))
    index = max(0, min(rank - 1, len(ordered) - 1))
    return round(ordered[index], 3)


def count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value if value is not None else "missing")
        counts[key] = counts.get(key, 0) + 1
    return counts


def compact_numbers(values: Any) -> list[float]:
    return [float(value) for value in values if isinstance(value, int | float)]


def sum_ints(values: Any) -> int | None:
    numbers = [int(value) for value in values if isinstance(value, int)]
    return sum(numbers) if numbers else None


def budget_value(value: float) -> float | None:
    return value if value > 0 else None


def print_summary(report: dict[str, Any]) -> None:
    dataset_id = report["dataset_id"]
    variable_id = report["variable_id"]
    metadata_p95 = report["metadata"]["p95_ms"]
    cold = report["tiles"]["cold"]
    warm = report["tiles"]["warm"]
    print(f"OK benchmark: dataset={dataset_id} variable={variable_id}")
    print(f"  metadata p95: {metadata_p95} ms")
    print(
        "  cold tiles: "
        f"count={cold['count']} p50={cold['p50_ms']} ms p95={cold['p95_ms']} ms "
        f"representations={cold['representation_counts']} cache={cold['cache_status_counts']} "
        f"budgets={cold['budget_status_counts']}"
    )
    if cold.get("object_get_count") is not None:
        print(
            "    cold object I/O: "
            f"gets={cold['object_get_count']} ranges={cold['object_byte_range_get_count']} "
            f"bytes={cold['object_bytes_read']} chunks={cold['zarr_chunk_count']}"
        )
    print(
        "  warm tiles: "
        f"count={warm['count']} p50={warm['p50_ms']} ms p95={warm['p95_ms']} ms "
        f"representations={warm['representation_counts']} cache={warm['cache_status_counts']} "
        f"budgets={warm['budget_status_counts']}"
    )
    if warm.get("object_get_count") is not None:
        print(
            "    warm object I/O: "
            f"gets={warm['object_get_count']} ranges={warm['object_byte_range_get_count']} "
            f"bytes={warm['object_bytes_read']} chunks={warm['zarr_chunk_count']}"
        )
    frontend = report.get("frontend")
    if frontend:
        print(f"  frontend shell: {frontend['elapsed_ms']} ms")
    playwright = report.get("playwright") or {}
    print(f"  playwright: {playwright.get('status', 'missing')}")
    frontend_rendering = report.get("frontend_rendering") or {}
    print(f"  frontend rendering: {frontend_rendering.get('mode', 'unknown')} ({frontend_rendering.get('source', 'missing')})")
    rendering_modes = report.get("rendering_modes") or {}
    browser_gpu = rendering_modes.get("browser_gpu") if isinstance(rendering_modes, dict) else None
    if isinstance(browser_gpu, dict):
        print(
            "  browser GPU: "
            f"ready={browser_gpu.get('ready')} active={browser_gpu.get('active')} "
            f"status={browser_gpu.get('status')} reason={browser_gpu.get('reason')}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
