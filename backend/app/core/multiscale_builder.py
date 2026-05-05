from __future__ import annotations

from io import BytesIO
import logging
import math
from typing import Any

import numpy as np
from pyproj import CRS
import zarr

from app.config import Settings
from app.core.browse_tiles import get_or_create_browse_overview
from app.core.browse_tiles import build_and_store_browse_overviews
from app.core.browse_tiles import sample_web_mercator_array
from app.core.browse_artifacts import browse_manifest_overview_path
from app.core.browse_artifacts import read_browse_manifest
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.multiscale_store import multiscale_store_path
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.projected_tile_generator import TILE_SIZE
from app.core.projected_tile_generator import render_projected_band_array
from app.core.projected_tile_generator import xyz_to_web_mercator_bbox
from app.core.zarr_v3 import load_1d_numeric_array
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import load_4d_window_decimated
from app.core.zarr_v3 import parse_array_metadata
from app.core.zarr_v3 import read_consolidated_metadata
from app.core.zarr_v3 import read_store_metadata


logger = logging.getLogger(__name__)

_MAX_PYRAMID_TILES_PER_LEVEL = 4096
_MAX_PYRAMID_ZOOM = 18
_DEFAULT_PREPOPULATE_TILE_BUDGET = 128


def build_and_store_multiscale_pyramid(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    *,
    overwrite: bool = False,
    zarr_format: int = 2,
    chunk_size: int = 256,
    min_size: int = 256,
    max_browser_dimension: int = 4096,
    full_resolution: bool = False,
    output_dtype: str = "float32",
    prepopulate_through_zoom: int | None = None,
    prepopulate_tile_budget: int = _DEFAULT_PREPOPULATE_TILE_BUDGET,
    max_zoom: int | None = None,
) -> dict[str, Any]:
    ensure_catalog_entry_metadata_ready(entry, connector)
    _validate_entry(entry)
    if zarr_format != 2:
        raise ValueError("Tile-aligned multiscale generation currently requires zarr_format=2")

    output_store_path = multiscale_store_path(settings, entry.path)
    if output_store_path is None:
        raise ValueError("OCI_MULTISCALE_PREFIX_ROOT must be configured to build a multiscale store")

    if entry.data_array_meta is None or entry.x_meta is None or entry.y_meta is None:
        raise ValueError(f"Dataset {entry.id} is missing source metadata needed for multiscale generation")

    source_store_metadata, metadata = _read_dataset_metadata(connector, entry.path)
    data_node = metadata.get(entry.data_array_name, {})
    x_node = metadata.get("x", {})
    y_node = metadata.get("y", {})
    time_node = metadata.get("time", {})
    spatial_ref_node = metadata.get("spatial_ref", {})

    x_values = load_1d_numeric_array(
        connector=connector,
        store_path=entry.path,
        array_name="x",
        metadata=entry.x_meta,
    ).astype(np.float64, copy=False)
    y_values = load_1d_numeric_array(
        connector=connector,
        store_path=entry.path,
        array_name="y",
        metadata=entry.y_meta,
    ).astype(np.float64, copy=False)
    time_values, time_attrs = _load_time_values(entry, connector, time_node)
    band_values, band_attrs = _build_band_coordinate(entry)

    filesystem = connector.get_filesystem()
    mapper_path = connector.build_oci_uri(output_store_path).removeprefix("oci://")
    if filesystem.exists(mapper_path):
        if not overwrite:
            raise ValueError(f"Output multiscale store already exists: {output_store_path}")
        logger.info("Removing existing multiscale store before overwrite: %s", output_store_path)
        filesystem.rm(mapper_path, recursive=True)

    mapper = filesystem.get_mapper(mapper_path)
    root = zarr.open_group(store=mapper, mode="w", zarr_format=zarr_format)
    dtype = np.dtype(output_dtype)
    zoom_levels = list(
        range(
            _minimum_pyramid_zoom(settings),
            _maximum_pyramid_zoom(settings, entry, explicit_max_zoom=max_zoom) + 1,
        )
    )
    prepopulated_zoom_max = _resolve_prepopulated_zoom_max(
        entry=entry,
        zoom_levels=zoom_levels,
        explicit_max_zoom=prepopulate_through_zoom,
        tile_budget=prepopulate_tile_budget,
    )
    root.attrs.update(
        _build_root_attributes(
            data_array_name=entry.data_array_name,
            dataset_paths=[str(zoom) for zoom in zoom_levels],
            zoom_levels=zoom_levels,
        )
    )
    root.attrs["source_representation"] = "tile_pyramid"
    root.attrs["tile_size"] = TILE_SIZE
    root.attrs["max_zoom"] = max(zoom_levels) if zoom_levels else None
    root.attrs["population_strategy"] = (
        "lazy_on_demand"
        if prepopulated_zoom_max is None
        else "prepopulated_then_lazy"
    )
    root.attrs["prepopulated_zoom_max"] = prepopulated_zoom_max

    level_shapes: list[list[int]] = []
    tile_ranges: dict[str, dict[str, int]] = {}
    level_arrays: dict[int, Any] = {}
    band_ids = _band_ids_from_entry(entry)
    for zoom in zoom_levels:
        tile_x_min, tile_x_max, tile_y_min, tile_y_max = _mercator_tile_range_for_bounds(entry.meta.bounds, zoom)
        tile_count_x = (tile_x_max - tile_x_min) + 1
        tile_count_y = (tile_y_max - tile_y_min) + 1
        level_height = tile_count_y * TILE_SIZE
        level_width = tile_count_x * TILE_SIZE
        logger.info(
            "Building tile pyramid level z=%d for %s (%d x %d tiles)",
            zoom,
            entry.id,
            tile_count_y,
            tile_count_x,
        )
        group = root.create_group(str(zoom), overwrite=True)
        level_bbox = _level_bbox_from_tile_range(
            zoom=zoom,
            tile_x_min=tile_x_min,
            tile_x_max=tile_x_max,
            tile_y_min=tile_y_min,
            tile_y_max=tile_y_max,
        )
        group.attrs.update(
            {
                "source_store_path": entry.path,
                "source_representation": "tile_pyramid",
                "zoom": zoom,
                "tile_size": TILE_SIZE,
                "tile_x_min": tile_x_min,
                "tile_x_max": tile_x_max,
                "tile_y_min": tile_y_min,
                "tile_y_max": tile_y_max,
                "bbox_epsg3857": list(level_bbox),
            }
        )

        data_array = _create_data_array(
            group=group,
            name=entry.data_array_name,
            shape=(len(time_values), len(band_values), level_height, level_width),
            chunk_size=chunk_size,
            dtype=dtype,
            zarr_format=zarr_format,
            attributes=dict(data_node.get("attributes", {})),
        )
        level_arrays[zoom] = data_array
        _create_coord_array(
            group=group,
            name="time",
            values=time_values,
            dimensions=("time",),
            zarr_format=zarr_format,
            attributes=time_attrs,
        )
        _create_coord_array(
            group=group,
            name="band",
            values=band_values,
            dimensions=("band",),
            zarr_format=zarr_format,
            attributes={**band_attrs, "band_labels": band_ids},
        )
        _create_spatial_ref_array(
            group=group,
            zarr_format=zarr_format,
            attributes=dict(spatial_ref_node.get("attributes", {})),
        )

        level_shapes.append([level_height, level_width])
        tile_ranges[str(zoom)] = {
            "tile_x_min": tile_x_min,
            "tile_x_max": tile_x_max,
            "tile_y_min": tile_y_min,
            "tile_y_max": tile_y_max,
        }

    if prepopulated_zoom_max is not None:
        _prepopulate_pyramid_levels(
            settings=settings,
            connector=connector,
            entry=entry,
            level_arrays=level_arrays,
            tile_ranges=tile_ranges,
            prepopulated_zoom_max=prepopulated_zoom_max,
            target_dtype=dtype,
        )

    zarr.consolidate_metadata(mapper, zarr_format=zarr_format)
    return {
        "dataset_id": entry.id,
        "source_store_path": entry.path,
        "output_store_path": output_store_path,
        "zarr_format": zarr_format,
        "levels": zoom_levels,
        "level_shapes": level_shapes,
        "tile_ranges": tile_ranges,
        "chunk_size": chunk_size,
        "dtype": dtype.name,
        "level_zero_source_decimation_factor": None,
        "source_representation": "tile_pyramid",
        "population_strategy": root.attrs["population_strategy"],
        "prepopulated_zoom_max": prepopulated_zoom_max,
        "prepopulate_tile_budget": prepopulate_tile_budget,
        "max_zoom": root.attrs["max_zoom"],
        "source_zarr_format": int(source_store_metadata.get("zarr_format", 0) or 0),
    }


