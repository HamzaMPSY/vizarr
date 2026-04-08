import math

import numpy as np
from pyproj import CRS, Transformer

from app.core.colormap import encode_tile
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.zarr_v3 import load_4d_window


WEB_MERCATOR_HALF_WORLD = 20037508.342789244
TILE_SIZE = 256


def _xyz_to_web_mercator_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    west = x / n * (2 * WEB_MERCATOR_HALF_WORLD) - WEB_MERCATOR_HALF_WORLD
    east = (x + 1) / n * (2 * WEB_MERCATOR_HALF_WORLD) - WEB_MERCATOR_HALF_WORLD
    north = WEB_MERCATOR_HALF_WORLD - y / n * (2 * WEB_MERCATOR_HALF_WORLD)
    south = WEB_MERCATOR_HALF_WORLD - (y + 1) / n * (2 * WEB_MERCATOR_HALF_WORLD)
    return west, south, east, north


def _tile_pixel_centers(
    bbox: tuple[float, float, float, float],
    tile_size: int = TILE_SIZE,
) -> tuple[np.ndarray, np.ndarray]:
    west, south, east, north = bbox
    pixel_width = (east - west) / tile_size
    pixel_height = (north - south) / tile_size

    xs = west + (np.arange(tile_size, dtype=np.float64) + 0.5) * pixel_width
    ys = north - (np.arange(tile_size, dtype=np.float64) + 0.5) * pixel_height
    return np.meshgrid(xs, ys)


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

    top = top_left * (1.0 - wx) + top_right * wx
    bottom = bottom_left * (1.0 - wx) + bottom_right * wx
    sampled = top * (1.0 - wy) + bottom * wy

    result[valid] = sampled.astype(np.float32)
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
) -> tuple[bytes, tuple[float, float]]:
    entry = ensure_catalog_entry_ready(entry, connector)
    x_values = entry.x_values
    y_values = entry.y_values
    assert x_values is not None
    assert y_values is not None
    assert entry.data_array_meta is not None

    band_index = entry.band_indices[variable]
    web_bbox = _xyz_to_web_mercator_bbox(z, x, y)
    mercator_xs, mercator_ys = _tile_pixel_centers(web_bbox)
    target_crs = CRS.from_wkt(entry.crs_wkt) if entry.crs_wkt else CRS.from_epsg(4326)
    transformer = Transformer.from_crs("EPSG:3857", target_crs, always_xy=True)
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
        window_bounds = _source_window_bounds_from_indices(
            x_idx=x_idx_full,
            y_idx=y_idx_full,
            width=len(x_values),
            height=len(y_values),
        )
    else:
        x_idx_full = y_idx_full = None
        window_bounds = _source_window_bounds(
            x_values=x_values,
            y_values=y_values,
            xs=source_xs,
            ys=source_ys,
        )

    if window_bounds is None:
        empty = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
        selected = next(item for item in entry.meta.variables if item.id == variable)
        actual_vmin, actual_vmax = _resolve_display_range(
            data=empty,
            fallback_vmin=selected.stats.p02,
            fallback_vmax=selected.stats.p98,
            vmin=vmin,
            vmax=vmax,
        )
        return encode_tile(empty, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)

    x_start, x_stop, y_start, y_stop = window_bounds
    window = load_4d_window(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=entry.data_array_meta,
        time_index=time_index,
        band_index=band_index,
        y_start=y_start,
        y_stop=y_stop,
        x_start=x_start,
        x_stop=x_stop,
    ).astype(np.float32)

    local_x_values = x_values[x_start:x_stop]
    local_y_values = y_values[y_start:y_stop]
    if x_idx_full is not None and y_idx_full is not None:
        x_idx = x_idx_full - x_start
        y_idx = y_idx_full - y_start
    else:
        x_idx = _coordinate_to_fractional_index(local_x_values, source_xs)
        y_idx = _coordinate_to_fractional_index(local_y_values, source_ys)
    reprojected = _bilinear_sample(
        data=window,
        y_idx=y_idx,
        x_idx=x_idx,
    )

    selected = next(item for item in entry.meta.variables if item.id == variable)
    actual_vmin, actual_vmax = _resolve_display_range(
        data=reprojected,
        fallback_vmin=selected.stats.p02,
        fallback_vmax=selected.stats.p98,
        vmin=vmin,
        vmax=vmax,
    )
    return encode_tile(reprojected, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)
