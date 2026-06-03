from math import ceil, cos, log2, pi

import numpy as np
from pyproj import CRS, Transformer

from app.config import Settings
from app.core.browse_tiles import get_or_create_browse_overview
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.serving_profile import build_dataset_serving_profile
from app.core.zarr_v3 import estimate_4d_nonempty_pixel_bounds
from app.core.zarr_v3 import estimate_4d_present_shard_pixel_bounds
from app.core.zarr_v3 import build_chunk_object_path
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import _chunks_per_shard
from app.core.zarr_v3 import _list_present_shard_positions
from app.core.zarr_v3 import _read_shard_index
from app.core.zarr_v3 import _UINT64_MAX
from app.models.dataset import DatasetBounds
from app.models.dataset import TileJSON


_BROWSE_FALLBACK_MIN_PIXELS = 64
_BROWSE_FALLBACK_MIN_COVERAGE_RATIO = 0.005
_DEFAULT_TILEJSON_BOUNDS = [-180.0, -85.0511, 180.0, 85.0511]
_FOCUS_CENTER_MAX_CHUNKS = 512


def build_dataset_tilejson(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    *,
    variable: str,
    time_index: int,
    tile_template: str,
) -> TileJSON:
    ensure_catalog_entry_ready(entry, connector)
    profile = build_dataset_serving_profile(settings, connector, entry)
    bounds = _time_specific_bounds(
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
    ) or entry.meta.bounds
    detail_minzoom = _detail_minzoom(settings, profile.browse_overview_max_zoom)
    center = _time_specific_center(
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
        zoom=detail_minzoom,
    )
    has_coarse_fallback = _has_useful_browse_fallback(
        settings=settings,
        connector=connector,
        entry=entry,
        variable=variable,
        time_index=time_index,
        browse_overview_max_zoom=profile.browse_overview_max_zoom,
    )

    return TileJSON(
        name=f"{entry.meta.name}:{variable}",
        tiles=[tile_template],
        bounds=_tilejson_bounds(bounds),
        center=center,
        minzoom=0 if has_coarse_fallback else detail_minzoom,
        maxzoom=_maxzoom_for_detail(
            bounds=bounds,
            native_resolution_m=entry.meta.native_resolution_m,
            detail_minzoom=detail_minzoom,
            multiscale_max_zoom=profile.multiscale_max_zoom,
        ),
        detail_minzoom=detail_minzoom,
        has_coarse_fallback=has_coarse_fallback,
        coarse_representation="browse" if has_coarse_fallback else None,
    )


def build_registry_tilejson(
    *,
    name: str,
    bounds: DatasetBounds | None,
    native_resolution_m: float | None,
    tile_template: str,
) -> TileJSON:
    return TileJSON(
        name=name,
        tiles=[tile_template],
        bounds=_tilejson_bounds(bounds),
        minzoom=0,
        maxzoom=_maxzoom_for_detail(
            bounds=bounds,
            native_resolution_m=native_resolution_m,
            detail_minzoom=0,
            multiscale_max_zoom=None,
        ),
        detail_minzoom=0,
        has_coarse_fallback=False,
        coarse_representation=None,
    )


def _tilejson_bounds(bounds: DatasetBounds | None) -> list[float]:
    if bounds is None:
        return list(_DEFAULT_TILEJSON_BOUNDS)
    return [bounds.west, bounds.south, bounds.east, bounds.north]


