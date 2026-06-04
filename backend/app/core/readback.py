import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr
from pyproj import CRS, Transformer

from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.datasets import DatasetRegistry
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.projected_tile_generator import _coordinate_to_fractional_index
from app.core.projected_tile_generator import _fractional_indices_from_geotransform
from app.core.tile_observability import TileRequestMetrics
from app.core.tile_observability import activate_tile_metrics
from app.core.zarr_v3 import load_2d_window
from app.core.zarr_v3 import load_3d_window
from app.core.zarr_v3 import load_4d_window
from app.models.artifacts import SourceBBoxReadbackResponse
from app.models.artifacts import SourcePointReadbackResponse
from app.models.artifacts import SourceReadbackDiagnostics
from app.models.dataset import DatasetMeta


@dataclass(frozen=True)
class ReadbackWindowTooLarge(ValueError):
    width: int
    height: int
    max_width: int
    max_height: int

    def __str__(self) -> str:
        return (
            f"Readback bbox window {self.width}x{self.height} exceeds "
            f"limit {self.max_width}x{self.max_height}"
        )


@dataclass(frozen=True)
class _ProjectedVariableContext:
    array_name: str
    data_array_meta: Any
    array_rank: int
    band_index: int | None
    unit: str | None


def read_synthetic_point(
    *,
    registry: DatasetRegistry,
    variable: str,
    lon: float,
    lat: float,
    time_index: int,
    include_diagnostics: bool,
) -> SourcePointReadbackResponse:
    data_array = _synthetic_variable(registry.dataset, variable, time_index)
    lat_name, lon_name = _resolve_coordinate_names(registry.dataset)
    lon_values = np.asarray(registry.dataset.coords[lon_name].values, dtype=np.float64)
    lat_values = np.asarray(registry.dataset.coords[lat_name].values, dtype=np.float64)
    unit = _variable_unit(registry.meta, variable)

    if not _coordinate_within_axis(lon_values, lon) or not _coordinate_within_axis(lat_values, lat):
        return SourcePointReadbackResponse(
            dataset_id=registry.meta.id,
            variable=variable,
            time_index=time_index,
            lon=lon,
            lat=lat,
            value=None,
            unit=unit,
            is_nodata=True,
            diagnostics=_diagnostics(
                storage_backend="synthetic",
                notes=["point is outside dataset coordinate extent"],
            )
            if include_diagnostics
            else None,
        )

    pixel_x = int(np.nanargmin(np.abs(lon_values - lon)))
    pixel_y = int(np.nanargmin(np.abs(lat_values - lat)))
    raw_value = np.asarray(data_array.values)[pixel_y, pixel_x]
    value, is_nodata = _json_value(raw_value, fill_value=None)
    return SourcePointReadbackResponse(
        dataset_id=registry.meta.id,
        variable=variable,
        time_index=time_index,
        lon=lon,
        lat=lat,
        value=value,
        unit=unit,
        is_nodata=is_nodata,
        pixel_x=pixel_x,
        pixel_y=pixel_y,
        diagnostics=_diagnostics(
            storage_backend="synthetic",
            source_window={"x_start": pixel_x, "x_stop": pixel_x + 1, "y_start": pixel_y, "y_stop": pixel_y + 1},
        )
        if include_diagnostics
        else None,
    )


