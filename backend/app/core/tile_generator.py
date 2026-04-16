import math

import numpy as np
import xarray as xr
from PIL import Image

from app.core.colormap import encode_tile
from app.core.variable_display import resolve_display_range
from app.models.dataset import DatasetMeta


def _resolve_coordinate_names(dataset: xr.Dataset) -> tuple[str, str]:
    lat_candidates = ("lat", "latitude", "y")
    lon_candidates = ("lon", "longitude", "x")

    lat_name = next((name for name in lat_candidates if name in dataset.coords), None)
    lon_name = next((name for name in lon_candidates if name in dataset.coords), None)
    if lat_name is None or lon_name is None:
        raise ValueError("Dataset must expose latitude and longitude coordinates")
    return lat_name, lon_name


def tile_to_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _extract_region(
    dataset: xr.Dataset,
    variable: str,
    time_index: int,
    bbox: tuple[float, float, float, float],
    tile_size: tuple[int, int] = (256, 256),
) -> np.ndarray:
    west, south, east, north = bbox
    lat_name, lon_name = _resolve_coordinate_names(dataset)
    lat_coord = dataset.coords[lat_name]
    lon_coord = dataset.coords[lon_name]
    south = max(south, float(lat_coord.min()))
    north = min(north, float(lat_coord.max()))

    lat_descending = float(lat_coord[0]) > float(lat_coord[-1])
    lat_slice = slice(north, south) if lat_descending else slice(south, north)

    variable_data = dataset[variable]
    if "time" in variable_data.dims:
        variable_data = variable_data.isel(time=time_index)

    data_array = variable_data.sel(
        {lat_name: lat_slice, lon_name: slice(west, east)}
    )
    if data_array.size == 0:
        return np.full(tile_size, np.nan, dtype=np.float32)

    values = data_array.values.astype(np.float32)
    image = Image.fromarray(values, mode="F").resize(tile_size, Image.Resampling.BILINEAR)
    return np.array(image, dtype=np.float32)


def resolve_range(meta: DatasetMeta, variable: str, vmin: float | None, vmax: float | None) -> tuple[float, float]:
    variable_meta = next(item for item in meta.variables if item.id == variable)
    return resolve_display_range(variable_meta, vmin, vmax)


def generate_tile(
    dataset: xr.Dataset,
    meta: DatasetMeta,
    variable: str,
    z: int,
    x: int,
    y: int,
    time_index: int,
    colormap: str,
    vmin: float | None,
    vmax: float | None,
) -> tuple[bytes, tuple[float, float]]:
    bbox = tile_to_bbox(z, x, y)
    values = _extract_region(dataset, variable, time_index, bbox)
    actual_vmin, actual_vmax = resolve_range(meta, variable, vmin, vmax)
    return encode_tile(values, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)