def _validate_entry(entry: CatalogEntry) -> None:
    source_crs = CRS.from_wkt(entry.crs_wkt) if entry.crs_wkt else CRS.from_epsg(4326)
    if source_crs != CRS.from_epsg(4326):
        raise ValueError(
            "Multiscale generation currently supports only EPSG:4326 source datasets; "
            f"got {source_crs.to_string()}"
        )
    if entry.data_array_meta is None or len(entry.data_array_meta.shape) != 4:
        raise ValueError("Multiscale generation requires a 4D time/band/y/x source array")


def _read_dataset_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_metadata, metadata = read_consolidated_metadata(
        connector=connector,
        store_path=store_path,
    )
    if metadata:
        return store_metadata, metadata
    return read_store_metadata(connector=connector, store_path=store_path)


def _load_time_values(
    entry: CatalogEntry,
    connector: OCIObjectStorageConnector,
    time_node: dict[str, Any],
) -> tuple[np.ndarray, dict[str, Any]]:
    if time_node:
        time_meta = parse_array_metadata(time_node)
        values = load_1d_numeric_array(
            connector=connector,
            store_path=entry.path,
            array_name="time",
            metadata=time_meta,
        )
        return values, dict(time_node.get("attributes", {}))
    if entry.data_array_meta is None:
        return np.asarray([0], dtype=np.int32), {}
    return np.arange(entry.data_array_meta.shape[0], dtype=np.int32), {}