def read_synthetic_bbox(
    *,
    registry: DatasetRegistry,
    variable: str,
    bbox: tuple[float, float, float, float],
    time_index: int,
    max_width: int,
    max_height: int,
    include_diagnostics: bool,
) -> SourceBBoxReadbackResponse:
    data_array = _synthetic_variable(registry.dataset, variable, time_index)
    lat_name, lon_name = _resolve_coordinate_names(registry.dataset)
    lon_values = np.asarray(registry.dataset.coords[lon_name].values, dtype=np.float64)
    lat_values = np.asarray(registry.dataset.coords[lat_name].values, dtype=np.float64)
    west, south, east, north = bbox

    if west <= east:
        lon_mask = (lon_values >= west) & (lon_values <= east)
    else:
        lon_mask = (lon_values >= west) | (lon_values <= east)
    lat_mask = (lat_values >= south) & (lat_values <= north)
    x_indices = np.where(lon_mask)[0]
    y_indices = np.where(lat_mask)[0]
    if x_indices.size == 0 or y_indices.size == 0:
        return _empty_bbox_response(
            dataset_id=registry.meta.id,
            variable=variable,
            time_index=time_index,
            bbox=bbox,
            unit=_variable_unit(registry.meta, variable),
            diagnostics=_diagnostics(
                storage_backend="synthetic",
                notes=["bbox does not intersect dataset coordinate centers"],
            )
            if include_diagnostics
            else None,
        )

    if int(x_indices.size) > max_width or int(y_indices.size) > max_height:
        raise ReadbackWindowTooLarge(int(x_indices.size), int(y_indices.size), max_width, max_height)

    values = np.asarray(data_array.values)[np.ix_(y_indices, x_indices)]
    json_values, valid_count = _json_grid(values, fill_value=None)
    return SourceBBoxReadbackResponse(
        dataset_id=registry.meta.id,
        variable=variable,
        time_index=time_index,
        bbox=list(bbox),
        shape=[len(json_values), len(json_values[0]) if json_values else 0],
        values=json_values,
        unit=_variable_unit(registry.meta, variable),
        valid_count=valid_count,
        diagnostics=_diagnostics(
            storage_backend="synthetic",
            source_window={
                "x_start": int(x_indices.min()),
                "x_stop": int(x_indices.max()) + 1,
                "y_start": int(y_indices.min()),
                "y_stop": int(y_indices.max()) + 1,
            },
        )
        if include_diagnostics
        else None,
    )


def read_projected_point(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    lon: float,
    lat: float,
    time_index: int,
    include_diagnostics: bool,
) -> SourcePointReadbackResponse:
    entry = ensure_catalog_entry_metadata_ready(entry, connector)
    context = _projected_variable_context(entry, variable)
    pixel = _source_pixel_for_lonlat(connector, entry, context, lon, lat)
    if pixel is None:
        return SourcePointReadbackResponse(
            dataset_id=entry.id,
            variable=variable,
            time_index=time_index,
            lon=lon,
            lat=lat,
            value=None,
            unit=context.unit,
            is_nodata=True,
            diagnostics=_diagnostics(
                storage_backend="oci_zarr",
                entry=entry,
                context=context,
                notes=["point is outside dataset extent"],
            )
            if include_diagnostics
            else None,
        )

    pixel_x, pixel_y = pixel
    metrics = TileRequestMetrics()
    with activate_tile_metrics(metrics):
        window = _load_source_window(
            connector=connector,
            entry=entry,
            context=context,
            time_index=time_index,
            y_start=pixel_y,
            y_stop=pixel_y + 1,
            x_start=pixel_x,
            x_stop=pixel_x + 1,
        )
    value, is_nodata = _json_value(window[0, 0], fill_value=context.data_array_meta.fill_value)
    return SourcePointReadbackResponse(
        dataset_id=entry.id,
        variable=variable,
        time_index=time_index,
        lon=lon,
        lat=lat,
        value=value,
        unit=context.unit,
        is_nodata=is_nodata,
        pixel_x=pixel_x,
        pixel_y=pixel_y,
        diagnostics=_diagnostics(
            storage_backend="oci_zarr",
            entry=entry,
            context=context,
            metrics=metrics,
            source_window={"x_start": pixel_x, "x_stop": pixel_x + 1, "y_start": pixel_y, "y_stop": pixel_y + 1},
        )
        if include_diagnostics
        else None,
    )


