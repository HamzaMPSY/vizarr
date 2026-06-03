import io
import math
from functools import lru_cache

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

from app.core.colormap import encode_tile
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.tile_observability import observe_tile_time
from app.core.variable_display import resolve_display_range
from app.core.zarr_v3 import load_2d_window
from app.core.zarr_v3 import load_2d_window_decimated
from app.core.zarr_v3 import load_3d_window
from app.core.zarr_v3 import load_3d_window_decimated
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import load_4d_window_decimated


WEB_MERCATOR_HALF_WORLD = 20037508.342789244
WEB_MERCATOR_RADIUS = WEB_MERCATOR_HALF_WORLD / math.pi
TILE_SIZE = 256


def xyz_to_web_mercator_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    west = x / n * (2 * WEB_MERCATOR_HALF_WORLD) - WEB_MERCATOR_HALF_WORLD
    east = (x + 1) / n * (2 * WEB_MERCATOR_HALF_WORLD) - WEB_MERCATOR_HALF_WORLD
    north = WEB_MERCATOR_HALF_WORLD - y / n * (2 * WEB_MERCATOR_HALF_WORLD)
    south = WEB_MERCATOR_HALF_WORLD - (y + 1) / n * (2 * WEB_MERCATOR_HALF_WORLD)
    return west, south, east, north


