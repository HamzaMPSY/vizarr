#!/usr/bin/env python3
"""Smoke-check the OCI browser path without storing OCI secrets.

The script intentionally uses only the Python standard library so it can run
from a host checkout, CI shell, or VM handoff environment without installing
Playwright or backend dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen


DEFAULT_API_URL = "http://localhost:8001/api"
DEFAULT_FRONTEND_URL = "http://localhost:5173"
MAX_WEB_MERCATOR_LATITUDE = 85.05112878


class SmokeFailure(RuntimeError):
    """A smoke requirement failed."""


class SmokeSkip(RuntimeError):
    """The live OCI smoke path is not applicable in this environment."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure("response was not valid JSON") from exc


def main() -> int:
    args = parse_args()
    api_url = normalize_base_url(args.api_url)
    frontend_url = normalize_base_url(args.frontend_url)

    try:
        run_smoke(
            api_url=api_url,
            frontend_url=frontend_url,
            dataset_id=args.dataset_id,
            variable_id=args.variable,
            timeout_seconds=args.timeout,
            skip_frontend_check=args.skip_frontend_check,
        )
    except SmokeSkip as exc:
        print(f"SKIP: {exc}")
        return 0
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Vizarr OCI dataset discovery, tile serving, and browser shell readiness.",
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
        help="Optional OCI dataset id to smoke-check.",
    )
    parser.add_argument(
        "--variable",
        default=os.environ.get("VIZARR_OCI_VARIABLE"),
        help="Optional variable/band id to request from the selected dataset.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("VIZARR_SMOKE_TIMEOUT", "30")),
        help="HTTP timeout in seconds, default: 30.",
    )
    parser.add_argument(
        "--skip-frontend-check",
        action="store_true",
        help="Only verify backend OCI catalog and tile serving.",
    )
    return parser.parse_args()


def run_smoke(
    *,
    api_url: str,
    frontend_url: str,
    dataset_id: str | None,
    variable_id: str | None,
    timeout_seconds: float,
    skip_frontend_check: bool,
) -> None:
    health = get_json(f"{api_url}/healthz", timeout_seconds)
    if health.get("status") != "ok":
        raise SmokeFailure(f"health check returned unexpected payload: {health!r}")
    print(f"OK health: {api_url}/healthz")

    datasets = get_json(f"{api_url}/datasets", timeout_seconds)
    if not isinstance(datasets, list):
        raise SmokeFailure("/datasets did not return a list")
    dataset = select_oci_dataset(datasets, dataset_id)
    print(f"OK dataset: {dataset['id']}")

    encoded_dataset = quote(dataset["id"], safe="")
    variables = get_json(f"{api_url}/datasets/{encoded_dataset}/variables", timeout_seconds)
    if not isinstance(variables, list) or not variables:
        raise SmokeFailure(f"dataset {dataset['id']} has no variables")
    variable = select_variable(variables, variable_id)
    print(f"OK variable: {variable['id']}")

    encoded_variable = quote(variable["id"], safe="")
    tilejson = get_json(f"{api_url}/tilejson/{encoded_dataset}/{encoded_variable}", timeout_seconds)
    tile_url = resolve_tile_url(tilejson)
    print(f"OK tilejson: bounds={tilejson['bounds']} url={tile_url}")

    tile = get_bytes(tile_url, timeout_seconds)
    content_type = tile.headers.get("content-type", "")
    if tile.status != 200:
        raise SmokeFailure(f"tile returned HTTP {tile.status}: {tile_url}")
    if "image/webp" not in content_type.lower():
        raise SmokeFailure(f"tile content-type was {content_type!r}, expected image/webp")
    print(f"OK tile: {len(tile.body)} bytes image/webp")

    if not skip_frontend_check:
        frontend = get_bytes(frontend_url, timeout_seconds)
        content_type = frontend.headers.get("content-type", "")
        if frontend.status != 200:
            raise SmokeFailure(f"frontend returned HTTP {frontend.status}: {frontend_url}")
        if "text/html" not in content_type.lower():
            raise SmokeFailure(f"frontend content-type was {content_type!r}, expected text/html")
        if b'id="root"' not in frontend.body and b"id='root'" not in frontend.body:
            raise SmokeFailure("frontend shell did not contain the React root node")
        print(f"OK frontend shell: {frontend_url}")

    print_manual_browser_checks(frontend_url, dataset["id"], variable["id"])