def read_projected_bbox(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    variable: str,
    bbox: tuple[float, float, float, float],
    time_index: int,
    max_width: int,
    max_height: int,
    include_diagnostics: bool,
) -> SourceBBoxReadbackResponse:
    entry = ensure_catalog_entry_metadata_ready(entry, connector)
    context = _projected_variable_context(entry, variable)
    window_bounds = _source_window_for_lonlat_bbox(connector, entry, context, bbox)
    if window_bounds is None:
        return _empty_bbox_response(
            dataset_id=entry.id,
            variable=variable,
            time_index=time_index,
            bbox=bbox,
            unit=context.unit,
            diagnostics=_diagnostics(
                storage_backend="oci_zarr",
                entry=entry,
                context=context,
                notes=["bbox does not intersect dataset extent"],
            )
            if include_diagnostics
            else None,
        )

    x_start, x_stop, y_start, y_stop = window_bounds
    width = x_stop - x_start
    height = y_stop - y_start
    if width > max_width or height > max_height:
        raise ReadbackWindowTooLarge(width, height, max_width, max_height)

    metrics = TileRequestMetrics()
    with activate_tile_metrics(metrics):
        window = _load_source_window(
            connector=connector,
            entry=entry,
            context=context,
            time_index=time_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
        )
    json_values, valid_count = _json_grid(window, fill_value=context.data_array_meta.fill_value)
    return SourceBBoxReadbackResponse(
        dataset_id=entry.id,
        variable=variable,
        time_index=time_index,
        bbox=list(bbox),
        shape=[height, width],
        values=json_values,
        unit=context.unit,
        valid_count=valid_count,
        diagnostics=_diagnostics(
            storage_backend="oci_zarr",
            entry=entry,
            context=context,
            metrics=metrics,
            source_window={"x_start": x_start, "x_stop": x_stop, "y_start": y_start, "y_stop": y_stop},
        )
        if include_diagnostics
        else None,
    )


def _synthetic_variable(dataset: xr.Dataset, variable: str, time_index: int) -> xr.DataArray:
    if variable not in dataset.data_vars:
        raise KeyError(variable)
    data_array = dataset[variable]
    if "time" in data_array.dims:
        if time_index >= int(data_array.sizes["time"]):
            raise IndexError("time_index is outside variable time range")
        return data_array.isel(time=time_index)
    return data_array


def _resolve_coordinate_names(dataset: xr.Dataset) -> tuple[str, str]:
    lat_name = next((name for name in ("lat", "latitude", "y") if name in dataset.coords), None)
    lon_name = next((name for name in ("lon", "longitude", "x") if name in dataset.coords), None)
    if lat_name is None or lon_name is None:
        raise ValueError("Dataset must expose latitude and longitude coordinates")
    return lat_name, lon_name


def _projected_variable_context(entry: CatalogEntry, variable: str) -> _ProjectedVariableContext:
    if variable not in entry.band_indices and variable not in entry.variable_array_names:
        raise KeyError(variable)
    array_name = entry.variable_array_names.get(variable, entry.data_array_name)
    data_array_meta = entry.data_array_metas.get(array_name) or entry.data_array_meta
    if data_array_meta is None:
        raise ValueError("Dataset metadata is incomplete")
    array_rank = len(data_array_meta.shape)
    band_index = entry.band_indices[variable] if array_rank == 4 else None
    return _ProjectedVariableContext(
        array_name=array_name,
        data_array_meta=data_array_meta,
        array_rank=array_rank,
        band_index=band_index,
        unit=_variable_unit(entry.meta, variable),
    )


