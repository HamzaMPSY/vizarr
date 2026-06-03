from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.core.colormap import encode_tile
from app.core.dataset_catalog import CatalogEntry
from app.core.multiscale_store import extract_level_array_metadata
from app.core.multiscale_store import extract_level_attributes
from app.core.multiscale_store import extract_multiscale_paths
from app.core.multiscale_store import read_root_store_metadata
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.projected_tile_generator import render_projected_band_array
from app.core.projected_tile_generator import resolve_projected_display_range
from app.core.projected_tile_generator import xyz_to_web_mercator_bbox


TILE_SIZE = 256


@dataclass(frozen=True)
class PyramidLevel:
    level_path: str
    zoom: int
    tile_x_min: int
    tile_x_max: int
    tile_y_min: int
    tile_y_max: int
    shape: tuple[int, int, int, int]
    chunks: tuple[int, int, int, int]
    dtype: np.dtype
    dimension_separator: str


def generate_pyramid_tile(
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
    data = render_pyramid_tile_array(
        connector=connector,
        store_path=entry.meta.multiscale_store_path,
        data_array_name=entry.data_array_name,
        variable=variable,
        variable_ids=[item.id for item in entry.meta.variables],
        z=z,
        x=x,
        y=y,
        time_index=time_index,
    )
    actual_vmin, actual_vmax = resolve_projected_display_range(entry, variable, data, vmin, vmax)
    return encode_tile(data, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)


def generate_and_cache_pyramid_tile(
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
    data = render_projected_band_array(
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
    store_pyramid_tile_array(
        connector=connector,
        store_path=entry.meta.multiscale_store_path,
        data_array_name=entry.data_array_name,
        variable=variable,
        variable_ids=[item.id for item in entry.meta.variables],
        z=z,
        x=x,
        y=y,
        time_index=time_index,
        data=data,
    )
    actual_vmin, actual_vmax = resolve_projected_display_range(entry, variable, data, vmin, vmax)
    return encode_tile(data, colormap, actual_vmin, actual_vmax), (actual_vmin, actual_vmax)


def render_pyramid_tile_array(
    *,
    connector: OCIObjectStorageConnector,
    store_path: str | None,
    data_array_name: str,
    variable: str,
    variable_ids: list[str],
    z: int,
    x: int,
    y: int,
    time_index: int,
) -> np.ndarray:
    if not store_path:
        return _empty_tile()

    level = load_pyramid_level_metadata(
        connector=connector,
        store_path=store_path,
        data_array_name=data_array_name,
        zoom=z,
    )
    if level is None:
        return _empty_tile()
    if x < level.tile_x_min or x > level.tile_x_max or y < level.tile_y_min or y > level.tile_y_max:
        return _empty_tile()
    if time_index < 0 or time_index >= level.shape[0]:
        return _empty_tile()

    try:
        band_index = variable_ids.index(variable)
    except ValueError:
        return _empty_tile()
    if band_index >= level.shape[1]:
        return _empty_tile()

    local_tile_y = y - level.tile_y_min
    local_tile_x = x - level.tile_x_min
    chunk_key = level.dimension_separator.join(
        str(item) for item in (time_index, band_index, local_tile_y, local_tile_x)
    )
    object_path = f"{store_path.rstrip('/')}/{level.level_path}/{data_array_name}/{chunk_key}"

    try:
        payload = connector.read_bytes(object_path, use_cache=True)
    except FileNotFoundError:
        raise FileNotFoundError(object_path)

    expected_shape = (
        min(level.chunks[2], level.shape[2] - (local_tile_y * level.chunks[2])),
        min(level.chunks[3], level.shape[3] - (local_tile_x * level.chunks[3])),
    )
    count = expected_shape[0] * expected_shape[1]
    tile = np.frombuffer(payload, dtype=level.dtype, count=count).copy().reshape(expected_shape)
    if expected_shape != (TILE_SIZE, TILE_SIZE):
        padded = _empty_tile()
        padded[: expected_shape[0], : expected_shape[1]] = tile
        return padded
    return tile.astype(np.float32, copy=False)


def store_pyramid_tile_array(
    *,
    connector: OCIObjectStorageConnector,
    store_path: str | None,
    data_array_name: str,
    variable: str,
    variable_ids: list[str],
    z: int,
    x: int,
    y: int,
    time_index: int,
    data: np.ndarray,
) -> bool:
    if not store_path:
        return False

    level = load_pyramid_level_metadata(
        connector=connector,
        store_path=store_path,
        data_array_name=data_array_name,
        zoom=z,
    )
    if level is None:
        return False
    if x < level.tile_x_min or x > level.tile_x_max or y < level.tile_y_min or y > level.tile_y_max:
        return False
    if time_index < 0 or time_index >= level.shape[0]:
        return False
    try:
        band_index = variable_ids.index(variable)
    except ValueError:
        return False
    if band_index >= level.shape[1]:
        return False

    local_tile_y = y - level.tile_y_min
    local_tile_x = x - level.tile_x_min
    object_path = _chunk_object_path(
        store_path=store_path,
        level=level,
        data_array_name=data_array_name,
        time_index=time_index,
        band_index=band_index,
        local_tile_y=local_tile_y,
        local_tile_x=local_tile_x,
    )
    payload = np.asarray(data, dtype=level.dtype).tobytes(order="C")
    connector.write_bytes(object_path, payload)
    return True


def load_pyramid_level_metadata(
    *,
    connector: OCIObjectStorageConnector,
    store_path: str,
    data_array_name: str,
    zoom: int,
) -> PyramidLevel | None:
    store_metadata = read_root_store_metadata(connector, store_path)
    for level_path in extract_multiscale_paths(store_metadata):
        attrs = extract_level_attributes(store_metadata, level_path)
        if not attrs:
            attrs = extract_level_attributes(
                store_metadata,
                level_path,
                connector=connector,
                store_path=store_path,
            )
        level_zoom = _as_int(attrs.get("zoom"))
        if level_zoom is None and level_path.isdigit():
            level_zoom = int(level_path)
        if level_zoom != zoom:
            continue

        array_metadata = extract_level_array_metadata(
            store_metadata,
            level_path,
            data_array_name,
            connector=connector,
            store_path=store_path,
        )
        shape = _as_int_tuple(array_metadata.get("shape"))
        chunks = _as_int_tuple(array_metadata.get("chunks"))
        if len(shape) != 4 or len(chunks) != 4:
            return None

        return PyramidLevel(
            level_path=level_path,
            zoom=zoom,
            tile_x_min=_required_int(attrs, "tile_x_min"),
            tile_x_max=_required_int(attrs, "tile_x_max"),
            tile_y_min=_required_int(attrs, "tile_y_min"),
            tile_y_max=_required_int(attrs, "tile_y_max"),
            shape=shape,
            chunks=chunks,
            dtype=np.dtype(array_metadata.get("dtype", "<f4")),
            dimension_separator=str(array_metadata.get("dimension_separator", ".")),
        )
    return None


def _chunk_object_path(
    *,
    store_path: str,
    level: PyramidLevel,
    data_array_name: str,
    time_index: int,
    band_index: int,
    local_tile_y: int,
    local_tile_x: int,
) -> str:
    chunk_key = level.dimension_separator.join(
        str(item) for item in (time_index, band_index, local_tile_y, local_tile_x)
    )
    return f"{store_path.rstrip('/')}/{level.level_path}/{data_array_name}/{chunk_key}"


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = _as_int(payload.get(key))
    if value is None:
        raise ValueError(f"Multiscale level metadata is missing {key}")
    return value


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    items: list[int] = []
    for item in value:
        parsed = _as_int(item)
        if parsed is None:
            return ()
        items.append(parsed)
    return tuple(items)


def _empty_tile() -> np.ndarray:
    return np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
