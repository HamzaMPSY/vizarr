import hashlib
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from threading import Thread

import numpy as np

from app.config import Settings
from app.core.browse_artifacts import browse_manifest_contains_overview
from app.core.browse_artifacts import browse_manifest_overview_path
from app.core.browse_artifacts import browse_overview_object_path
from app.core.browse_artifacts import build_browse_manifest
from app.core.browse_artifacts import read_browse_manifest
from app.core.browse_artifacts import write_browse_manifest
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


logger = logging.getLogger(__name__)
_OVERVIEW_CACHE: OrderedDict[str, tuple[np.ndarray, tuple[float, float, float, float]]] = OrderedDict()
_OVERVIEW_CACHE_LOCK = Lock()
_OVERVIEW_CACHE_MAX_ENTRIES = 16
_BUILD_LOCKS: dict[str, Lock] = {}
_BUILD_LOCKS_LOCK = Lock()


@dataclass(frozen=True)
class BrowseTileResult:
    tile_bytes: bytes
    display_range: tuple[float, float]
    source: str


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
) -> BrowseTileResult:
    overview, overview_bbox, source = get_or_create_browse_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
        allow_build=settings.browse_request_build_enabled,
    )
    tile_data = sample_web_mercator_array(
        overview,
        overview_bbox,
        xyz_to_web_mercator_bbox(z, x, y),
        width=256,
        height=256,
    )
    actual_vmin, actual_vmax = resolve_projected_display_range(entry, variable, tile_data, vmin, vmax)
    return BrowseTileResult(
        tile_bytes=encode_tile(tile_data, colormap, actual_vmin, actual_vmax),
        display_range=(actual_vmin, actual_vmax),
        source=source,
    )


def prewarm_browse_overviews(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    catalog: dict[str, CatalogEntry],
    all_variables: bool = False,
) -> int:
    warmed = 0
    for entry in catalog.values():
        if not entry.meta.variables:
            continue
        variables = [item.id for item in entry.meta.variables]
        if not all_variables:
            variables = variables[:1]
        for variable in variables:
            if browse_overview_exists(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable,
                time_index=0,
            ):
                continue
            get_or_create_browse_overview(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable,
                time_index=0,
            )
            warmed += 1
            logger.info("Prewarmed browse overview for %s variable %s", entry.id, variable)
    return warmed


def start_background_browse_prewarm(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    catalog: dict[str, CatalogEntry],
) -> Thread:
    thread = Thread(
        target=_run_background_browse_prewarm,
        args=(settings, connector, catalog),
        daemon=True,
        name="browse-prewarm",
    )
    thread.start()
    return thread