def select_oci_dataset(datasets: list[Any], dataset_id: str | None) -> dict[str, Any]:
    typed = [item for item in datasets if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if dataset_id is not None:
        for item in typed:
            if item["id"] == dataset_id:
                if not is_oci_dataset(item):
                    raise SmokeFailure(f"dataset {dataset_id!r} is present but does not look OCI-backed")
                return item
        raise SmokeFailure(f"requested dataset {dataset_id!r} was not returned by /datasets")

    for item in typed:
        if is_oci_dataset(item):
            return item
    raise SmokeSkip("no OCI-backed dataset metadata returned by /datasets")


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
        raise SmokeFailure("variables payload had no usable variable ids")
    if variable_id is None:
        return typed[0]
    for item in typed:
        if item["id"] == variable_id:
            return item
    raise SmokeFailure(f"requested variable {variable_id!r} was not returned by /variables")


def resolve_tile_url(tilejson: Any) -> str:
    if not isinstance(tilejson, dict):
        raise SmokeFailure("TileJSON response was not an object")
    bounds = tilejson.get("bounds")
    tiles = tilejson.get("tiles")
    if not is_valid_bounds(bounds):
        raise SmokeFailure(f"TileJSON bounds are invalid: {bounds!r}")
    if not isinstance(tiles, list) or not tiles or not isinstance(tiles[0], str):
        raise SmokeFailure("TileJSON did not include a tile URL template")

    z = choose_zoom(tilejson)
    x, y = center_xyz(bounds, z)
    return tiles[0].replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


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


def is_valid_bounds(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(isinstance(item, int | float) and math.isfinite(item) for item in value):
        return False
    west, south, east, north = value
    return -180 <= west < east <= 180 and -90 <= south < north <= 90


def get_json(url: str, timeout_seconds: float) -> Any:
    return get_bytes(url, timeout_seconds).json()


def get_bytes(url: str, timeout_seconds: float) -> HttpResult:
    request = Request(url, headers={"User-Agent": "vizarr-oci-browser-smoke/1.0"})
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
        detail = decode_error_detail(body)
        raise SmokeFailure(f"HTTP {exc.code} from {url}: {detail}") from exc
    except URLError as exc:
        raise SmokeFailure(f"could not connect to {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SmokeFailure(f"timed out connecting to {url}") from exc


def maybe_skip_for_oci_auth(status: int, body: bytes, url: str) -> None:
    if status not in {401, 403, 503}:
        return
    detail = decode_error_detail(body).lower()
    if any(token in detail for token in ("oci", "session", "token", "credential", "auth")):
        raise SmokeSkip(f"OCI auth is not available for {url}: {decode_error_detail(body)}")


def decode_error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body[:500].decode("utf-8", errors="replace")
    if isinstance(payload, dict) and payload.get("detail") is not None:
        return str(payload["detail"])
    return str(payload)


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def print_manual_browser_checks(frontend_url: str, dataset_id: str, variable_id: str) -> None:
    print("Manual browser checks:")
    print(f"  1. Open {frontend_url}.")
    print(f"  2. Select dataset {dataset_id!r} and variable/composite {variable_id!r}.")
    print("  3. Confirm the map auto-fits the dataset footprint instead of staying at the default world view.")
    print("  4. Confirm either the map raster layer or the sidebar tile preview is visibly populated.")


if __name__ == "__main__":
    raise SystemExit(main())
