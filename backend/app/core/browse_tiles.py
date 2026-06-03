import hashlib
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from threading import Thread
from typing import Callable

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
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.projected_tile_generator import (
    TILE_SIZE,
    WEB_MERCATOR_HALF_WORLD,
    WEB_MERCATOR_RADIUS,
    _is_fast_latlon_entry,
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
_BROWSE_OVERVIEW_SAMPLES_PER_SHARD_AXIS = 2.0
_MIN_OVERVIEW_MOSAIC_TILES = 16
_MAX_OVERVIEW_MOSAIC_TILES = 64
_MAX_OVERVIEW_MOSAIC_EXTRA_ZOOM = 4


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
    browse_zoom = _resolved_browse_zoom(settings, z)
    overview, overview_bbox, source = get_or_create_browse_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
        zoom=browse_zoom,
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
                zoom=settings.browse_tile_max_zoom,
                exact=True,
            ):
                continue
            get_or_create_browse_overview(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable,
                time_index=0,
                zoom=settings.browse_tile_max_zoom,
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
    zoom: int,
    exact: bool = False,
    max_zoom_override: int | None = None,
) -> bool:
    resolved_zoom = _resolved_browse_zoom(settings, zoom, max_zoom_override=max_zoom_override)
    cache_path = _overview_cache_path(settings, entry, variable, time_index, resolved_zoom)
    if cache_path.exists():
        return True
    if _overview_cache_get(str(cache_path)) is not None:
        return True
    manifest = read_browse_manifest(connector, settings, entry)
    if exact:
        available_levels = _available_manifest_zoom_levels(
            manifest,
            variable=variable,
            time_index=time_index,
        )
        return resolved_zoom in available_levels
    if browse_manifest_contains_overview(manifest, variable=variable, time_index=time_index, zoom=resolved_zoom):
        return True
    return bool(_available_manifest_zoom_levels(manifest, variable=variable, time_index=time_index))


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


def _resolved_browse_zoom(settings: Settings, zoom: int, *, max_zoom_override: int | None = None) -> int:
    ceiling = _browse_zoom_ceiling(settings, max_zoom_override=max_zoom_override)
    return max(0, min(int(zoom), ceiling))


def _default_browse_zoom_levels(settings: Settings, *, max_zoom_override: int | None = None) -> list[int]:
    return list(range(0, _browse_zoom_ceiling(settings, max_zoom_override=max_zoom_override) + 1))


def _browse_zoom_ceiling(settings: Settings, *, max_zoom_override: int | None = None) -> int:
    ceiling = int(settings.browse_tile_max_zoom)
    if max_zoom_override is None:
        return ceiling
    return max(ceiling, int(max_zoom_override))


def _available_manifest_zoom_levels(
    manifest: dict[str, object] | None,
    *,
    variable: str,
    time_index: int,
) -> list[int]:
    if manifest is None:
        return []
    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        return []
    variable_entry = variables.get(variable)
    if not isinstance(variable_entry, dict):
        return []
    overviews = variable_entry.get("overviews")
    if not isinstance(overviews, dict):
        return []
    overview_entry = overviews.get(str(time_index))
    if not isinstance(overview_entry, dict):
        return []
    levels = overview_entry.get("levels")
    if not isinstance(levels, dict):
        return []
    parsed: list[int] = []
    for level in levels:
        try:
            parsed.append(int(level))
        except ValueError:
            continue
    return sorted(set(parsed))


def _browse_manifest_best_overview_path(
    manifest: dict[str, object] | None,
    *,
    variable: str,
    time_index: int,
    zoom: int,
) -> str | None:
    exact = browse_manifest_overview_path(
        manifest,
        variable=variable,
        time_index=time_index,
        zoom=zoom,
    )
    if exact is not None:
        return exact
    available_levels = _available_manifest_zoom_levels(
        manifest,
        variable=variable,
        time_index=time_index,
    )
    if not available_levels:
        return None
    lower_or_equal = [level for level in available_levels if level <= zoom]
    selected_zoom = max(lower_or_equal) if lower_or_equal else min(available_levels)
    return browse_manifest_overview_path(
        manifest,
        variable=variable,
        time_index=time_index,
        zoom=selected_zoom,
    )