def _pixel_centers(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    west, south, east, north = bbox
    pixel_width = (east - west) / width
    pixel_height = (north - south) / height

    xs = west + (np.arange(width, dtype=np.float64) + 0.5) * pixel_width
    ys = north - (np.arange(height, dtype=np.float64) + 0.5) * pixel_height
    return np.meshgrid(xs, ys)


def _pixel_center_axes(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    west, south, east, north = bbox
    pixel_width = (east - west) / width
    pixel_height = (north - south) / height

    xs = west + (np.arange(width, dtype=np.float64) + 0.5) * pixel_width
    ys = north - (np.arange(height, dtype=np.float64) + 0.5) * pixel_height
    return xs, ys


def _web_mercator_x_to_lon(xs: np.ndarray) -> np.ndarray:
    return np.degrees(xs / WEB_MERCATOR_RADIUS)


def _web_mercator_y_to_lat(ys: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan(np.sinh(ys / WEB_MERCATOR_RADIUS)))


def _fractional_indices_from_geotransform(
    geo_transform: tuple[float, float, float, float, float, float],
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = geo_transform
    linear = np.array(
        [
            [pixel_width, rot_x],
            [rot_y, pixel_height],
        ],
        dtype=np.float64,
    )
    determinant = float(np.linalg.det(linear))
    if math.isclose(determinant, 0.0):
        return None

    inverse = np.linalg.inv(linear)
    offsets = np.stack([xs - origin_x, ys - origin_y], axis=0).reshape(2, -1)
    pixel_coordinates = inverse @ offsets
    cols = pixel_coordinates[0].reshape(xs.shape) - 0.5
    rows = pixel_coordinates[1].reshape(xs.shape) - 0.5
    return cols, rows


def _fractional_indices_from_north_up_geotransform_axes(
    geo_transform: tuple[float, float, float, float, float, float],
    lon_values: np.ndarray,
    lat_values: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = geo_transform
    if not math.isclose(rot_x, 0.0) or not math.isclose(rot_y, 0.0):
        return None
    if math.isclose(pixel_width, 0.0) or math.isclose(pixel_height, 0.0):
        return None

    x_idx = ((lon_values - origin_x) / pixel_width) - 0.5
    y_idx = ((lat_values - origin_y) / pixel_height) - 0.5
    return x_idx, y_idx


def _coordinate_to_fractional_index(values: np.ndarray, coordinates: np.ndarray) -> np.ndarray:
    positions = np.arange(len(values), dtype=np.float64)
    if values[0] <= values[-1]:
        return np.interp(coordinates, values, positions, left=np.nan, right=np.nan)

    reversed_positions = np.interp(
        coordinates,
        values[::-1],
        positions,
        left=np.nan,
        right=np.nan,
    )
    return (len(values) - 1) - reversed_positions


def _bilinear_sample(data: np.ndarray, y_idx: np.ndarray, x_idx: np.ndarray) -> np.ndarray:
    height, width = data.shape
    result = np.full(y_idx.shape, np.nan, dtype=np.float32)

    valid = (
        np.isfinite(x_idx)
        & np.isfinite(y_idx)
        & (x_idx >= 0)
        & (y_idx >= 0)
        & (x_idx <= width - 1)
        & (y_idx <= height - 1)
    )
    if not np.any(valid):
        return result

    x = x_idx[valid]
    y = y_idx[valid]

    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)

    wx = x - x0
    wy = y - y0

    top_left = data[y0, x0]
    top_right = data[y0, x1]
    bottom_left = data[y1, x0]
    bottom_right = data[y1, x1]

    weights_top_left = (1.0 - wx) * (1.0 - wy)
    weights_top_right = wx * (1.0 - wy)
    weights_bottom_left = (1.0 - wx) * wy
    weights_bottom_right = wx * wy

    stacked_values = np.stack(
        [top_left, top_right, bottom_left, bottom_right],
        axis=0,
    ).astype(np.float32)
    stacked_weights = np.stack(
        [
            weights_top_left,
            weights_top_right,
            weights_bottom_left,
            weights_bottom_right,
        ],
        axis=0,
    ).astype(np.float32)

    finite_neighbors = np.isfinite(stacked_values)
    weighted_sum = np.sum(
        np.where(finite_neighbors, stacked_values * stacked_weights, 0.0),
        axis=0,
    )
    weight_sum = np.sum(
        np.where(finite_neighbors, stacked_weights, 0.0),
        axis=0,
    )
    sampled = np.full(weight_sum.shape, np.nan, dtype=np.float32)
    non_zero = weight_sum > 0
    sampled[non_zero] = (weighted_sum[non_zero] / weight_sum[non_zero]).astype(np.float32)

    result[valid] = sampled
    return result


def _axis_edges(center_indices: np.ndarray) -> np.ndarray | None:
    if center_indices.ndim != 1 or center_indices.size == 0:
        return None
    finite = np.isfinite(center_indices)
    if not np.all(finite):
        return None
    if center_indices.size == 1:
        return np.asarray([center_indices[0] - 0.5, center_indices[0] + 0.5], dtype=np.float64)

    deltas = np.diff(center_indices)
    if not np.all(deltas > 0):
        return None

    edges = np.empty(center_indices.size + 1, dtype=np.float64)
    edges[1:-1] = (center_indices[:-1] + center_indices[1:]) / 2.0
    edges[0] = center_indices[0] - (deltas[0] / 2.0)
    edges[-1] = center_indices[-1] + (deltas[-1] / 2.0)
    return edges


def _aggregate_window_to_target_grid(
    window: np.ndarray,
    *,
    x_start: int,
    y_start: int,
    x_idx_axis: np.ndarray,
    y_idx_axis: np.ndarray,
    width: int,
    height: int,
) -> np.ndarray:
    result = np.full((height, width), np.nan, dtype=np.float32)
    finite = np.isfinite(window)
    if not np.any(finite):
        return result

    x_edges = _axis_edges(x_idx_axis)
    y_edges = _axis_edges(y_idx_axis)
    if x_edges is None or y_edges is None:
        return result

    rows, cols = np.where(finite)
    source_cols = cols + x_start
    source_rows = rows + y_start
    target_cols = np.searchsorted(x_edges, source_cols, side="right") - 1
    target_rows = np.searchsorted(y_edges, source_rows, side="right") - 1
    valid = (target_cols >= 0) & (target_cols < width) & (target_rows >= 0) & (target_rows < height)
    if not np.any(valid):
        return result

    target_cols = target_cols[valid]
    target_rows = target_rows[valid]
    values = window[rows[valid], cols[valid]].astype(np.float64, copy=False)
    sums = np.zeros((height, width), dtype=np.float64)
    counts = np.zeros((height, width), dtype=np.uint32)
    np.add.at(sums, (target_rows, target_cols), values)
    np.add.at(counts, (target_rows, target_cols), 1)
    populated = counts > 0
    result[populated] = (sums[populated] / counts[populated]).astype(np.float32)
    return result


def _source_window_bounds(
    x_values: np.ndarray,
    y_values: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[int, int, int, int] | None:
    x_min_bound = min(float(x_values[0]), float(x_values[-1]))
    x_max_bound = max(float(x_values[0]), float(x_values[-1]))
    y_min_bound = min(float(y_values[0]), float(y_values[-1]))
    y_max_bound = max(float(y_values[0]), float(y_values[-1]))

    finite = (
        np.isfinite(xs)
        & np.isfinite(ys)
        & (xs >= x_min_bound)
        & (xs <= x_max_bound)
        & (ys >= y_min_bound)
        & (ys <= y_max_bound)
    )
    if not np.any(finite):
        return None

    x_min = float(np.nanmin(xs[finite]))
    x_max = float(np.nanmax(xs[finite]))
    y_min = float(np.nanmin(ys[finite]))
    y_max = float(np.nanmax(ys[finite]))

    x_start = max(int(np.searchsorted(x_values, x_min, side="left")) - 1, 0)
    x_stop = min(int(np.searchsorted(x_values, x_max, side="right")) + 1, len(x_values))

    if y_values[0] > y_values[-1]:
        y_reversed = y_values[::-1]
        y_start_rev = max(int(np.searchsorted(y_reversed, y_min, side="left")) - 1, 0)
        y_stop_rev = min(int(np.searchsorted(y_reversed, y_max, side="right")) + 1, len(y_reversed))
        y_start = len(y_values) - y_stop_rev
        y_stop = len(y_values) - y_start_rev
    else:
        y_start = max(int(np.searchsorted(y_values, y_min, side="left")) - 1, 0)
        y_stop = min(int(np.searchsorted(y_values, y_max, side="right")) + 1, len(y_values))

    if x_start >= x_stop or y_start >= y_stop:
        return None
    return x_start, x_stop, y_start, y_stop


def _source_window_bounds_from_indices(
    x_idx: np.ndarray,
    y_idx: np.ndarray,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    finite = (
        np.isfinite(x_idx)
        & np.isfinite(y_idx)
        & (x_idx >= 0)
        & (y_idx >= 0)
        & (x_idx <= width - 1)
        & (y_idx <= height - 1)
    )
    if not np.any(finite):
        return None

    x_min = max(int(np.floor(np.nanmin(x_idx[finite]))) - 1, 0)
    x_max = min(int(np.ceil(np.nanmax(x_idx[finite]))) + 2, width)
    y_min = max(int(np.floor(np.nanmin(y_idx[finite]))) - 1, 0)
    y_max = min(int(np.ceil(np.nanmax(y_idx[finite]))) + 2, height)

    if x_min >= x_max or y_min >= y_max:
        return None
    return x_min, x_max, y_min, y_max


def _source_window_bounds_from_axis_indices(
    x_idx: np.ndarray,
    y_idx: np.ndarray,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    finite_x = np.isfinite(x_idx) & (x_idx >= 0) & (x_idx <= width - 1)
    finite_y = np.isfinite(y_idx) & (y_idx >= 0) & (y_idx <= height - 1)
    if not np.any(finite_x) or not np.any(finite_y):
        return None

    x_min = max(int(np.floor(np.nanmin(x_idx[finite_x]))) - 1, 0)
    x_max = min(int(np.ceil(np.nanmax(x_idx[finite_x]))) + 2, width)
    y_min = max(int(np.floor(np.nanmin(y_idx[finite_y]))) - 1, 0)
    y_max = min(int(np.ceil(np.nanmax(y_idx[finite_y]))) + 2, height)
    if x_min >= x_max or y_min >= y_max:
        return None
    return x_min, x_max, y_min, y_max


@lru_cache(maxsize=16)
def _transformer_from_mercator(target_crs_repr: str) -> Transformer:
    target_crs = CRS.from_user_input(target_crs_repr)
    return Transformer.from_crs("EPSG:3857", target_crs, always_xy=True)


def _is_fast_latlon_entry(entry: CatalogEntry) -> bool:
    if entry.geo_transform is None:
        return False
    origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = entry.geo_transform
    if math.isclose(pixel_width, 0.0) or math.isclose(pixel_height, 0.0):
        return False
    if not math.isclose(rot_x, 0.0) or not math.isclose(rot_y, 0.0):
        return False
    source_crs = CRS.from_wkt(entry.crs_wkt) if entry.crs_wkt else CRS.from_epsg(4326)
    return source_crs == CRS.from_epsg(4326)


def _resolve_display_range(
    data: np.ndarray,
    fallback_vmin: float,
    fallback_vmax: float,
    vmin: float | None,
    vmax: float | None,
) -> tuple[float, float]:
    if vmin is not None and vmax is not None:
        return vmin, vmax

    if vmin is None and vmax is None and not math.isclose(fallback_vmin, fallback_vmax):
        return fallback_vmin, fallback_vmax

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return (
            fallback_vmin if vmin is None else vmin,
            fallback_vmax if vmax is None else vmax,
        )

    computed_vmin = float(np.nanpercentile(finite, 2))
    computed_vmax = float(np.nanpercentile(finite, 98))

    if math.isclose(computed_vmin, computed_vmax):
        computed_vmin = float(np.nanmin(finite))
        computed_vmax = float(np.nanmax(finite))

    if math.isclose(computed_vmin, computed_vmax):
        computed_vmax = computed_vmin + 1.0

    return (
        computed_vmin if vmin is None else vmin,
        computed_vmax if vmax is None else vmax,
    )


def resolve_projected_display_range(
    entry: CatalogEntry,
    variable: str,
    data: np.ndarray,
    vmin: float | None,
    vmax: float | None,
) -> tuple[float, float]:
    selected = next(item for item in entry.meta.variables if item.id == variable)
    return _resolve_display_range(
        data=data,
        fallback_vmin=resolve_display_range(selected, None, None)[0],
        fallback_vmax=resolve_display_range(selected, None, None)[1],
        vmin=vmin,
        vmax=vmax,
    )


def resolve_composite_band_ids(entry: CatalogEntry, composite_id: str) -> list[str] | None:
    for style in entry.meta.composite_styles:
        if style.id == composite_id:
            return style.bands
    return None


def _normalize_composite_channel(
    data: np.ndarray,
    *,
    fallback_vmin: float,
    fallback_vmax: float,
    vmin: float | None,
    vmax: float | None,
) -> np.ndarray:
    actual_vmin, actual_vmax = _resolve_display_range(
        data=data,
        fallback_vmin=fallback_vmin,
        fallback_vmax=fallback_vmax,
        vmin=vmin,
        vmax=vmax,
    )
    if actual_vmax <= actual_vmin:
        actual_vmax = actual_vmin + 1e-6
    normalized = (np.clip(data, actual_vmin, actual_vmax) - actual_vmin) / (actual_vmax - actual_vmin)
    return (np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=0.0) * 255).astype(np.uint8)


def encode_rgb_tile(rgb_data: np.ndarray) -> bytes:
    finite_mask = np.all(np.isfinite(rgb_data), axis=-1)
    rgba = np.zeros((*rgb_data.shape[:2], 4), dtype=np.uint8)
    rgba[..., :3] = np.nan_to_num(rgb_data, nan=0.0, posinf=255.0, neginf=0.0).astype(np.uint8)
    rgba[..., 3] = np.where(finite_mask, 255, 0).astype(np.uint8)
    with observe_tile_time("image_encoding"):
        image = Image.fromarray(rgba, mode="RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=85)
    return buffer.getvalue()


def render_projected_composite_array(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    composite_id: str,
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
    time_index: int,
    vmin: float | None = None,
    vmax: float | None = None,
    max_source_oversample: float | None = None,
    max_parallel_chunk_reads: int | None = None,
) -> np.ndarray:
    entry = ensure_catalog_entry_metadata_ready(entry, connector)
    band_ids = resolve_composite_band_ids(entry, composite_id)
    if band_ids is None:
        raise ValueError(f"Unknown composite style '{composite_id}'")

    channels: list[np.ndarray] = []
    for band_id in band_ids:
        channel = render_projected_band_array(
            connector=connector,
            entry=entry,
            variable=band_id,
            bbox=bbox,
            width=width,
            height=height,
            time_index=time_index,
            max_source_oversample=max_source_oversample,
            max_parallel_chunk_reads=max_parallel_chunk_reads,
        )
        selected = next(item for item in entry.meta.variables if item.id == band_id)
        fallback_vmin, fallback_vmax = resolve_display_range(selected, None, None)
        channels.append(
            _normalize_composite_channel(
                channel,
                fallback_vmin=fallback_vmin,
                fallback_vmax=fallback_vmax,
                vmin=vmin,
                vmax=vmax,
            )
        )

    return np.stack(channels, axis=-1)


def sample_web_mercator_array(
    data: np.ndarray,
    data_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> np.ndarray:
    data_height, data_width = data.shape
    target_xs, target_ys = _pixel_centers(target_bbox, width=width, height=height)
    data_west, data_south, data_east, data_north = data_bbox
    data_width_span = data_east - data_west
    data_height_span = data_north - data_south
    if math.isclose(data_width_span, 0.0) or math.isclose(data_height_span, 0.0):
        return np.full((height, width), np.nan, dtype=np.float32)

    x_idx = ((target_xs - data_west) / data_width_span) * data_width - 0.5
    y_idx = ((data_north - target_ys) / data_height_span) * data_height - 0.5
    return _bilinear_sample(data=data.astype(np.float32, copy=False), y_idx=y_idx, x_idx=x_idx)


def _load_projected_source_window(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    array_name: str,
    data_array_meta,
    array_rank: int,
    band_index: int | None,
    time_index: int,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
    max_parallel_chunk_reads: int | None,
) -> np.ndarray:
    if array_rank == 2:
        return load_2d_window(
            connector=connector,
            store_path=entry.path,
            array_name=array_name,
            metadata=data_array_meta,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=max_parallel_chunk_reads,
        ).astype(np.float32)
    if band_index is None:
        return load_3d_window(
            connector=connector,
            store_path=entry.path,
            array_name=array_name,
            metadata=data_array_meta,
            time_index=time_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=max_parallel_chunk_reads,
        ).astype(np.float32)
    return load_4d_window(
        connector=connector,
        store_path=entry.path,
        array_name=array_name,
        metadata=data_array_meta,
        time_index=time_index,
        band_index=band_index,
        y_start=y_start,
        y_stop=y_stop,
        x_start=x_start,
        x_stop=x_stop,
        max_parallel_chunk_reads=max_parallel_chunk_reads,
    ).astype(np.float32)


def render_projected_band_array(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
    time_index: int,
    max_source_oversample: float | None = None,
    max_parallel_chunk_reads: int | None = None,
) -> np.ndarray:
    entry = ensure_catalog_entry_metadata_ready(entry, connector)
    array_name = entry.variable_array_names.get(variable, entry.data_array_name)
    data_array_meta = entry.data_array_metas.get(array_name) or entry.data_array_meta
    assert data_array_meta is not None

    array_rank = len(data_array_meta.shape)
    band_index = entry.band_indices[variable] if array_rank == 4 else None
    aggregation_x_idx_axis: np.ndarray | None = None
    aggregation_y_idx_axis: np.ndarray | None = None
    if _is_fast_latlon_entry(entry):
        source_width = int(data_array_meta.shape[-1])
        source_height = int(data_array_meta.shape[-2])
        mercator_x_axis, mercator_y_axis = _pixel_center_axes(bbox, width=width, height=height)
        lon_values = _web_mercator_x_to_lon(mercator_x_axis)
        lat_values = _web_mercator_y_to_lat(mercator_y_axis)
        affine_axes = _fractional_indices_from_north_up_geotransform_axes(
            entry.geo_transform,
            lon_values=lon_values,
            lat_values=lat_values,
        )
        assert affine_axes is not None
        x_idx_axis, y_idx_axis = affine_axes
        aggregation_x_idx_axis = x_idx_axis
        aggregation_y_idx_axis = y_idx_axis
        x_idx_full = np.broadcast_to(x_idx_axis[np.newaxis, :], (height, width))
        y_idx_full = np.broadcast_to(y_idx_axis[:, np.newaxis], (height, width))
        window_bounds = _source_window_bounds_from_axis_indices(
            x_idx=x_idx_axis,
            y_idx=y_idx_axis,
            width=source_width,
            height=source_height,
        )
        source_xs = source_ys = None
        x_values = y_values = None
    else:
        entry = ensure_catalog_entry_ready(entry, connector)
        x_values = entry.x_values
        y_values = entry.y_values
        mercator_xs, mercator_ys = _pixel_centers(bbox, width=width, height=height)
        target_crs = CRS.from_wkt(entry.crs_wkt) if entry.crs_wkt else CRS.from_epsg(4326)
        transformer = _transformer_from_mercator(target_crs.to_wkt())
        source_xs, source_ys = transformer.transform(
            mercator_xs,
            mercator_ys,
        )
        affine_indices = (
            _fractional_indices_from_geotransform(entry.geo_transform, source_xs, source_ys)
            if entry.geo_transform is not None
            else None
        )
        if affine_indices is not None:
            x_idx_full, y_idx_full = affine_indices
            source_width = int(data_array_meta.shape[-1])
            source_height = int(data_array_meta.shape[-2])
            window_bounds = _source_window_bounds_from_indices(
                x_idx=x_idx_full,
                y_idx=y_idx_full,
                width=source_width,
                height=source_height,
            )
        else:
            assert x_values is not None
            assert y_values is not None
            x_idx_full = y_idx_full = None
            window_bounds = _source_window_bounds(
                x_values=x_values,
                y_values=y_values,
                xs=source_xs,
                ys=source_ys,
            )

    if window_bounds is None:
        return np.full((height, width), np.nan, dtype=np.float32)

    x_start, x_stop, y_start, y_stop = window_bounds
    use_decimated_window = (
        x_idx_full is not None
        and y_idx_full is not None
        and max_source_oversample is not None
        and max_source_oversample > 0
        and (
            (x_stop - x_start) > (width * max_source_oversample)
            or (y_stop - y_start) > (height * max_source_oversample)
        )
    )
    if use_decimated_window:
        x_step = max(1, math.ceil((x_stop - x_start) / max(width * max_source_oversample, 1.0)))
        y_step = max(1, math.ceil((y_stop - y_start) / max(height * max_source_oversample, 1.0)))
        if array_rank == 2:
            window, sampled_y, sampled_x = load_2d_window_decimated(
                connector=connector,
                store_path=entry.path,
                array_name=array_name,
                metadata=data_array_meta,
                y_start=y_start,
                y_stop=y_stop,
                x_start=x_start,
                x_stop=x_stop,
                y_step=y_step,
                x_step=x_step,
                max_parallel_chunk_reads=max_parallel_chunk_reads,
            )
        elif band_index is None:
            window, sampled_y, sampled_x = load_3d_window_decimated(
                connector=connector,
                store_path=entry.path,
                array_name=array_name,
                metadata=data_array_meta,
                time_index=time_index,
                y_start=y_start,
                y_stop=y_stop,
                x_start=x_start,
                x_stop=x_stop,
                y_step=y_step,
                x_step=x_step,
                max_parallel_chunk_reads=max_parallel_chunk_reads,
            )
        else:
            window, sampled_y, sampled_x = load_4d_window_decimated(
                connector=connector,
                store_path=entry.path,
                array_name=array_name,
                metadata=data_array_meta,
                time_index=time_index,
                band_index=band_index,
                y_start=y_start,
                y_stop=y_stop,
                x_start=x_start,
                x_stop=x_stop,
                y_step=y_step,
                x_step=x_step,
                max_parallel_chunk_reads=max_parallel_chunk_reads,
            )
        window = window.astype(np.float32)
        x_idx = _coordinate_to_fractional_index(sampled_x, x_idx_full)
        y_idx = _coordinate_to_fractional_index(sampled_y, y_idx_full)
    else:
        window = _load_projected_source_window(
            connector=connector,
            entry=entry,
            array_name=array_name,
            data_array_meta=data_array_meta,
            array_rank=array_rank,
            band_index=band_index,
            time_index=time_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=max_parallel_chunk_reads,
        )

    if x_idx_full is not None and y_idx_full is not None and not use_decimated_window:
        x_idx = x_idx_full - x_start
        y_idx = y_idx_full - y_start
    else:
        if not use_decimated_window:
            assert source_xs is not None
            assert source_ys is not None
            assert x_values is not None
            assert y_values is not None
            local_x_values = x_values[x_start:x_stop]
            local_y_values = y_values[y_start:y_stop]
            x_idx = _coordinate_to_fractional_index(local_x_values, source_xs)
            y_idx = _coordinate_to_fractional_index(local_y_values, source_ys)
    sampled = _bilinear_sample(
        data=window,
        y_idx=y_idx,
        x_idx=x_idx,
    )
    if (
        use_decimated_window
        and aggregation_x_idx_axis is not None
        and aggregation_y_idx_axis is not None
        and not np.any(np.isfinite(sampled))
    ):
        full_window = _load_projected_source_window(
            connector=connector,
            entry=entry,
            array_name=array_name,
            data_array_meta=data_array_meta,
            array_rank=array_rank,
            band_index=band_index,
            time_index=time_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=max_parallel_chunk_reads,
        )
        aggregated = _aggregate_window_to_target_grid(
            full_window,
            x_start=x_start,
            y_start=y_start,
            x_idx_axis=aggregation_x_idx_axis,
            y_idx_axis=aggregation_y_idx_axis,
            width=width,
            height=height,
        )
        if np.any(np.isfinite(aggregated)):
            return aggregated
    return sampled


def generate_projected_band_tile(
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
    max_parallel_chunk_reads: int | None = None,
) -> tuple[bytes, tuple[float, float]]:
    reprojected = render_projected_band_array(
        connector=connector,
        entry=entry,
        variable=variable,
        bbox=xyz_to_web_mercator_bbox(z, x, y),
        width=TILE_SIZE,
        height=TILE_SIZE,
        time_index=time_index,
        max_source_oversample=1.0,
        max_parallel_chunk_reads=max_parallel_chunk_reads,
    )
    actual_vmin, actual_vmax = resolve_projected_display_range(entry, variable, reprojected, vmin, vmax)
    return encode_tile(reprojected, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)


def generate_projected_composite_tile(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    composite_id: str,
    z: int,
    x: int,
    y: int,
    time_index: int,
    vmin: float | None,
    vmax: float | None,
    max_parallel_chunk_reads: int | None = None,
) -> tuple[bytes, tuple[float, float]]:
    rgb_data = render_projected_composite_array(
        connector=connector,
        entry=entry,
        composite_id=composite_id,
        bbox=xyz_to_web_mercator_bbox(z, x, y),
        width=TILE_SIZE,
        height=TILE_SIZE,
        time_index=time_index,
        vmin=vmin,
        vmax=vmax,
        max_source_oversample=1.0,
        max_parallel_chunk_reads=max_parallel_chunk_reads,
    )
    finite = rgb_data[np.isfinite(rgb_data)]
    if finite.size == 0:
        return encode_rgb_tile(rgb_data), (0.0, 255.0)
    return encode_rgb_tile(rgb_data), (float(np.nanmin(finite)), float(np.nanmax(finite)))