def _time_specific_bounds(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> DatasetBounds | None:
    if entry.data_array_meta is None or entry.geo_transform is None:
        return None

    band_index = entry.band_indices.get(variable)
    if band_index is None:
        return None

    pixel_window = estimate_4d_present_shard_pixel_bounds(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=entry.data_array_meta,
        time_indices=[time_index],
        band_index=band_index,
    )
    if pixel_window is None:
        pixel_window = estimate_4d_nonempty_pixel_bounds(
            connector=connector,
            store_path=entry.path,
            array_name=entry.data_array_name,
            metadata=entry.data_array_meta,
            time_indices=[time_index],
            band_index=band_index,
        )
    if pixel_window is None:
        return None

    x_start, x_stop, y_start, y_stop = pixel_window
    return _bounds_from_pixel_window(
        x_start=x_start,
        x_stop=x_stop,
        y_start=y_start,
        y_stop=y_stop,
        crs_wkt=entry.crs_wkt,
        geo_transform=entry.geo_transform,
    )


def _time_specific_center(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    zoom: int,
) -> list[float] | None:
    if entry.data_array_meta is None or entry.geo_transform is None:
        return None
    if len(entry.data_array_meta.shape) != 4 or entry.data_array_meta.sharding is None:
        return None

    band_index = entry.band_indices.get(variable)
    if band_index is None:
        return None

    metadata = entry.data_array_meta
    sharding = metadata.sharding
    chunks_per_shard = _chunks_per_shard(
        shard_shape=metadata.chunk_shape,
        inner_chunk_shape=sharding.chunk_shape,
    )
    chunk_y = int(metadata.effective_chunk_shape[2])
    chunk_x = int(metadata.effective_chunk_shape[3])
    shape_y = int(metadata.shape[2])
    shape_x = int(metadata.shape[3])

    shard_positions = _list_present_shard_positions(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=metadata,
        time_indices=[time_index],
        band_index=band_index,
    )
    if not shard_positions:
        return None

    checked_chunks = 0
    for position in sorted(shard_positions, key=lambda item: (item[2], item[3])):
        _, _, y_shard_index, x_shard_index = position
        object_path = build_chunk_object_path(
            store_path=entry.path,
            array_name=entry.data_array_name,
            separator=metadata.separator,
            chunk_indices=position,
        )
        try:
            shard_index = _read_shard_index(
                connector=connector,
                object_path=object_path,
                shard_shape=metadata.chunk_shape,
                inner_chunk_shape=sharding.chunk_shape,
                index_codecs=sharding.index_codecs,
                index_location=sharding.index_location,
            )
        except FileNotFoundError:
            continue

        for local_y in range(shard_index.shape[2]):
            for local_x in range(shard_index.shape[3]):
                offset = int(shard_index[0, 0, local_y, local_x, 0])
                length = int(shard_index[0, 0, local_y, local_x, 1])
                if offset == _UINT64_MAX and length == _UINT64_MAX:
                    continue
                checked_chunks += 1
                if checked_chunks > _FOCUS_CENTER_MAX_CHUNKS:
                    return None

                global_y_chunk = y_shard_index * chunks_per_shard[2] + local_y
                global_x_chunk = x_shard_index * chunks_per_shard[3] + local_x
                y_start = global_y_chunk * chunk_y
                x_start = global_x_chunk * chunk_x
                y_stop = min(y_start + chunk_y, shape_y)
                x_stop = min(x_start + chunk_x, shape_x)
                if x_start >= x_stop or y_start >= y_stop:
                    continue

                window = load_4d_window(
                    connector=connector,
                    store_path=entry.path,
                    array_name=entry.data_array_name,
                    metadata=metadata,
                    time_index=time_index,
                    band_index=band_index,
                    y_start=y_start,
                    y_stop=y_stop,
                    x_start=x_start,
                    x_stop=x_stop,
                    max_parallel_chunk_reads=1,
                ).astype(np.float32)
                finite = np.isfinite(window)
                if not np.any(finite):
                    continue

                rows, cols = np.where(finite)
                bounds = _bounds_from_pixel_window(
                    x_start=x_start + int(cols.min()),
                    x_stop=x_start + int(cols.max()) + 1,
                    y_start=y_start + int(rows.min()),
                    y_stop=y_start + int(rows.max()) + 1,
                    crs_wkt=entry.crs_wkt,
                    geo_transform=entry.geo_transform,
                )
                if bounds is None:
                    return None
                return [
                    (bounds.west + bounds.east) / 2.0,
                    (bounds.south + bounds.north) / 2.0,
                    float(zoom),
                ]

    return None


def _bounds_from_pixel_window(
    *,
    x_start: int,
    x_stop: int,
    y_start: int,
    y_stop: int,
    crs_wkt: str | None,
    geo_transform: tuple[float, float, float, float, float, float] | None,
) -> DatasetBounds | None:
    if x_start >= x_stop or y_start >= y_stop or geo_transform is None:
        return None

    source_crs = CRS.from_wkt(crs_wkt) if crs_wkt else CRS.from_epsg(4326)
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = geo_transform

    def _point(column: int, row: int) -> tuple[float, float]:
        return (
            origin_x + (column * pixel_width) + (row * rot_x),
            origin_y + (column * rot_y) + (row * pixel_height),
        )

    xs: list[float] = []
    ys: list[float] = []
    for column, row in (
        (x_start, y_start),
        (x_stop, y_start),
        (x_stop, y_stop),
        (x_start, y_stop),
    ):
        x_value, y_value = _point(column, row)
        xs.append(x_value)
        ys.append(y_value)

    lon_values, lat_values = transformer.transform(xs, ys)
    return DatasetBounds(
        west=max(min(lon_values), -180.0),
        south=max(min(lat_values), -85.0511),
        east=min(max(lon_values), 180.0),
        north=min(max(lat_values), 85.0511),
    )


def _detail_minzoom(settings: Settings, browse_overview_max_zoom: int | None) -> int:
    if browse_overview_max_zoom is not None:
        return max(browse_overview_max_zoom + 1, 0)
    return max(settings.browse_tile_max_zoom + 1, 0)


def _has_useful_browse_fallback(
    *,
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
    browse_overview_max_zoom: int | None,
) -> bool:
    if browse_overview_max_zoom is None:
        return False
    try:
        overview, _overview_bbox, _source = get_or_create_browse_overview(
            settings=settings,
            connector=connector,
            entry=entry,
            variable=variable,
            time_index=time_index,
            zoom=browse_overview_max_zoom,
            allow_build=False,
        )
    except Exception:
        return False

    finite = np.isfinite(overview)
    finite_pixels = int(finite.sum())
    if finite_pixels <= 0:
        return False
    minimum_pixels = max(
        _BROWSE_FALLBACK_MIN_PIXELS,
        ceil(overview.size * _BROWSE_FALLBACK_MIN_COVERAGE_RATIO),
    )
    return finite_pixels >= minimum_pixels


def _maxzoom_for_detail(
    *,
    bounds: DatasetBounds | None,
    native_resolution_m: float | None,
    detail_minzoom: int,
    multiscale_max_zoom: int | None,
) -> int:
    native_maxzoom = _native_resolution_maxzoom(bounds=bounds, native_resolution_m=native_resolution_m)
    return max(detail_minzoom, native_maxzoom, multiscale_max_zoom or 0)


def _native_resolution_maxzoom(
    *,
    bounds: DatasetBounds | None,
    native_resolution_m: float | None,
) -> int:
    if native_resolution_m is None or native_resolution_m <= 0:
        return 18
    center_lat = 0.0 if bounds is None else (bounds.south + bounds.north) / 2.0
    meters_per_pixel_at_zoom_zero = 156543.03392804097 * cos(center_lat * pi / 180.0)
    if meters_per_pixel_at_zoom_zero <= 0:
        return 18
    return min(max(0, ceil(log2(meters_per_pixel_at_zoom_zero / native_resolution_m))) + 1, 22)