def get_or_create_browse_overview(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
    allow_build: bool = True,
    max_zoom_override: int | None = None,
) -> tuple[np.ndarray, tuple[float, float, float, float], str]:
    resolved_zoom = _resolved_browse_zoom(settings, zoom, max_zoom_override=max_zoom_override)
    cache_path = _overview_cache_path(settings, entry, variable, time_index, resolved_zoom)
    cache_key = str(cache_path)

    cached = _overview_cache_get(cache_key)
    if cached is not None:
        return cached[0], cached[1], "memory"

    if cache_path.exists():
        loaded = _load_overview(cache_path)
        _overview_cache_set(cache_key, loaded)
        return loaded[0], loaded[1], "local"

    manifest = read_browse_manifest(connector, settings, entry)
    overview_path = _browse_manifest_best_overview_path(
        manifest,
        variable=variable,
        time_index=time_index,
        zoom=resolved_zoom,
    )
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
        overview_path = _browse_manifest_best_overview_path(
            manifest,
            variable=variable,
            time_index=time_index,
            zoom=resolved_zoom,
        )
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
            zoom=resolved_zoom,
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
    zoom_levels: list[int] | None = None,
    overwrite: bool = False,
    max_zoom_override: int | None = None,
    progress_callback: Callable[[bool], None] | None = None,
) -> dict[str, object]:
    manifest = read_browse_manifest(connector, settings, entry, use_cache=False) or build_browse_manifest(
        settings,
        entry,
        {},
    )
    variables_payload = dict(manifest.get("variables", {}))
    generated = 0
    reused = 0
    requested_zoom_levels = [
        _resolved_browse_zoom(settings, zoom, max_zoom_override=max_zoom_override)
        for zoom in (zoom_levels or _default_browse_zoom_levels(settings, max_zoom_override=max_zoom_override))
    ]
    generation_zoom_levels = sorted(set(requested_zoom_levels), reverse=True)

    for variable in variables:
        variable_payload = dict(variables_payload.get(variable, {}))
        overviews = dict(variable_payload.get("overviews", {}))
        for time_index in time_indices:
            overview_payload = dict(overviews.get(str(time_index), {}))
            levels_payload = dict(overview_payload.get("levels", {}))
            base_overview: tuple[np.ndarray, tuple[float, float, float, float]] | None = None
            base_zoom: int | None = None
            for zoom in generation_zoom_levels:
                object_path = browse_overview_object_path(settings, entry, variable, time_index, zoom)
                exists = connector.object_exists(object_path)
                if exists and not overwrite:
                    logger.info(
                        "Reusing browse overview for %s variable=%s time_index=%d z=%d",
                        entry.id,
                        variable,
                        time_index,
                        zoom,
                    )
                    reused += 1
                    if progress_callback is not None:
                        progress_callback(False)
                    levels_payload.setdefault(str(zoom), {"path": object_path})
                    if base_overview is None:
                        base_overview = _materialize_overview_level(
                            settings=settings,
                            connector=connector,
                            entry=entry,
                            variable=variable,
                            time_index=time_index,
                            zoom=zoom,
                            object_path=object_path,
                            allow_build=False,
                        )
                        if base_overview is not None:
                            base_zoom = zoom
                    continue

                if base_overview is not None and base_zoom is not None and zoom < base_zoom:
                    logger.info(
                        "Deriving browse overview for %s variable=%s time_index=%d z=%d from z=%d",
                        entry.id,
                        variable,
                        time_index,
                        zoom,
                        base_zoom,
                    )
                    built = _derive_overview_from_base(
                        settings=settings,
                        base=base_overview,
                        zoom=zoom,
                    )
                else:
                    logger.info(
                        "Building browse overview for %s variable=%s time_index=%d z=%d",
                        entry.id,
                        variable,
                        time_index,
                        zoom,
                    )
                    built = _build_overview(
                        settings=settings,
                        connector=connector,
                        entry=entry,
                        variable=variable,
                        time_index=time_index,
                        zoom=zoom,
                    )
                    if base_zoom is None or zoom >= base_zoom:
                        base_overview = built
                        base_zoom = zoom

                _write_overview_level(
                    connector=connector,
                    object_path=object_path,
                    cache_path=_overview_cache_path(settings, entry, variable, time_index, zoom),
                    built=built,
                )
                logger.info(
                    "Stored browse overview for %s variable=%s time_index=%d z=%d at %s",
                    entry.id,
                    variable,
                    time_index,
                    zoom,
                    object_path,
                )
                levels_payload[str(zoom)] = {
                    "path": object_path,
                    "bbox": [float(value) for value in built[1]],
                    "zoom": zoom,
                }
                generated += 1
                if progress_callback is not None:
                    progress_callback(True)

            if levels_payload:
                overview_payload["levels"] = levels_payload
                lowest_zoom = min(int(value) for value in levels_payload)
                highest_zoom = max(int(value) for value in levels_payload)
                overview_payload["path"] = levels_payload[str(lowest_zoom)]["path"]
                overview_payload["max_zoom_path"] = levels_payload[str(highest_zoom)]["path"]
                overviews[str(time_index)] = overview_payload

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
        "zoom_levels": requested_zoom_levels,
    }