def _load_source_window(
    *,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    context: _ProjectedVariableContext,
    time_index: int,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
) -> np.ndarray:
    _validate_time_index(context, time_index)
    if context.array_rank == 2:
        return load_2d_window(
            connector=connector,
            store_path=entry.path,
            array_name=context.array_name,
            metadata=context.data_array_meta,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=1,
        )
    if context.array_rank == 3:
        return load_3d_window(
            connector=connector,
            store_path=entry.path,
            array_name=context.array_name,
            metadata=context.data_array_meta,
            time_index=time_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=1,
        )
    if context.array_rank == 4:
        if context.band_index is None:
            raise ValueError("4D readback requires a band index")
        return load_4d_window(
            connector=connector,
            store_path=entry.path,
            array_name=context.array_name,
            metadata=context.data_array_meta,
            time_index=time_index,
            band_index=context.band_index,
            y_start=y_start,
            y_stop=y_stop,
            x_start=x_start,
            x_stop=x_stop,
            max_parallel_chunk_reads=1,
        )
    raise ValueError(f"Unsupported source array rank: {context.array_rank}")


def _validate_time_index(context: _ProjectedVariableContext, time_index: int) -> None:
    if context.array_rank == 2:
        if time_index != 0:
            raise IndexError("time_index is outside static variable time range")
        return
    time_size = int(context.data_array_meta.shape[0])
    if time_index >= time_size:
        raise IndexError("time_index is outside variable time range")


def _source_pixel_for_lonlat(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    context: _ProjectedVariableContext,
    lon: float,
    lat: float,
) -> tuple[int, int] | None:
    source_x, source_y = _lonlat_to_source_xy(entry, lon, lat)
    x_idx, y_idx = _source_fractional_indices(connector, entry, context, np.asarray([[source_x]]), np.asarray([[source_y]]))
    if x_idx is None or y_idx is None or not math.isfinite(float(x_idx[0, 0])) or not math.isfinite(float(y_idx[0, 0])):
        return None
    pixel_x = int(round(float(x_idx[0, 0])))
    pixel_y = int(round(float(y_idx[0, 0])))
    width = int(context.data_array_meta.shape[-1])
    height = int(context.data_array_meta.shape[-2])
    if pixel_x < 0 or pixel_x >= width or pixel_y < 0 or pixel_y >= height:
        return None
    return pixel_x, pixel_y


def _source_window_for_lonlat_bbox(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    context: _ProjectedVariableContext,
    bbox: tuple[float, float, float, float],
) -> tuple[int, int, int, int] | None:
    west, south, east, north = bbox
    corners = [(west, south), (west, north), (east, south), (east, north)]
    source_points = [_lonlat_to_source_xy(entry, lon, lat) for lon, lat in corners]
    source_xs = np.asarray([[point[0] for point in source_points]], dtype=np.float64)
    source_ys = np.asarray([[point[1] for point in source_points]], dtype=np.float64)
    x_idx, y_idx = _source_fractional_indices(connector, entry, context, source_xs, source_ys)
    if x_idx is None or y_idx is None:
        return None

    finite = np.isfinite(x_idx) & np.isfinite(y_idx)
    if not np.any(finite):
        return None
    width = int(context.data_array_meta.shape[-1])
    height = int(context.data_array_meta.shape[-2])
    x_start = max(int(math.floor(float(np.nanmin(x_idx[finite])))), 0)
    x_stop = min(int(math.ceil(float(np.nanmax(x_idx[finite])))) + 1, width)
    y_start = max(int(math.floor(float(np.nanmin(y_idx[finite])))), 0)
    y_stop = min(int(math.ceil(float(np.nanmax(y_idx[finite])))) + 1, height)
    if x_start >= x_stop or y_start >= y_stop:
        return None
    return x_start, x_stop, y_start, y_stop


