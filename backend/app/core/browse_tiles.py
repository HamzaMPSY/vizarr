import hashlib
import math
from collections import OrderedDict
from pathlib import Path
from threading import Lock

import numpy as np

from app.config import Settings
from app.core.colormap import encode_tile
from app.core.dataset_catalog import CatalogEntry
from app.core.projected_tile_generator import (
    WEB_MERCATOR_RADIUS,
    render_projected_band_array,
    resolve_projected_display_range,
    sample_web_mercator_array,
    xyz_to_web_mercator_bbox,
)
from app.core.oci_object_storage import OCIObjectStorageConnector


_OVERVIEW_CACHE: OrderedDict[str, tuple[np.ndarray, tuple[float, float, float, float]]] = OrderedDict()
_OVERVIEW_CACHE_LOCK = Lock()
_OVERVIEW_CACHE_MAX_ENTRIES = 16
_BUILD_LOCKS: dict[str, Lock] = {}
_BUILD_LOCKS_LOCK = Lock()


def generate_browse_tile(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    z: int,
    x: int,
    y: int,
    time_index: int,
    colormap: str,
    vmin: float | None,
    vmax: float | None,
) -> tuple[bytes, tuple[float, float]]:
    overview, overview_bbox = get_or_create_browse_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
    )
    tile_data = sample_web_mercator_array(
        overview,
        overview_bbox,
        xyz_to_web_mercator_bbox(z, x, y),
        width=256,
        height=256,
    )
    actual_vmin, actual_vmax = resolve_projected_display_range(entry, variable, tile_data, vmin, vmax)
    return encode_tile(tile_data, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)


def get_or_create_browse_overview(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    cache_path = _overview_cache_path(settings, entry, variable, time_index)
    cache_key = str(cache_path)

    cached = _overview_cache_get(cache_key)
    if cached is not None:
        return cached

    if cache_path.exists():
        loaded = _load_overview(cache_path)
        _overview_cache_set(cache_key, loaded)
        return loaded

    build_lock = _build_lock(cache_key)
    with build_lock:
        cached = _overview_cache_get(cache_key)
        if cached is not None:
            return cached
        if cache_path.exists():
            loaded = _load_overview(cache_path)
            _overview_cache_set(cache_key, loaded)
            return loaded

        built = _build_overview(
            settings=settings,
            connector=connector,
            entry=entry,
            variable=variable,
            time_index=time_index,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            cache_path,
            data=built[0],
            bbox=np.asarray(built[1], dtype=np.float64),
        )
        _overview_cache_set(cache_key, built)
        return built


def _build_overview(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    if entry.meta.bounds is None:
        raise ValueError(f"Dataset {entry.id} is missing bounds required for browse overview generation")

    overview_bbox = _wgs84_bounds_to_web_mercator(
        entry.meta.bounds.west,
        entry.meta.bounds.south,
        entry.meta.bounds.east,
        entry.meta.bounds.north,
    )
    width, height = _overview_dimensions(overview_bbox, settings.browse_overview_max_size)
    overview = render_projected_band_array(
        connector=connector,
        entry=entry,
        variable=variable,
        bbox=overview_bbox,
        width=width,
        height=height,
        time_index=time_index,
    )
    return overview, overview_bbox


def _overview_dimensions(
    bbox: tuple[float, float, float, float],
    max_size: int,
) -> tuple[int, int]:
    west, south, east, north = bbox
    width_span = max(east - west, 1.0)
    height_span = max(north - south, 1.0)
    if width_span >= height_span:
        width = max_size
        height = max(1, round(max_size * (height_span / width_span)))
    else:
        height = max_size
        width = max(1, round(max_size * (width_span / height_span)))
    return width, height


def _wgs84_bounds_to_web_mercator(
    west: float,
    south: float,
    east: float,
    north: float,
) -> tuple[float, float, float, float]:
    return (
        _lon_to_web_mercator(west),
        _lat_to_web_mercator(south),
        _lon_to_web_mercator(east),
        _lat_to_web_mercator(north),
    )


def _lon_to_web_mercator(lon: float) -> float:
    return WEB_MERCATOR_RADIUS * math.radians(lon)


def _lat_to_web_mercator(lat: float) -> float:
    clamped = max(min(lat, 85.05112878), -85.05112878)
    return WEB_MERCATOR_RADIUS * math.log(math.tan((math.pi / 4.0) + (math.radians(clamped) / 2.0)))


def _overview_cache_path(
    settings: Settings,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> Path:
    digest = hashlib.sha1(
        f"{entry.id}:{variable}:{time_index}:{settings.planner_version}:{settings.browse_overview_max_size}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return Path(settings.browse_local_cache_dir) / entry.id / f"{variable}-{time_index}-{digest}.npz"


def _load_overview(path: Path) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    with np.load(path, allow_pickle=False) as payload:
        data = payload["data"].astype(np.float32, copy=False)
        bbox = tuple(float(value) for value in payload["bbox"].tolist())
    return data, bbox


def _overview_cache_get(
    key: str,
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    with _OVERVIEW_CACHE_LOCK:
        cached = _OVERVIEW_CACHE.get(key)
        if cached is None:
            return None
        _OVERVIEW_CACHE.move_to_end(key)
        return cached


def _overview_cache_set(
    key: str,
    value: tuple[np.ndarray, tuple[float, float, float, float]],
) -> None:
    with _OVERVIEW_CACHE_LOCK:
        _OVERVIEW_CACHE[key] = value
        _OVERVIEW_CACHE.move_to_end(key)
        while len(_OVERVIEW_CACHE) > _OVERVIEW_CACHE_MAX_ENTRIES:
            _OVERVIEW_CACHE.popitem(last=False)


def _build_lock(key: str) -> Lock:
    with _BUILD_LOCKS_LOCK:
        lock = _BUILD_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _BUILD_LOCKS[key] = lock
        return lock