def _build_overview(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    if entry.meta.bounds is None:
        raise ValueError(f"Dataset {entry.id} is missing bounds required for browse overview generation")
    entry = ensure_catalog_entry_metadata_ready(entry, connector)

    overview_bbox = _wgs84_bounds_to_web_mercator(
        entry.meta.bounds.west,
        entry.meta.bounds.south,
        entry.meta.bounds.east,
        entry.meta.bounds.north,
    )
    width, height = _overview_dimensions(overview_bbox, settings.browse_overview_max_size, zoom)
    source_oversample = _browse_overview_source_oversample(
        entry,
        width=width,
        height=height,
    )
    if _is_fast_latlon_entry(entry):
        overview = render_projected_band_array(
            connector=connector,
            entry=entry,
            variable=variable,
            bbox=overview_bbox,
            width=width,
            height=height,
            time_index=time_index,
            max_source_oversample=source_oversample,
            max_parallel_chunk_reads=1,
        )
        return overview, overview_bbox

    mosaic_plan = _select_mosaic_tile_range(overview_bbox, zoom)
    if mosaic_plan is not None:
        render_zoom, tile_range = mosaic_plan
        overview = _build_overview_from_tile_mosaic(
            connector=connector,
            entry=entry,
            variable=variable,
            time_index=time_index,
            render_zoom=render_zoom,
            overview_bbox=overview_bbox,
            width=width,
            height=height,
            tile_range=tile_range,
        )
        return overview, overview_bbox

    overview = render_projected_band_array(
        connector=connector,
        entry=entry,
        variable=variable,
        bbox=overview_bbox,
        width=width,
        height=height,
        time_index=time_index,
        max_source_oversample=source_oversample,
        max_parallel_chunk_reads=1,
    )
    return overview, overview_bbox


def _build_overview_from_tile_mosaic(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    render_zoom: int,
    overview_bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    tile_range: tuple[int, int, int, int],
) -> np.ndarray:
    min_x, max_x, min_y, max_y = tile_range
    tiles_wide = max_x - min_x + 1
    tiles_high = max_y - min_y + 1
    mosaic = np.full((tiles_high * TILE_SIZE, tiles_wide * TILE_SIZE), np.nan, dtype=np.float32)

    top_left_bbox = xyz_to_web_mercator_bbox(render_zoom, min_x, min_y)
    bottom_right_bbox = xyz_to_web_mercator_bbox(render_zoom, max_x, max_y)
    mosaic_bbox = (
        top_left_bbox[0],
        bottom_right_bbox[1],
        bottom_right_bbox[2],
        top_left_bbox[3],
    )
    tile_source_oversample = _browse_overview_source_oversample(
        entry,
        width=TILE_SIZE,
        height=TILE_SIZE,
    )

    for tile_y in range(min_y, max_y + 1):
        for tile_x in range(min_x, max_x + 1):
            tile_bbox = xyz_to_web_mercator_bbox(render_zoom, tile_x, tile_y)
            tile = render_projected_band_array(
                connector=connector,
                entry=entry,
                variable=variable,
                bbox=tile_bbox,
                width=TILE_SIZE,
                height=TILE_SIZE,
                time_index=time_index,
                max_source_oversample=tile_source_oversample,
                max_parallel_chunk_reads=1,
            )
            row_offset = (tile_y - min_y) * TILE_SIZE
            col_offset = (tile_x - min_x) * TILE_SIZE
            mosaic[
                row_offset : row_offset + TILE_SIZE,
                col_offset : col_offset + TILE_SIZE,
            ] = tile

    return sample_web_mercator_array(
        mosaic,
        mosaic_bbox,
        overview_bbox,
        width=width,
        height=height,
    )


def _derive_overview_from_base(
    *,
    settings: Settings,
    base: tuple[np.ndarray, tuple[float, float, float, float]],
    zoom: int,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    base_data, base_bbox = base
    width, height = _overview_dimensions(base_bbox, settings.browse_overview_max_size, zoom)
    derived = sample_web_mercator_array(
        base_data,
        base_bbox,
        base_bbox,
        width=width,
        height=height,
    )
    return derived, base_bbox


def _overview_dimensions(
    bbox: tuple[float, float, float, float],
    max_size: int,
    zoom: int,
) -> tuple[int, int]:
    west, south, east, north = bbox
    world_span = 2.0 * math.pi * WEB_MERCATOR_RADIUS
    pixels_per_world = 256 * (2**zoom)
    width = max(1, math.ceil(max(east - west, 1.0) / world_span * pixels_per_world))
    height = max(1, math.ceil(max(north - south, 1.0) / world_span * pixels_per_world))
    max_dimension = max(width, height)
    if max_dimension > max_size:
        scale = max_size / max_dimension
        width = max(1, math.ceil(width * scale))
        height = max(1, math.ceil(height * scale))
    return width, height


def _browse_overview_source_oversample(
    entry: CatalogEntry,
    *,
    width: int,
    height: int,
) -> float:
    metadata = entry.data_array_meta
    if metadata is None or metadata.sharding is None:
        return 1.0

    source_width = int(metadata.shape[-1])
    source_height = int(metadata.shape[-2])
    shard_width = int(metadata.chunk_shape[-1])
    shard_height = int(metadata.chunk_shape[-2])
    if shard_width <= 0 or shard_height <= 0:
        return 1.0

    shard_columns = math.ceil(source_width / shard_width)
    shard_rows = math.ceil(source_height / shard_height)
    x_ratio = (_BROWSE_OVERVIEW_SAMPLES_PER_SHARD_AXIS * shard_columns) / max(width, 1)
    y_ratio = (_BROWSE_OVERVIEW_SAMPLES_PER_SHARD_AXIS * shard_rows) / max(height, 1)
    return max(min(max(x_ratio, y_ratio), 1.0), 0.05)


def _select_mosaic_tile_range(
    bbox: tuple[float, float, float, float],
    zoom: int,
) -> tuple[int, tuple[int, int, int, int]] | None:
    best: tuple[int, tuple[int, int, int, int]] | None = None
    for candidate_zoom in range(zoom, zoom + _MAX_OVERVIEW_MOSAIC_EXTRA_ZOOM + 1):
        tile_range = _intersecting_xyz_tile_range(bbox, candidate_zoom)
        if tile_range is None:
            return best

        min_x, max_x, min_y, max_y = tile_range
        tile_count = (max_x - min_x + 1) * (max_y - min_y + 1)
        if tile_count > _MAX_OVERVIEW_MOSAIC_TILES:
            return best

        best = (candidate_zoom, tile_range)
        if tile_count >= _MIN_OVERVIEW_MOSAIC_TILES:
            return best

    return best


def _intersecting_xyz_tile_range(
    bbox: tuple[float, float, float, float],
    zoom: int,
) -> tuple[int, int, int, int] | None:
    west, south, east, north = bbox
    clamped_west = max(-WEB_MERCATOR_HALF_WORLD, min(WEB_MERCATOR_HALF_WORLD, west))
    clamped_east = max(-WEB_MERCATOR_HALF_WORLD, min(WEB_MERCATOR_HALF_WORLD, east))
    clamped_south = max(-WEB_MERCATOR_HALF_WORLD, min(WEB_MERCATOR_HALF_WORLD, south))
    clamped_north = max(-WEB_MERCATOR_HALF_WORLD, min(WEB_MERCATOR_HALF_WORLD, north))
    if clamped_east <= clamped_west or clamped_north <= clamped_south:
        return None

    tile_limit = 2**zoom
    min_x = _clamp_tile_index(_mercator_x_to_tile_index(clamped_west, zoom), tile_limit)
    max_x = _clamp_tile_index(_mercator_x_to_tile_index(math.nextafter(clamped_east, -math.inf), zoom), tile_limit)
    min_y = _clamp_tile_index(_mercator_y_to_tile_index(clamped_north, zoom), tile_limit)
    max_y = _clamp_tile_index(_mercator_y_to_tile_index(math.nextafter(clamped_south, math.inf), zoom), tile_limit)
    return min_x, max_x, min_y, max_y


def _mercator_x_to_tile_index(value: float, zoom: int) -> int:
    world_span = 2.0 * WEB_MERCATOR_HALF_WORLD
    return int(math.floor(((value + WEB_MERCATOR_HALF_WORLD) / world_span) * (2**zoom)))


def _mercator_y_to_tile_index(value: float, zoom: int) -> int:
    world_span = 2.0 * WEB_MERCATOR_HALF_WORLD
    return int(math.floor(((WEB_MERCATOR_HALF_WORLD - value) / world_span) * (2**zoom)))


def _clamp_tile_index(index: int, tile_limit: int) -> int:
    return max(0, min(tile_limit - 1, index))


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
    zoom: int,
) -> Path:
    digest = hashlib.sha1(
        f"{entry.id}:{variable}:{time_index}:{zoom}:{settings.planner_version}:{settings.browse_overview_max_size}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return Path(settings.browse_local_cache_dir) / entry.id / f"{variable}-{time_index}-z{zoom}-{digest}.npz"


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


def _materialize_overview_level(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
    object_path: str,
    allow_build: bool,
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    cache_path = _overview_cache_path(settings, entry, variable, time_index, zoom)
    cache_key = str(cache_path)

    cached = _overview_cache_get(cache_key)
    if cached is not None:
        return cached

    if cache_path.exists():
        loaded = _load_overview(cache_path)
        _overview_cache_set(cache_key, loaded)
        return loaded

    if connector.object_exists(object_path):
        try:
            loaded = _load_overview_from_object_storage(
                connector=connector,
                object_path=object_path,
                cache_path=cache_path,
            )
        except FileNotFoundError:
            loaded = None
        if loaded is not None:
            _overview_cache_set(cache_key, loaded)
            return loaded

    if not allow_build:
        return None

    built = _build_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
        zoom=zoom,
    )
    _write_overview_level(
        connector=connector,
        object_path=object_path,
        cache_path=cache_path,
        built=built,
    )
    return built


def _write_overview_level(
    *,
    connector: OCIObjectStorageConnector,
    object_path: str,
    cache_path: Path,
    built: tuple[np.ndarray, tuple[float, float, float, float]],
) -> None:
    payload = _serialize_overview(*built)
    connector.write_bytes(
        object_path,
        payload,
        content_type="application/octet-stream",
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    _overview_cache_set(str(cache_path), built)


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