def browse_overview_exists(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> bool:
    cache_path = _overview_cache_path(settings, entry, variable, time_index)
    if cache_path.exists():
        return True
    if _overview_cache_get(str(cache_path)) is not None:
        return True
    manifest = read_browse_manifest(connector, settings, entry)
    return browse_manifest_contains_overview(manifest, variable=variable, time_index=time_index)


def _run_background_browse_prewarm(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    catalog: dict[str, CatalogEntry],
) -> None:
    try:
        warmed = prewarm_browse_overviews(
            settings=settings,
            connector=connector,
            catalog=catalog,
            all_variables=True,
        )
        logger.info("Background browse prewarm finished with %d generated overview(s)", warmed)
    except Exception:
        logger.exception("Background browse prewarm failed")


def get_or_create_browse_overview(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    allow_build: bool = True,
) -> tuple[np.ndarray, tuple[float, float, float, float], str]:
    cache_path = _overview_cache_path(settings, entry, variable, time_index)
    cache_key = str(cache_path)

    cached = _overview_cache_get(cache_key)
    if cached is not None:
        return cached[0], cached[1], "memory"

    if cache_path.exists():
        loaded = _load_overview(cache_path)
        _overview_cache_set(cache_key, loaded)
        return loaded[0], loaded[1], "local"

    manifest = read_browse_manifest(connector, settings, entry)
    overview_path = browse_manifest_overview_path(manifest, variable=variable, time_index=time_index)
    if overview_path is not None:
        try:
            loaded = _load_overview_from_object_storage(
                connector=connector,
                object_path=overview_path,
                cache_path=cache_path,
            )
        except FileNotFoundError:
            loaded = None
        if loaded is not None:
            _overview_cache_set(cache_key, loaded)
            return loaded[0], loaded[1], "oci"

    build_lock = _build_lock(cache_key)
    with build_lock:
        cached = _overview_cache_get(cache_key)
        if cached is not None:
            return cached[0], cached[1], "memory"
        if cache_path.exists():
            loaded = _load_overview(cache_path)
            _overview_cache_set(cache_key, loaded)
            return loaded[0], loaded[1], "local"

        manifest = read_browse_manifest(connector, settings, entry)
        overview_path = browse_manifest_overview_path(manifest, variable=variable, time_index=time_index)
        if overview_path is not None:
            try:
                loaded = _load_overview_from_object_storage(
                    connector=connector,
                    object_path=overview_path,
                    cache_path=cache_path,
                )
            except FileNotFoundError:
                loaded = None
            if loaded is not None:
                _overview_cache_set(cache_key, loaded)
                return loaded[0], loaded[1], "oci"

        if not allow_build or not settings.browse_dev_fallback_enabled:
            raise FileNotFoundError(
                f"No durable browse overview is available for dataset={entry.id} variable={variable} time_index={time_index}"
            )

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
        return built[0], built[1], "generated"


def build_and_store_browse_overviews(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variables: list[str],
    time_indices: list[int],
    overwrite: bool = False,
) -> dict[str, object]:
    manifest = read_browse_manifest(connector, settings, entry, use_cache=False) or build_browse_manifest(
        settings,
        entry,
        {},
    )
    variables_payload = dict(manifest.get("variables", {}))
    generated = 0
    reused = 0

    for variable in variables:
        variable_payload = dict(variables_payload.get(variable, {}))
        overviews = dict(variable_payload.get("overviews", {}))
        for time_index in time_indices:
            object_path = browse_overview_object_path(settings, entry, variable, time_index)
            exists = connector.object_exists(object_path)
            if exists and not overwrite:
                reused += 1
                overviews.setdefault(str(time_index), {"path": object_path})
                continue

            built = _build_overview(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable,
                time_index=time_index,
            )
            connector.write_bytes(
                object_path,
                _serialize_overview(*built),
                content_type="application/octet-stream",
            )
            cache_path = _overview_cache_path(settings, entry, variable, time_index)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                cache_path,
                data=built[0],
                bbox=np.asarray(built[1], dtype=np.float64),
            )
            _overview_cache_set(str(cache_path), built)
            overviews[str(time_index)] = {
                "path": object_path,
                "bbox": [float(value) for value in built[1]],
            }
            generated += 1

        variable_payload["overviews"] = overviews
        variables_payload[variable] = variable_payload

    manifest = build_browse_manifest(settings, entry, variables_payload)
    manifest_path = write_browse_manifest(connector, settings, entry, manifest)
    return {
        "manifest_path": manifest_path,
        "generated": generated,
        "reused": reused,
        "variables": variables,
        "time_indices": time_indices,
    }


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


def _load_overview_from_object_storage(
    *,
    connector: OCIObjectStorageConnector,
    object_path: str,
    cache_path: Path,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    payload = connector.read_bytes(object_path, use_cache=True)
    loaded = _deserialize_overview(payload)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return loaded


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


def _serialize_overview(
    data: np.ndarray,
    bbox: tuple[float, float, float, float],
) -> bytes:
    payload = BytesIO()
    np.savez_compressed(
        payload,
        data=data,
        bbox=np.asarray(bbox, dtype=np.float64),
    )
    return payload.getvalue()


def _deserialize_overview(payload: bytes) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    with np.load(BytesIO(payload), allow_pickle=False) as data:
        array = data["data"].astype(np.float32, copy=False)
        bbox = tuple(float(value) for value in data["bbox"].tolist())
    return array, bbox