def _build_band_coordinate(entry: CatalogEntry) -> tuple[np.ndarray, dict[str, Any]]:
    labels = _band_ids_from_entry(entry)
    attributes: dict[str, Any] = {}
    if labels:
        attributes["band_labels"] = labels
    return np.arange(len(labels), dtype=np.int32), attributes


def _band_ids_from_entry(entry: CatalogEntry) -> list[str]:
    variable_ids = [item.id for item in entry.meta.variables]
    if variable_ids:
        return variable_ids
    return list(entry.band_names)


def _minimum_pyramid_zoom(settings: Settings) -> int:
    return 0


def _resolve_prepopulated_zoom_max(
    *,
    entry: CatalogEntry,
    zoom_levels: list[int],
    explicit_max_zoom: int | None,
    tile_budget: int,
) -> int | None:
    if not zoom_levels or tile_budget <= 0:
        return None

    if explicit_max_zoom is not None:
        eligible = [zoom for zoom in zoom_levels if zoom <= explicit_max_zoom]
        return max(eligible) if eligible else None

    cumulative_tiles = 0
    selected_zoom: int | None = None
    for zoom in zoom_levels:
        tile_x_min, tile_x_max, tile_y_min, tile_y_max = _mercator_tile_range_for_bounds(entry.meta.bounds, zoom)
        tile_count = ((tile_x_max - tile_x_min) + 1) * ((tile_y_max - tile_y_min) + 1)
        if cumulative_tiles + tile_count > tile_budget:
            break
        cumulative_tiles += tile_count
        selected_zoom = zoom
    return selected_zoom


def _maximum_pyramid_zoom(
    settings: Settings,
    entry: CatalogEntry,
    *,
    explicit_max_zoom: int | None = None,
) -> int:
    native_resolution_m = entry.meta.native_resolution_m
    bounds = entry.meta.bounds
    if explicit_max_zoom is not None:
        return max(settings.browse_tile_max_zoom, min(max(explicit_max_zoom, 0), _MAX_PYRAMID_ZOOM))
    if native_resolution_m is None or native_resolution_m <= 0 or bounds is None:
        return max(settings.browse_tile_max_zoom, 12)

    center_lat = max(min((bounds.south + bounds.north) / 2.0, 85.05112878), -85.05112878)
    meters_per_pixel_at_zoom_zero = 156543.03392804097 * math.cos(math.radians(center_lat))
    if meters_per_pixel_at_zoom_zero <= 0:
        return max(settings.browse_tile_max_zoom, 12)

    native_zoom = math.ceil(math.log2(meters_per_pixel_at_zoom_zero / native_resolution_m))
    target_zoom = max(settings.browse_tile_max_zoom, min(max(native_zoom, 0), _MAX_PYRAMID_ZOOM))
    while target_zoom > settings.browse_tile_max_zoom:
        tile_x_min, tile_x_max, tile_y_min, tile_y_max = _mercator_tile_range_for_bounds(bounds, target_zoom)
        tile_count = ((tile_x_max - tile_x_min) + 1) * ((tile_y_max - tile_y_min) + 1)
        if tile_count <= _MAX_PYRAMID_TILES_PER_LEVEL:
            break
        target_zoom -= 1
    return target_zoom