def _source_fractional_indices(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    context: _ProjectedVariableContext,
    source_xs: np.ndarray,
    source_ys: np.ndarray,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    if entry.geo_transform is not None:
        return _fractional_indices_from_geotransform(entry.geo_transform, source_xs, source_ys)

    ready = ensure_catalog_entry_ready(entry, connector)
    if ready.x_values is None or ready.y_values is None:
        return None, None
    return (
        _coordinate_to_fractional_index(ready.x_values, source_xs),
        _coordinate_to_fractional_index(ready.y_values, source_ys),
    )


def _lonlat_to_source_xy(entry: CatalogEntry, lon: float, lat: float) -> tuple[float, float]:
    source_crs = CRS.from_wkt(entry.crs_wkt) if entry.crs_wkt else CRS.from_epsg(4326)
    transformer = Transformer.from_crs("EPSG:4326", source_crs, always_xy=True)
    source_x, source_y = transformer.transform(lon, lat)
    return float(source_x), float(source_y)


def _json_grid(values: np.ndarray, *, fill_value: Any) -> tuple[list[list[int | float | bool | None]], int]:
    rows: list[list[int | float | bool | None]] = []
    valid_count = 0
    for row in values:
        json_row: list[int | float | bool | None] = []
        for value in row:
            json_value, is_nodata = _json_value(value, fill_value=fill_value)
            if not is_nodata:
                valid_count += 1
            json_row.append(json_value)
        rows.append(json_row)
    return rows, valid_count


def _json_value(value: Any, *, fill_value: Any) -> tuple[int | float | bool | None, bool]:
    scalar = value.item() if hasattr(value, "item") else value
    if fill_value is not None:
        try:
            if scalar == fill_value:
                return None, True
        except (TypeError, ValueError):
            pass
    if isinstance(scalar, (bool, np.bool_)):
        return bool(scalar), False
    if isinstance(scalar, (int, np.integer)):
        return int(scalar), False
    try:
        numeric = float(scalar)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(numeric):
        return None, True
    return numeric, False


def _empty_bbox_response(
    *,
    dataset_id: str,
    variable: str,
    time_index: int,
    bbox: tuple[float, float, float, float],
    unit: str | None,
    diagnostics: SourceReadbackDiagnostics | None,
) -> SourceBBoxReadbackResponse:
    return SourceBBoxReadbackResponse(
        dataset_id=dataset_id,
        variable=variable,
        time_index=time_index,
        bbox=list(bbox),
        shape=[0, 0],
        values=[],
        unit=unit,
        valid_count=0,
        diagnostics=diagnostics,
    )


def _diagnostics(
    *,
    storage_backend: str,
    entry: CatalogEntry | None = None,
    context: _ProjectedVariableContext | None = None,
    metrics: TileRequestMetrics | None = None,
    source_window: dict[str, int | None] | None = None,
    notes: list[str] | None = None,
) -> SourceReadbackDiagnostics:
    snapshot = metrics.snapshot() if metrics is not None else {}
    return SourceReadbackDiagnostics(
        storage_backend=storage_backend,
        source_path=entry.path if entry is not None else None,
        array_name=context.array_name if context is not None else None,
        dtype=str(context.data_array_meta.dtype) if context is not None else None,
        source_crs=(entry.meta.crs_authority or entry.crs_wkt) if entry is not None else None,
        source_window=source_window,
        chunk_shape=list(context.data_array_meta.effective_chunk_shape) if context is not None else None,
        object_get_count=int(snapshot.get("object_get_count", 0)),
        byte_range_get_count=int(snapshot.get("byte_range_get_count", 0)),
        object_bytes_read=int(snapshot.get("object_bytes_read", 0)),
        zarr_chunk_count=int(snapshot.get("chunk_reads", 0)),
        zarr_shard_index_reads=int(snapshot.get("shard_index_reads", 0)),
        notes=notes or [],
    )


def _variable_unit(meta: DatasetMeta, variable: str) -> str | None:
    selected = next((item for item in meta.variables if item.id == variable), None)
    return selected.unit if selected is not None else None


def _coordinate_within_axis(values: np.ndarray, coordinate: float) -> bool:
    if values.size == 0:
        return False
    lower = min(float(values[0]), float(values[-1]))
    upper = max(float(values[0]), float(values[-1]))
    return lower <= coordinate <= upper