def _mercator_tile_range_for_bounds(
    bounds,
    zoom: int,
) -> tuple[int, int, int, int]:
    if bounds is None:
        max_index = (2**zoom) - 1
        return 0, max_index, 0, max_index

    epsilon = 1e-9
    tile_x_min = _lon_to_tile_x(bounds.west, zoom)
    tile_x_max = _lon_to_tile_x(min(bounds.east - epsilon, 180.0 - epsilon), zoom)
    tile_y_min = _lat_to_tile_y(max(bounds.north - epsilon, -85.05112878), zoom)
    tile_y_max = _lat_to_tile_y(min(bounds.south + epsilon, 85.05112878), zoom)
    max_index = (2**zoom) - 1
    return (
        max(0, min(tile_x_min, max_index)),
        max(0, min(tile_x_max, max_index)),
        max(0, min(tile_y_min, max_index)),
        max(0, min(tile_y_max, max_index)),
    )


def _lon_to_tile_x(lon: float, zoom: int) -> int:
    scale = 2**zoom
    return int(math.floor(((lon + 180.0) / 360.0) * scale))


def _lat_to_tile_y(lat: float, zoom: int) -> int:
    clamped_lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(clamped_lat)
    scale = 2**zoom
    mercator = math.asinh(math.tan(lat_rad))
    return int(math.floor((1.0 - (mercator / math.pi)) * scale / 2.0))


def _level_bbox_from_tile_range(
    *,
    zoom: int,
    tile_x_min: int,
    tile_x_max: int,
    tile_y_min: int,
    tile_y_max: int,
) -> tuple[float, float, float, float]:
    west, _south_ignored, _east_ignored, north = xyz_to_web_mercator_bbox(zoom, tile_x_min, tile_y_min)
    _west_ignored, south, east, _north_ignored = xyz_to_web_mercator_bbox(zoom, tile_x_max, tile_y_max)
    return west, south, east, north


def _prepopulate_pyramid_levels(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    level_arrays: dict[int, Any],
    tile_ranges: dict[str, dict[str, int]],
    prepopulated_zoom_max: int,
    target_dtype: np.dtype,
) -> None:
    variables = _band_ids_from_entry(entry)
    time_count = int(entry.data_array_meta.shape[0]) if entry.data_array_meta is not None else 1
    overview_cache: dict[tuple[str, int, int], tuple[np.ndarray, tuple[float, float, float, float]]] = {}
    overview_zoom_ceiling = max(int(settings.browse_tile_max_zoom), int(prepopulated_zoom_max))

    if overview_zoom_ceiling > settings.browse_tile_max_zoom:
        build_and_store_browse_overviews(
            settings=settings,
            connector=connector,
            entry=entry,
            variables=variables,
            time_indices=list(range(time_count)),
            zoom_levels=list(range(int(settings.browse_tile_max_zoom) + 1, overview_zoom_ceiling + 1)),
            overwrite=False,
            max_zoom_override=overview_zoom_ceiling,
        )

    for zoom in sorted(level for level in level_arrays if level <= prepopulated_zoom_max):
        tile_range = tile_ranges[str(zoom)]
        level_array = level_arrays[zoom]
        logger.info("Prepopulating pyramid level z=%d for %s", zoom, entry.id)
        for time_index in range(time_count):
            for band_index, variable in enumerate(variables):
                for tile_y in range(tile_range["tile_y_min"], tile_range["tile_y_max"] + 1):
                    row_offset = (tile_y - tile_range["tile_y_min"]) * TILE_SIZE
                    for tile_x in range(tile_range["tile_x_min"], tile_range["tile_x_max"] + 1):
                        col_offset = (tile_x - tile_range["tile_x_min"]) * TILE_SIZE
                        tile = _render_prepopulated_tile(
                            settings=settings,
                            connector=connector,
                            entry=entry,
                            variable=variable,
                            time_index=time_index,
                            zoom=zoom,
                            x=tile_x,
                            y=tile_y,
                            overview_cache=overview_cache,
                            browse_overview_max_zoom=overview_zoom_ceiling,
                        ).astype(target_dtype, copy=False)
                        level_array[
                            time_index,
                            band_index,
                            row_offset : row_offset + TILE_SIZE,
                            col_offset : col_offset + TILE_SIZE,
                        ] = tile


def _render_prepopulated_tile(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
    x: int,
    y: int,
    overview_cache: dict[tuple[str, int, int], tuple[np.ndarray, tuple[float, float, float, float]]],
    browse_overview_max_zoom: int | None = None,
) -> np.ndarray:
    tile_bbox = xyz_to_web_mercator_bbox(zoom, x, y)
    overview_zoom_ceiling = (
        int(settings.browse_tile_max_zoom)
        if browse_overview_max_zoom is None
        else max(int(settings.browse_tile_max_zoom), int(browse_overview_max_zoom))
    )
    if zoom <= overview_zoom_ceiling:
        cache_key = (variable, time_index, zoom)
        overview = overview_cache.get(cache_key)
        if overview is None:
            overview_data, overview_bbox, _source = get_or_create_browse_overview(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable,
                time_index=time_index,
                zoom=zoom,
                allow_build=True,
                max_zoom_override=overview_zoom_ceiling,
            )
            overview = (overview_data, overview_bbox)
            overview_cache[cache_key] = overview
        return sample_web_mercator_array(
            overview[0],
            overview[1],
            tile_bbox,
            width=TILE_SIZE,
            height=TILE_SIZE,
        )

    return render_projected_band_array(
        connector=connector,
        entry=entry,
        variable=variable,
        bbox=tile_bbox,
        width=TILE_SIZE,
        height=TILE_SIZE,
        time_index=time_index,
        max_source_oversample=1.0,
        max_parallel_chunk_reads=1,
    )


def _build_spatial_levels(x_values: np.ndarray, y_values: np.ndarray, *, min_size: int) -> list[tuple[np.ndarray, np.ndarray]]:
    levels = [(np.asarray(x_values), np.asarray(y_values))]
    threshold = max(min_size, 1)
    while max(levels[-1][0].size, levels[-1][1].size) > threshold:
        next_x = _downsample_1d_mean(levels[-1][0])
        next_y = _downsample_1d_mean(levels[-1][1])
        if next_x.size >= levels[-1][0].size and next_y.size >= levels[-1][1].size:
            break
        levels.append((next_x, next_y))
    return levels


def _resolve_level_zero_sampling(
    *,
    height: int,
    width: int,
    max_browser_dimension: int,
    full_resolution: bool,
) -> tuple[np.ndarray, np.ndarray, int]:
    if full_resolution:
        step = 1
    else:
        threshold = max(max_browser_dimension, 1)
        step = 1
        while max(math.ceil(height / step), math.ceil(width / step)) > threshold:
            step *= 2
    return _decimated_indices(height, step), _decimated_indices(width, step), step


def _decimated_indices(length: int, step: int) -> np.ndarray:
    if length <= 0:
        return np.asarray([], dtype=np.int64)
    indices = np.arange(0, length, max(step, 1), dtype=np.int64)
    if indices.size == 0 or indices[-1] != length - 1:
        indices = np.append(indices, length - 1)
    return indices


def _build_root_attributes(
    *,
    data_array_name: str,
    dataset_paths: list[str],
    zoom_levels: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "multiscales": [
            {
                "name": data_array_name,
                "version": "0.4",
                "axes": [
                    {"name": "time", "type": "time"},
                    {"name": "band", "type": "channel"},
                    {"name": "y", "type": "space"},
                    {"name": "x", "type": "space"},
                ],
                "datasets": [{"path": path} for path in dataset_paths],
            }
        ],
        "zoom_levels": zoom_levels or [],
    }


def _build_multiscale_from_browse_overviews(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    mapper,
    zarr_format: int,
) -> dict[str, Any] | None:
    manifest = read_browse_manifest(connector, settings, entry, use_cache=False)
    if not isinstance(manifest, dict):
        return None

    variables_node = manifest.get("variables")
    if not isinstance(variables_node, dict) or not variables_node:
        return None

    band_ids = [item.id for item in entry.meta.variables if item.id in variables_node] or sorted(variables_node)
    if not band_ids:
        return None

    common_time_indices = _common_browse_time_indices(variables_node, band_ids)
    if not common_time_indices:
        return None

    common_zoom_levels = _common_browse_zoom_levels(variables_node, band_ids, common_time_indices)
    if not common_zoom_levels:
        return None

    logger.info(
        "Building multiscale store for %s from browse overviews: bands=%s times=%s zooms=%s",
        entry.id,
        band_ids,
        common_time_indices,
        common_zoom_levels,
    )

    root = zarr.open_group(store=mapper, mode="w", zarr_format=zarr_format)
    root.attrs.update(
        _build_root_attributes(
            data_array_name=entry.data_array_name,
            dataset_paths=[str(level_index) for level_index in range(len(common_zoom_levels))],
            zoom_levels=common_zoom_levels,
        )
    )
    root.attrs["source_representation"] = "browse_overviews"
    root.attrs["browse_zoom_levels"] = common_zoom_levels

    band_values = np.arange(len(band_ids), dtype=np.int32)
    band_attrs = {"band_labels": band_ids}
    time_values = np.asarray(common_time_indices, dtype=np.int32)
    time_attrs = {}
    if entry.meta.time_values:
        labels = [entry.meta.time_values[index] for index in common_time_indices if index < len(entry.meta.time_values)]
        if len(labels) == len(common_time_indices):
            time_attrs["time_labels"] = labels

    level_shapes: list[list[int]] = []
    for level_index, zoom in enumerate(common_zoom_levels):
        sample_data, sample_bbox = _read_browse_overview(
            connector=connector,
            settings=settings,
            entry=entry,
            variable=band_ids[0],
            time_index=common_time_indices[0],
            zoom=zoom,
        )
        height, width = sample_data.shape
        level_shapes.append([height, width])
        level_x, level_y = _coords_from_bbox(sample_bbox, height=height, width=width)

        group = root.create_group(str(level_index), overwrite=True)
        group.attrs.update(
            {
                "source_store_path": entry.path,
                "source_representation": "browse_overviews",
                "browse_zoom_level": zoom,
                "bbox_epsg3857": list(sample_bbox),
                "level": level_index,
            }
        )
        data_array = _create_data_array(
            group=group,
            name=entry.data_array_name,
            shape=(len(time_values), len(band_values), height, width),
            chunk_size=max(height, width),
            dtype=np.dtype(np.float32),
            zarr_format=zarr_format,
            attributes={"band_labels": band_ids},
        )
        _create_coord_array(
            group=group,
            name="x",
            values=level_x,
            dimensions=("x",),
            zarr_format=zarr_format,
            attributes={},
        )
        _create_coord_array(
            group=group,
            name="y",
            values=level_y,
            dimensions=("y",),
            zarr_format=zarr_format,
            attributes={},
        )
        _create_coord_array(
            group=group,
            name="time",
            values=time_values,
            dimensions=("time",),
            zarr_format=zarr_format,
            attributes=time_attrs,
        )
        _create_coord_array(
            group=group,
            name="band",
            values=band_values,
            dimensions=("band",),
            zarr_format=zarr_format,
            attributes=band_attrs,
        )

        level_payload = np.full((len(time_values), len(band_values), height, width), np.nan, dtype=np.float32)
        for time_offset, time_index in enumerate(common_time_indices):
            for band_offset, variable in enumerate(band_ids):
                overview_data, overview_bbox = _read_browse_overview(
                    connector=connector,
                    settings=settings,
                    entry=entry,
                    variable=variable,
                    time_index=time_index,
                    zoom=zoom,
                )
                if overview_data.shape != (height, width):
                    raise ValueError(
                        f"Browse overview shape mismatch for dataset={entry.id} variable={variable} time={time_index} zoom={zoom}"
                    )
                if tuple(round(value, 9) for value in overview_bbox) != tuple(round(value, 9) for value in sample_bbox):
                    raise ValueError(
                        f"Browse overview bbox mismatch for dataset={entry.id} variable={variable} time={time_index} zoom={zoom}"
                    )
                level_payload[time_offset, band_offset, :, :] = overview_data.astype(np.float32, copy=False)
        data_array[:, :, :, :] = level_payload

    zarr.consolidate_metadata(mapper, zarr_format=zarr_format)
    return {
        "levels": list(range(len(common_zoom_levels))),
        "level_shapes": level_shapes,
        "level_zero_source_decimation_factor": None,
        "source_representation": "browse_overviews",
        "browse_zoom_levels": common_zoom_levels,
    }


def _common_browse_time_indices(variables_node: dict[str, Any], band_ids: list[str]) -> list[int]:
    time_sets: list[set[int]] = []
    for band_id in band_ids:
        overviews = variables_node.get(band_id, {}).get("overviews", {})
        if not isinstance(overviews, dict):
            return []
        time_sets.append({int(key) for key, value in overviews.items() if isinstance(value, dict) and str(key).isdigit()})
    if not time_sets:
        return []
    return sorted(set.intersection(*time_sets))


def _common_browse_zoom_levels(
    variables_node: dict[str, Any],
    band_ids: list[str],
    time_indices: list[int],
) -> list[int]:
    zoom_sets: list[set[int]] = []
    for band_id in band_ids:
        overviews = variables_node.get(band_id, {}).get("overviews", {})
        for time_index in time_indices:
            entry = overviews.get(str(time_index), {})
            levels = entry.get("levels", {}) if isinstance(entry, dict) else {}
            if not isinstance(levels, dict):
                return []
            zoom_sets.append({int(key) for key in levels if str(key).isdigit()})
    if not zoom_sets:
        return []
    return sorted(set.intersection(*zoom_sets), reverse=True)


def _read_browse_overview(
    *,
    connector: OCIObjectStorageConnector,
    settings: Settings,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    object_path = browse_manifest_overview_path(
        read_browse_manifest(connector, settings, entry, use_cache=False),
        variable=variable,
        time_index=time_index,
        zoom=zoom,
    )
    if object_path is None:
        raise FileNotFoundError(f"Missing browse overview for {entry.id} {variable} t={time_index} z={zoom}")
    payload = connector.read_bytes(object_path, use_cache=True)
    with np.load(BytesIO(payload), allow_pickle=False) as data:
        array = data["data"].astype(np.float32, copy=False)
        bbox = tuple(float(value) for value in data["bbox"].tolist())
    return array, bbox


def _coords_from_bbox(
    bbox: tuple[float, float, float, float],
    *,
    height: int,
    width: int,
) -> tuple[np.ndarray, np.ndarray]:
    west, south, east, north = bbox
    x_step = (east - west) / max(width, 1)
    y_step = (north - south) / max(height, 1)
    x = west + (np.arange(width, dtype=np.float64) + 0.5) * x_step
    y = north - (np.arange(height, dtype=np.float64) + 0.5) * y_step
    return x, y


def _bbox_from_level_coords(
    x_values: np.ndarray,
    y_values: np.ndarray,
) -> tuple[float, float, float, float]:
    x_step = float(x_values[1] - x_values[0]) if len(x_values) > 1 else 0.0
    y_step = float(y_values[0] - y_values[1]) if len(y_values) > 1 else 0.0
    west = float(x_values[0] - (x_step / 2.0))
    east = float(x_values[-1] + (x_step / 2.0))
    north = float(y_values[0] + (y_step / 2.0))
    south = float(y_values[-1] - (y_step / 2.0))
    return west, south, east, north


def _create_data_array(
    *,
    group,
    name: str,
    shape: tuple[int, int, int, int],
    chunk_size: int,
    dtype: np.dtype,
    zarr_format: int,
    attributes: dict[str, Any],
):
    array_attrs = dict(attributes)
    kwargs: dict[str, Any] = {
        "name": name,
        "shape": shape,
        "chunks": (
            1,
            1,
            min(chunk_size, max(shape[2], 1)),
            min(chunk_size, max(shape[3], 1)),
        ),
        "dtype": dtype,
        "fill_value": np.nan,
        "overwrite": True,
        "attributes": _array_attributes(("time", "band", "y", "x"), array_attrs, zarr_format=zarr_format),
    }
    if zarr_format == 3:
        kwargs["dimension_names"] = ("time", "band", "y", "x")
    elif zarr_format == 2:
        kwargs["compressor"] = None
        kwargs["filters"] = None
    return group.create_array(**kwargs)


def _create_coord_array(
    *,
    group,
    name: str,
    values: np.ndarray,
    dimensions: tuple[str, ...],
    zarr_format: int,
    attributes: dict[str, Any],
):
    chunks = tuple(min(max(values.shape[index], 1), 1024) for index in range(len(values.shape))) or (1,)
    kwargs: dict[str, Any] = {
        "name": name,
        "data": values,
        "chunks": chunks,
        "overwrite": True,
        "attributes": _array_attributes(dimensions, attributes, zarr_format=zarr_format),
    }
    if zarr_format == 3:
        kwargs["dimension_names"] = dimensions
    elif zarr_format == 2:
        kwargs["compressor"] = None
        kwargs["filters"] = None
    return group.create_array(**kwargs)


def _create_spatial_ref_array(*, group, zarr_format: int, attributes: dict[str, Any]) -> None:
    if not attributes:
        return
    kwargs: dict[str, Any] = {
        "name": "spatial_ref",
        "data": np.asarray(0, dtype=np.int32),
        "overwrite": True,
        "attributes": dict(attributes),
    }
    if zarr_format == 3:
        kwargs["dimension_names"] = ()
    group.create_array(**kwargs)


def _array_attributes(dimensions: tuple[str, ...], attributes: dict[str, Any], *, zarr_format: int) -> dict[str, Any]:
    result = dict(attributes)
    if zarr_format == 2:
        result["_ARRAY_DIMENSIONS"] = list(dimensions)
    return result


def _copy_level_zero_from_source(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    target_array,
    target_dtype: np.dtype,
    chunk_size: int,
    source_y_indices: np.ndarray,
    source_x_indices: np.ndarray,
    source_decimation_factor: int,
) -> None:
    if entry.data_array_meta is None:
        raise ValueError(f"Dataset {entry.id} is missing source array metadata")
    time_count, band_count, height, width = entry.data_array_meta.shape
    for time_index in range(time_count):
        for band_index in range(band_count):
            if source_decimation_factor > 1:
                window, _sampled_y, _sampled_x = load_4d_window_decimated(
                    connector=connector,
                    store_path=entry.path,
                    array_name=entry.data_array_name,
                    metadata=entry.data_array_meta,
                    time_index=time_index,
                    band_index=band_index,
                    y_start=0,
                    y_stop=height,
                    x_start=0,
                    x_stop=width,
                    y_step=source_decimation_factor,
                    x_step=source_decimation_factor,
                )
                target_array[time_index, band_index, :, :] = _prepare_source_window(
                    window,
                    fill_value=entry.data_array_meta.fill_value,
                    target_dtype=target_dtype,
                )
                continue
            for target_y_start in range(0, len(source_y_indices), chunk_size):
                target_y_stop = min(target_y_start + chunk_size, len(source_y_indices))
                for target_x_start in range(0, len(source_x_indices), chunk_size):
                    target_x_stop = min(target_x_start + chunk_size, len(source_x_indices))
                    window = _load_level_zero_window(
                        connector=connector,
                        entry=entry,
                        time_index=time_index,
                        band_index=band_index,
                        target_y_indices=source_y_indices[target_y_start:target_y_stop],
                        target_x_indices=source_x_indices[target_x_start:target_x_stop],
                        source_decimation_factor=source_decimation_factor,
                    )
                    target_array[
                        time_index,
                        band_index,
                        target_y_start:target_y_stop,
                        target_x_start:target_x_stop,
                    ] = _prepare_source_window(
                        window,
                        fill_value=entry.data_array_meta.fill_value,
                        target_dtype=target_dtype,
                    )


def _load_level_zero_window(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    time_index: int,
    band_index: int,
    target_y_indices: np.ndarray,
    target_x_indices: np.ndarray,
    source_decimation_factor: int,
) -> np.ndarray:
    if entry.data_array_meta is None:
        raise ValueError(f"Dataset {entry.id} is missing source array metadata")
    return load_4d_window(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=entry.data_array_meta,
        time_index=time_index,
        band_index=band_index,
        y_start=int(target_y_indices[0]),
        y_stop=int(target_y_indices[-1]) + 1,
        x_start=int(target_x_indices[0]),
        x_stop=int(target_x_indices[-1]) + 1,
    )


def _copy_downsampled_level(
    *,
    source_array,
    target_array,
    target_dtype: np.dtype,
    chunk_size: int,
) -> None:
    time_count, band_count, height, width = source_array.shape
    step = chunk_size * 2
    for time_index in range(time_count):
        for band_index in range(band_count):
            for y_start in range(0, height, step):
                y_stop = min(y_start + step, height)
                for x_start in range(0, width, step):
                    x_stop = min(x_start + step, width)
                    block = np.asarray(
                        source_array[time_index, band_index, y_start:y_stop, x_start:x_stop],
                        dtype=np.float32,
                    )
                    reduced = _downsample_2d_mean(block).astype(target_dtype, copy=False)
                    target_y_start = y_start // 2
                    target_x_start = x_start // 2
                    target_y_stop = target_y_start + reduced.shape[0]
                    target_x_stop = target_x_start + reduced.shape[1]
                    target_array[time_index, band_index, target_y_start:target_y_stop, target_x_start:target_x_stop] = reduced


def _prepare_source_window(
    window: np.ndarray,
    *,
    fill_value: Any,
    target_dtype: np.dtype,
) -> np.ndarray:
    result = np.asarray(window, dtype=np.float32)
    if fill_value is not None and not (isinstance(fill_value, float) and math.isnan(fill_value)):
        result[result == fill_value] = np.nan
    return result.astype(target_dtype, copy=False)


def _downsample_1d_mean(values: np.ndarray) -> np.ndarray:
    if values.size <= 1:
        return np.asarray(values)
    work = np.asarray(values, dtype=np.float64)
    if work.size % 2 == 1:
        work = np.pad(work, (0, 1), constant_values=np.nan)
    return np.nanmean(work.reshape(-1, 2), axis=1)


def _downsample_2d_mean(values: np.ndarray) -> np.ndarray:
    work = np.asarray(values, dtype=np.float32)
    if work.shape[0] % 2 == 1:
        work = np.pad(work, ((0, 1), (0, 0)), constant_values=np.nan)
    if work.shape[1] % 2 == 1:
        work = np.pad(work, ((0, 0), (0, 1)), constant_values=np.nan)
    return np.nanmean(work.reshape(work.shape[0] // 2, 2, work.shape[1] // 2, 2), axis=(1, 3))
