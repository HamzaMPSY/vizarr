from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from pyproj import CRS
from pyproj import Transformer
import zarr

from app.config import get_settings
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.zarr_reader import open_dataset_from_path


logger = logging.getLogger(__name__)


DEFAULT_EXCLUDED_COLUMNS = {
    "year",
    "month",
    "day",
    "date",
    "timestamp",
    "time",
}

DEFAULT_READ_WORKERS = 8
DEFAULT_WRITE_BATCH = 10
DEFAULT_MAX_GRID_CELLS = 50_000_000
DEFAULT_CHUNK_SIZE = 256
DEFAULT_ZARR_V3_SHARD_SIZE = 4096
GRID_COORD_DECIMALS = 12


@dataclass(frozen=True)
class ConversionConfig:
    x_column: str
    y_column: str
    value_columns: tuple[str, ...] | None
    layout: str
    timestamp_column: str | None
    timestamp_regex: str | None
    x_dim: str
    y_dim: str
    y_descending: bool
    dtype: str
    crs: str | None
    max_grid_cells: int
    x_resolution: float | None
    y_resolution: float | None
    cell_aggregation: str
    string_cell_aggregation: str
    shard_size: int = DEFAULT_ZARR_V3_SHARD_SIZE
    source_crs: str | None = None
    x_snap_origin: float | None = None
    y_snap_origin: float | None = None


@dataclass(frozen=True)
class InputTimeSlice:
    source_uri: str
    timestamp: np.datetime64


@dataclass(frozen=True)
class ExistingStoreContext:
    x_values: np.ndarray
    y_values: np.ndarray
    time_values: np.ndarray
    value_columns: tuple[str, ...]
    layout: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read Spark-partitioned Parquet folders from OCI Object Storage and write them "
            "into one Zarr store with a time dimension."
        )
    )
    parser.add_argument(
        "--links-file",
        required=True,
        help="Path to a text file containing one parquet folder URI/path per line.",
    )
    parser.add_argument(
        "--output-store",
        required=True,
        help=(
            "Destination Zarr store path. Accepts either an OCI URI "
            "(oci://bucket@namespace/prefix/output.zarr) or a bucket-relative path."
        ),
    )
    parser.add_argument("--x-column", required=True, help="Column holding the x/lon coordinate.")
    parser.add_argument("--y-column", required=True, help="Column holding the y/lat coordinate.")
    parser.add_argument(
        "--value-columns",
        help="Comma-separated value columns to write. If omitted, numeric non-coordinate columns are used.",
    )
    parser.add_argument(
        "--layout",
        choices=("bands", "per-variable"),
        default="bands",
        help=(
            "Output value layout. 'bands' writes a viewer-compatible 4D cube "
            "(time, band, y, x). 'per-variable' writes one 3D variable per value column. Default: bands."
        ),
    )
    parser.add_argument(
        "--timestamp-column",
        help="Column or Spark partition column holding the timestamp for each parquet folder.",
    )
    parser.add_argument(
        "--timestamp-regex",
        help=(
            "Regex used against the parquet folder path when the timestamp is not stored in a column. "
            "Use a named group 'ts' or a single capture group."
        ),
    )
    parser.add_argument("--x-dim", default="x", help="Output Zarr x dimension name.")
    parser.add_argument("--y-dim", default="y", help="Output Zarr y dimension name.")
    parser.add_argument(
        "--y-order",
        choices=("ascending", "descending"),
        default="descending",
        help="Order to use for the y coordinate in the output grid.",
    )
    parser.add_argument(
        "--dtype",
        default="float32",
        help="Output dtype for data variables. Default: float32.",
    )
    parser.add_argument(
        "--crs",
        help="CRS for the grid, for example EPSG:32629 or EPSG:4326. Adds spatial_ref metadata.",
    )
    parser.add_argument(
        "--source-crs",
        default="EPSG:4326",
        help=(
            "CRS of the input x/y columns before rasterization. "
            "Use a projected output --crs plus metre resolutions for fixed-grid products such as Sentinel-2 10 m. "
            "Default: EPSG:4326."
        ),
    )
    parser.add_argument(
        "--x-resolution",
        type=_positive_float,
        help="Optional x-cell size used to snap point coordinates onto a regular grid before writing Zarr.",
    )
    parser.add_argument(
        "--y-resolution",
        type=_positive_float,
        help="Optional y-cell size used to snap point coordinates onto a regular grid before writing Zarr.",
    )
    parser.add_argument(
        "--x-snap-origin",
        type=float,
        help="Optional x snap origin in the output CRS. If omitted, it is inferred once from all inputs.",
    )
    parser.add_argument(
        "--y-snap-origin",
        type=float,
        help="Optional y snap origin in the output CRS. If omitted, it is inferred once from all inputs.",
    )
    parser.add_argument(
        "--cell-aggregation",
        default="mean",
        choices=("mean", "first", "min", "max"),
        help="How to combine multiple parquet rows that land in the same output grid cell. Default: mean.",
    )
    parser.add_argument(
        "--string-cell-aggregation",
        default="first",
        choices=("first", "mode"),
        help="How to combine string/categorical values when multiple rows land in the same grid cell. Default: first.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the destination store if it already exists.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing store if it already exists.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Inner chunk size to use for y/x dimensions in the output Zarr store. Default: 256.",
    )
    parser.add_argument(
        "--shard-size",
        type=_positive_int,
        default=DEFAULT_ZARR_V3_SHARD_SIZE,
        help=(
            "Shard size to use for y/x dimensions in Zarr v3 output stores. "
            "Ignored for Zarr v2. Default: 4096."
        ),
    )
    parser.add_argument(
        "--zarr-version",
        type=int,
        choices=(2, 3),
        default=3,
        help="Zarr format version to write. Default: 3.",
    )
    parser.add_argument(
        "--read-workers",
        type=_positive_int,
        default=DEFAULT_READ_WORKERS,
        help=f"Number of parallel workers for parquet reads. Default: {DEFAULT_READ_WORKERS}.",
    )
    parser.add_argument(
        "--write-batch",
        type=_positive_int,
        default=DEFAULT_WRITE_BATCH,
        help=f"Number of time steps to concatenate before each Zarr write. Default: {DEFAULT_WRITE_BATCH}.",
    )
    parser.add_argument(
        "--max-grid-cells",
        type=_positive_int,
        default=DEFAULT_MAX_GRID_CELLS,
        help=(
            "Maximum allowed dense grid size as unique_y * unique_x before the run fails fast. "
            f"Default: {DEFAULT_MAX_GRID_CELLS}."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity for conversion progress messages. Default: INFO.",
    )
    return parser.parse_args()


def _load_links(path: str) -> list[str]:
    logger.info("Loading parquet links from %s", path)
    items: list[str] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    if not items:
        raise ValueError(f"No parquet links found in {path}")
    logger.info("Loaded %d parquet link(s)", len(items))
    logger.debug("Parquet links: %s", items)
    return items


def _normalize_oci_uri(raw_path: str, connector: OCIObjectStorageConnector) -> str:
    if raw_path.startswith("oci://"):
        logger.debug("Using explicit OCI URI: %s", raw_path)
        return raw_path
    normalized = connector.build_oci_uri(raw_path)
    logger.debug("Normalized bucket-relative path %s -> %s", raw_path, normalized)
    return normalized


def _mapper_path_from_oci_uri(oci_uri: str) -> str:
    return oci_uri.removeprefix("oci://")


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {raw!r}")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive float, got {raw!r}")
    return value


def _validate_storage_layout(chunk_size: int, shard_size: int, zarr_version: int) -> None:
    if zarr_version != 3:
        return
    if shard_size < chunk_size:
        raise ValueError(
            f"Zarr v3 shard size must be >= chunk size, got shard_size={shard_size} chunk_size={chunk_size}"
        )
    if shard_size % chunk_size != 0:
        raise ValueError(
            f"Zarr v3 shard size must be an integer multiple of chunk size, got shard_size={shard_size} "
            f"chunk_size={chunk_size}"
        )


def _required_columns(config: ConversionConfig) -> list[str] | None:
    if config.value_columns is None:
        return None

    columns: list[str] = [config.x_column, config.y_column]
    if config.timestamp_column:
        columns.append(config.timestamp_column)
    columns.extend(config.value_columns)
    return columns


def _first_value(series: pd.Series) -> Any:
    non_null = series.dropna()
    if non_null.empty:
        return None
    return non_null.iloc[0]


def _mode_value(series: pd.Series) -> Any:
    non_null = series.dropna()
    if non_null.empty:
        return None
    modes = non_null.mode()
    if modes.empty:
        return non_null.iloc[0]
    return modes.iloc[0]


def _is_auth_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        status = getattr(current, "status", None)
        code = getattr(current, "code", None)
        if status == 401 or code == "NotAuthenticated":
            return True
        current = current.__cause__ or current.__context__
    return False


def _raise_auth_expired(error: BaseException) -> None:
    raise RuntimeError(
        "OCI authentication expired during parquet-to-zarr conversion. "
        "Refresh the OCI session on the host, restart the backend container, and rerun the command."
    ) from error


def _read_partitioned_parquet(
    filesystem: Any,
    parquet_uri: str,
    columns: list[str] | None,
) -> pd.DataFrame:
    import pyarrow.dataset as ds
    import pyarrow.fs as pafs

    pyfs = pafs.PyFileSystem(pafs.FSSpecHandler(filesystem))
    dataset = ds.dataset(
        _mapper_path_from_oci_uri(parquet_uri),
        filesystem=pyfs,
        format="parquet",
        partitioning="hive",
    )
    logger.debug("Reading parquet dataset at %s", parquet_uri)
    try:
        table = dataset.to_table(columns=columns)
    except Exception as error:
        if _is_auth_error(error):
            logger.error("OCI auth failure while reading parquet dataset %s", parquet_uri)
            _raise_auth_expired(error)
        raise
    frame = table.to_pandas()
    logger.info(
        "Read parquet dataset %s with %d row(s), %d column(s)",
        parquet_uri,
        len(frame.index),
        len(frame.columns),
    )
    logger.debug("Columns for %s: %s", parquet_uri, list(frame.columns))
    return frame


def _read_all_parallel(
    parquet_uris: list[str],
    columns: list[str] | None,
    filesystem: Any,
    max_workers: int,
) -> dict[str, pd.DataFrame]:
    if not parquet_uris:
        return {}

    if max_workers == 1 or len(parquet_uris) == 1:
        return {
            parquet_uri: _read_partitioned_parquet(filesystem, parquet_uri, columns)
            for parquet_uri in parquet_uris
        }

    results: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(parquet_uris))) as executor:
        futures = {
            executor.submit(_read_partitioned_parquet, filesystem, parquet_uri, columns): parquet_uri
            for parquet_uri in parquet_uris
        }
        for future in as_completed(futures):
            parquet_uri = futures[future]
            results[parquet_uri] = future.result()
            logger.debug("Parallel read completed for %s", parquet_uri)
    return results


def _detect_value_columns(df: pd.DataFrame, config: ConversionConfig) -> tuple[str, ...]:
    if config.value_columns is not None:
        missing = [column for column in config.value_columns if column not in df.columns]
        if missing:
            raise ValueError(f"Missing requested value columns: {missing}")
        logger.info("Using explicit value columns: %s", list(config.value_columns))
        return config.value_columns

    excluded = {
        config.x_column,
        config.y_column,
        config.timestamp_column,
        *DEFAULT_EXCLUDED_COLUMNS,
    }
    numeric = [
        column
        for column in df.columns
        if column not in excluded and pd.api.types.is_numeric_dtype(df[column])
    ]
    if not numeric:
        raise ValueError("Unable to infer value columns. Pass --value-columns explicitly.")
    logger.info("Inferred value columns: %s", numeric)
    return tuple(numeric)


def _extract_timestamps(df: pd.DataFrame, parquet_uri: str, config: ConversionConfig) -> list[np.datetime64]:
    if config.timestamp_column:
        if config.timestamp_column not in df.columns:
            raise ValueError(f"Timestamp column '{config.timestamp_column}' not found in parquet data")
        parsed = pd.to_datetime(pd.Series(df[config.timestamp_column]).dropna())
        unique = parsed.drop_duplicates().sort_values()
        timestamps = [np.datetime64(value.to_datetime64()) for value in unique.tolist()]
        if not timestamps:
            raise ValueError(f"Timestamp column '{config.timestamp_column}' did not contain any non-null values")
        if len(timestamps) == 1:
            logger.info(
                "Extracted timestamp %s from column %s for %s",
                timestamps[0],
                config.timestamp_column,
                parquet_uri,
            )
        else:
            logger.info(
                "Extracted %d timestamp values from column %s for %s",
                len(timestamps),
                config.timestamp_column,
                parquet_uri,
            )
        return timestamps

    if config.timestamp_regex:
        match = re.search(config.timestamp_regex, parquet_uri)
        if not match:
            raise ValueError(f"Timestamp regex did not match parquet path: {parquet_uri}")
        captured = match.groupdict().get("ts") or match.group(1)
        timestamp = np.datetime64(pd.Timestamp(captured).to_datetime64())
        logger.info(
            "Extracted timestamp %s from path using regex %s",
            timestamp,
            config.timestamp_regex,
        )
        return [timestamp]

    raise ValueError("Provide either --timestamp-column or --timestamp-regex")


def _extract_timestamp(df: pd.DataFrame, parquet_uri: str, config: ConversionConfig) -> np.datetime64:
    timestamps = _extract_timestamps(df, parquet_uri, config)
    if len(timestamps) != 1:
        raise ValueError(
            f"Expected exactly one timestamp value for {parquet_uri}, found {len(timestamps)}. "
            "Split the frame by timestamp before building the dataset slice."
        )
    return timestamps[0]


def _slice_frame_for_timestamp(
    df: pd.DataFrame,
    config: ConversionConfig,
    timestamp: np.datetime64,
) -> pd.DataFrame:
    if config.timestamp_column is None:
        return df.copy()

    parsed = pd.to_datetime(df[config.timestamp_column]).to_numpy(dtype="datetime64[ns]")
    selected = np.datetime64(pd.Timestamp(timestamp).to_datetime64())
    return df.loc[parsed == selected].copy()


def _build_input_time_slices(
    parquet_uris: list[str],
    coordinate_frames: dict[str, pd.DataFrame],
    config: ConversionConfig,
) -> list[InputTimeSlice]:
    slices: list[InputTimeSlice] = []
    for parquet_uri in parquet_uris:
        timestamps = _extract_timestamps(coordinate_frames[parquet_uri], parquet_uri, config)
        slices.extend(
            InputTimeSlice(source_uri=parquet_uri, timestamp=timestamp)
            for timestamp in timestamps
        )
    return slices


def _extract_existing_value_columns(dataset: xr.Dataset) -> tuple[tuple[str, ...], str]:
    if "bands" in dataset.data_vars:
        labels = dataset["bands"].attrs.get("band_labels")
        if not isinstance(labels, list) or not labels:
            raise ValueError("Existing bands dataset is missing non-empty 'band_labels' metadata")
        return tuple(str(item) for item in labels), "bands"

    columns = tuple(name for name in dataset.data_vars if name != "spatial_ref")
    if not columns:
        raise ValueError("Existing store does not expose any data variables")
    return columns, "per-variable"


def _load_existing_store_context(
    connector: OCIObjectStorageConnector,
    output_store_uri: str,
    config: ConversionConfig,
) -> ExistingStoreContext:
    dataset = open_dataset_from_path(
        connector=connector,
        zarr_path=output_store_uri,
        consolidated=True,
    )
    try:
        if config.x_dim not in dataset.coords or config.y_dim not in dataset.coords or "time" not in dataset.coords:
            raise ValueError(
                f"Existing store {output_store_uri} is missing one of the expected coordinates: "
                f"{config.x_dim}, {config.y_dim}, time"
            )
        x_values = np.asarray(dataset[config.x_dim].values, dtype=np.float64)
        y_values = np.asarray(dataset[config.y_dim].values, dtype=np.float64)
        time_values = pd.to_datetime(dataset["time"].values).to_numpy(dtype="datetime64[ns]")
        value_columns, layout = _extract_existing_value_columns(dataset)
        return ExistingStoreContext(
            x_values=x_values,
            y_values=y_values,
            time_values=time_values,
            value_columns=value_columns,
            layout=layout,
        )
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def _existing_grid_snap_origin(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    return float(values[0])


def _validate_expected_columns_against_existing(
    expected_columns: tuple[str, ...],
    existing_columns: tuple[str, ...],
    output_store_uri: str,
) -> None:
    if expected_columns != existing_columns:
        raise ValueError(
            f"Value columns for append do not match existing store {output_store_uri}: "
            f"new={list(expected_columns)} existing={list(existing_columns)}"
        )


def _resize_sparse_store_time_axis(
    filesystem: Any,
    output_store_uri: str,
    config: ConversionConfig,
    *,
    existing_context: ExistingStoreContext,
    final_time_values: list[np.datetime64],
) -> None:
    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    root = zarr.open_group(store=mapper, mode="a")
    final_time_length = len(final_time_values)

    if existing_context.layout == "bands":
        root["bands"].resize((final_time_length, len(existing_context.value_columns), len(existing_context.y_values), len(existing_context.x_values)))
    else:
        for column in existing_context.value_columns:
            root[column].resize((final_time_length, len(existing_context.y_values), len(existing_context.x_values)))

    root["time"].resize((final_time_length,))
    root["time"][:] = np.asarray([pd.Timestamp(item).value for item in final_time_values], dtype=np.int64)


def _validate_append_frames_fit_existing_grid(
    coordinate_frames: dict[str, pd.DataFrame],
    parquet_uris: list[str],
    config: ConversionConfig,
    existing_context: ExistingStoreContext,
    output_store_uri: str,
) -> None:
    for parquet_uri in parquet_uris:
        prepared = _prepare_coordinate_frame(coordinate_frames[parquet_uri], config)
        x_positions = pd.Index(existing_context.x_values).get_indexer(prepared[config.x_column].astype(float))
        y_positions = pd.Index(existing_context.y_values).get_indexer(prepared[config.y_column].astype(float))
        valid = (x_positions >= 0) & (y_positions >= 0)
        if not np.all(valid):
            missing = int((~valid).sum())
            raise ValueError(
                f"Append input {parquet_uri} does not fit the existing target grid for {output_store_uri}: "
                f"{missing} point(s) fall outside the stored x/y coordinates"
            )


def _sorted_coordinate_values(series: pd.Series, descending: bool) -> np.ndarray:
    values = np.asarray(sorted(pd.unique(series.astype(float))), dtype=np.float64)
    if descending:
        values = values[::-1]
    return values


def _regular_step(values: np.ndarray) -> float | None:
    if len(values) < 2:
        return None
    diffs = np.diff(values.astype(np.float64))
    step = float(diffs[0])
    if np.allclose(diffs, step, rtol=0.0, atol=max(abs(step) * 1e-6, 1e-9)):
        return step
    return None


def _build_spatial_ref_attrs(
    x_values: np.ndarray,
    y_values: np.ndarray,
    crs_value: str,
) -> dict[str, Any]:
    crs = CRS.from_user_input(crs_value)
    attrs: dict[str, Any] = {
        "crs_wkt": crs.to_wkt(),
    }
    x_step = _regular_step(x_values)
    y_step = _regular_step(y_values)
    if x_step is not None and y_step is not None:
        origin_x = float(x_values[0] - (x_step / 2.0))
        origin_y = float(y_values[0] - (y_step / 2.0))
        attrs["GeoTransform"] = f"{origin_x} {float(x_step)} 0.0 {origin_y} 0.0 {float(y_step)}"
    return attrs


def _build_grid_array(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    values: np.ndarray,
    x_values: np.ndarray,
    y_values: np.ndarray,
    dtype: str,
) -> np.ndarray:
    x_index = pd.Index(x_values)
    y_index = pd.Index(y_values)

    x_positions = x_index.get_indexer(df[x_col].astype(float))
    y_positions = y_index.get_indexer(df[y_col].astype(float))

    grid = np.full((len(y_values), len(x_values)), np.nan, dtype=dtype)
    valid = (x_positions >= 0) & (y_positions >= 0)
    grid[y_positions[valid], x_positions[valid]] = values[valid]
    return grid[np.newaxis, :, :]


def _crs_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left == right
    return CRS.from_user_input(left) == CRS.from_user_input(right)


def _encode_value_column(
    frame: pd.DataFrame,
    column: str,
    target_dtype: str,
) -> tuple[np.ndarray, str, dict[str, Any]]:
    series = frame[column]
    if pd.api.types.is_numeric_dtype(series):
        return series.to_numpy(dtype=target_dtype), target_dtype, {}

    non_null = series.dropna()
    categories = sorted({str(value) for value in non_null.tolist()})
    mapping = {category: index for index, category in enumerate(categories)}
    encoded = series.map(lambda value: mapping.get(str(value), -1) if pd.notna(value) else -1).to_numpy(dtype="float32")
    attrs = {
        "categorical_encoding": {str(index): category for category, index in mapping.items()},
        "_FillValue": -1,
        "original_dtype": str(series.dtype),
    }
    logger.info("Encoded string column %s into %d categorical code(s)", column, len(categories))
    return encoded, "float32", attrs


def _transform_coordinate_frame(df: pd.DataFrame, config: ConversionConfig) -> pd.DataFrame:
    frame = df.copy()
    frame[config.x_column] = frame[config.x_column].astype(np.float64)
    frame[config.y_column] = frame[config.y_column].astype(np.float64)

    if config.crs and config.source_crs and not _crs_equal(config.source_crs, config.crs):
        transformer = Transformer.from_crs(config.source_crs, config.crs, always_xy=True)
        x_values, y_values = transformer.transform(
            frame[config.x_column].to_numpy(dtype=np.float64),
            frame[config.y_column].to_numpy(dtype=np.float64),
        )
        frame[config.x_column] = np.asarray(x_values, dtype=np.float64)
        frame[config.y_column] = np.asarray(y_values, dtype=np.float64)
    return frame


def _infer_snap_origin(values: np.ndarray, resolution: float) -> float:
    remainders = np.mod(values.astype(np.float64), resolution)
    return float(np.round(np.median(remainders), decimals=GRID_COORD_DECIMALS))


def _snap_to_resolution(series: pd.Series, resolution: float, origin: float | None = None) -> pd.Series:
    values = series.astype(np.float64).to_numpy()
    anchor = 0.0 if origin is None else float(origin)
    snapped = anchor + np.round((values - anchor) / resolution) * resolution
    snapped = np.round(snapped, decimals=GRID_COORD_DECIMALS)
    return pd.Series(snapped, index=series.index, dtype=np.float64)


def _prepare_spatial_frame(
    df: pd.DataFrame,
    config: ConversionConfig,
    value_columns: tuple[str, ...],
) -> pd.DataFrame:
    columns = [config.x_column, config.y_column, *value_columns]
    frame = _transform_coordinate_frame(df[columns].copy(), config)

    if config.x_resolution is not None:
        frame[config.x_column] = _snap_to_resolution(
            frame[config.x_column],
            config.x_resolution,
            config.x_snap_origin,
        )
    if config.y_resolution is not None:
        frame[config.y_column] = _snap_to_resolution(
            frame[config.y_column],
            config.y_resolution,
            config.y_snap_origin,
        )

    duplicate_mask = frame.duplicated(subset=[config.y_column, config.x_column], keep=False)
    duplicate_count = int(duplicate_mask.sum())
    if duplicate_count:
        if not value_columns:
            logger.info(
                "Deduplicating %d coordinate-only row(s) while resolving the shared target grid",
                duplicate_count,
            )
            frame = frame.drop_duplicates(
                subset=[config.y_column, config.x_column],
                keep="first",
            ).reset_index(drop=True)
            return frame

        numeric_columns = [
            column for column in value_columns if pd.api.types.is_numeric_dtype(frame[column])
        ]
        non_numeric_columns = [column for column in value_columns if column not in numeric_columns]
        aggregation_map: dict[str, Any] = {}
        for column in numeric_columns:
            aggregation_map[column] = config.cell_aggregation
        for column in non_numeric_columns:
            aggregation_map[column] = _first_value if config.string_cell_aggregation == "first" else _mode_value
        logger.info(
            "Aggregating %d row(s) into shared grid cells using numeric=%s string=%s",
            duplicate_count,
            config.cell_aggregation,
            config.string_cell_aggregation,
        )
        frame = (
            frame.groupby([config.y_column, config.x_column], as_index=False, sort=False)
            .agg(aggregation_map)
        )

    return frame


def _prepare_coordinate_frame(
    df: pd.DataFrame,
    config: ConversionConfig,
) -> pd.DataFrame:
    return _prepare_spatial_frame(df, config, ())


def _build_regular_axis(
    minimum: float,
    maximum: float,
    resolution: float,
    descending: bool,
) -> np.ndarray:
    steps = int(round((maximum - minimum) / resolution))
    values = minimum + (np.arange(steps + 1, dtype=np.float64) * resolution)
    values = np.round(values, decimals=GRID_COORD_DECIMALS)
    if descending:
        values = values[::-1]
    return values


def _load_coordinate_frames(
    filesystem: Any,
    parquet_uris: list[str],
    first_frame: pd.DataFrame,
    config: ConversionConfig,
    max_workers: int,
) -> dict[str, pd.DataFrame]:
    coordinate_columns = [config.x_column, config.y_column]
    if config.timestamp_column:
        coordinate_columns.append(config.timestamp_column)
    coordinate_frames: dict[str, pd.DataFrame] = {
        parquet_uris[0]: first_frame[coordinate_columns].copy(),
    }
    remaining_uris = parquet_uris[1:]
    if remaining_uris:
        coordinate_frames.update(
            _read_all_parallel(
                parquet_uris=remaining_uris,
                columns=coordinate_columns,
                filesystem=filesystem,
                max_workers=max_workers,
            )
        )
    return coordinate_frames


def _resolve_snap_origins(
    coordinate_frames: dict[str, pd.DataFrame],
    parquet_uris: list[str],
    config: ConversionConfig,
) -> tuple[float | None, float | None]:
    if config.x_resolution is None and config.y_resolution is None:
        return config.x_snap_origin, config.y_snap_origin
    if config.x_snap_origin is not None and config.y_snap_origin is not None:
        return config.x_snap_origin, config.y_snap_origin

    transformed_frames = [
        _transform_coordinate_frame(coordinate_frames[parquet_uri], config)
        for parquet_uri in parquet_uris
    ]
    resolved_x_origin = config.x_snap_origin
    resolved_y_origin = config.y_snap_origin

    if config.x_resolution is not None and resolved_x_origin is None:
        all_x = np.concatenate([frame[config.x_column].to_numpy(dtype=np.float64) for frame in transformed_frames])
        resolved_x_origin = _infer_snap_origin(all_x, config.x_resolution)
    if config.y_resolution is not None and resolved_y_origin is None:
        all_y = np.concatenate([frame[config.y_column].to_numpy(dtype=np.float64) for frame in transformed_frames])
        resolved_y_origin = _infer_snap_origin(all_y, config.y_resolution)

    logger.info(
        "Resolved shared snap origins: x_origin=%s y_origin=%s (source_crs=%s, output_crs=%s)",
        resolved_x_origin,
        resolved_y_origin,
        config.source_crs,
        config.crs,
    )
    return resolved_x_origin, resolved_y_origin


def _resolve_target_grid(
    parquet_uris: list[str],
    coordinate_frames: dict[str, pd.DataFrame],
    config: ConversionConfig,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    if len(parquet_uris) <= 1:
        return None, None

    prepared_frames = [
        _prepare_coordinate_frame(coordinate_frames[parquet_uri], config)
        for parquet_uri in parquet_uris
    ]
    x_min = min(float(frame[config.x_column].min()) for frame in prepared_frames if not frame.empty)
    x_max = max(float(frame[config.x_column].max()) for frame in prepared_frames if not frame.empty)
    y_min = min(float(frame[config.y_column].min()) for frame in prepared_frames if not frame.empty)
    y_max = max(float(frame[config.y_column].max()) for frame in prepared_frames if not frame.empty)

    if config.x_resolution is not None:
        target_x = _build_regular_axis(
            minimum=x_min,
            maximum=x_max,
            resolution=config.x_resolution,
            descending=False,
        )
    else:
        target_x = np.asarray(
            sorted(
                {
                    float(value)
                    for frame in prepared_frames
                    for value in pd.unique(frame[config.x_column].astype(float))
                }
            ),
            dtype=np.float64,
        )

    if config.y_resolution is not None:
        target_y = _build_regular_axis(
            minimum=y_min,
            maximum=y_max,
            resolution=config.y_resolution,
            descending=config.y_descending,
        )
    else:
        target_y = np.asarray(
            sorted(
                {
                    float(value)
                    for frame in prepared_frames
                    for value in pd.unique(frame[config.y_column].astype(float))
                }
            ),
            dtype=np.float64,
        )
        if config.y_descending:
            target_y = target_y[::-1]

    estimated_cells = len(target_x) * len(target_y)

    logger.info(
        "Resolved shared target grid across %d parquet input(s): unique_x=%d, unique_y=%d, estimated_cells=%d",
        len(parquet_uris),
        len(target_x),
        len(target_y),
        estimated_cells,
    )
    if estimated_cells > config.max_grid_cells:
        logger.warning(
            "Combined target grid across %d parquet input(s) exceeds the dense-grid limit: unique_x=%d, unique_y=%d, estimated_cells=%d, limit=%d. Falling back to sparse chunked writes.",
            len(parquet_uris),
            len(target_x),
            len(target_y),
            estimated_cells,
            config.max_grid_cells,
        )
    logger.debug(
        "Shared target grid details: x[min=%s, max=%s], y[min=%s, max=%s]",
        target_x[0] if len(target_x) else None,
        target_x[-1] if len(target_x) else None,
        target_y[-1] if len(target_y) else None,
        target_y[0] if len(target_y) else None,
    )
    return target_x, target_y


def _validate_dense_grid_shape(
    df: pd.DataFrame,
    parquet_uri: str,
    x_values: np.ndarray,
    y_values: np.ndarray,
    config: ConversionConfig,
) -> None:
    estimated_cells = len(x_values) * len(y_values)
    row_count = len(df.index)
    density = row_count / estimated_cells if estimated_cells else 0.0

    logger.info(
        "Dense grid estimate for %s: rows=%d, unique_x=%d, unique_y=%d, estimated_cells=%d, density=%.8f",
        parquet_uri,
        row_count,
        len(x_values),
        len(y_values),
        estimated_cells,
        density,
    )

    if estimated_cells > config.max_grid_cells:
        extra_hint = ""
        if "QUADKEY" in df.columns:
            extra_hint = (
                " The parquet also has a QUADKEY column, which strongly suggests this is point/tile-indexed "
                "data rather than a dense regular raster grid."
            )
        if config.x_resolution is None and config.y_resolution is None:
            extra_hint += " Pass --x-resolution and --y-resolution to bin point coordinates onto a regular grid."
        raise ValueError(
            f"Dense grid too large for {parquet_uri}: unique_x={len(x_values)}, unique_y={len(y_values)}, "
            f"estimated_cells={estimated_cells}, limit={config.max_grid_cells}.{extra_hint}"
        )

    if config.x_resolution is not None or config.y_resolution is not None:
        if density < 0.1:
            logger.warning(
                "Snapped grid for %s is sparse (density=%.8f). Proceeding because an explicit output resolution was provided.",
                parquet_uri,
                density,
            )
        return

    if row_count > 0 and estimated_cells > row_count * 4:
        extra_hint = ""
        if "QUADKEY" in df.columns:
            extra_hint = " QUADKEY is present, so this dataset likely needs a quadkey/point aggregation path."
        if config.x_resolution is None and config.y_resolution is None:
            extra_hint += " Pass --x-resolution and --y-resolution to rasterize point coordinates."
        raise ValueError(
            f"Input does not look like a dense regular grid for {parquet_uri}: rows={row_count}, "
            f"unique_x={len(x_values)}, unique_y={len(y_values)}, estimated_cells={estimated_cells}, "
            f"density={density:.8f}.{extra_hint}"
        )


def _grid_dataset(
    df: pd.DataFrame,
    parquet_uri: str,
    config: ConversionConfig,
    expected_x: np.ndarray | None,
    expected_y: np.ndarray | None,
) -> xr.Dataset:
    logger.info("Building xarray dataset for %s", parquet_uri)
    value_columns = _detect_value_columns(df, config)
    timestamp = _extract_timestamp(df, parquet_uri, config)
    spatial_df = _prepare_spatial_frame(df, config, value_columns)
    logger.info(
        "Prepared spatial frame for %s: input_rows=%d, output_rows=%d",
        parquet_uri,
        len(df.index),
        len(spatial_df.index),
    )
    if config.x_resolution is not None or config.y_resolution is not None:
        logger.info(
            "Using snapped grid resolution for %s: x_resolution=%s, y_resolution=%s",
            parquet_uri,
            config.x_resolution,
            config.y_resolution,
        )

    x_values = _sorted_coordinate_values(spatial_df[config.x_column], descending=False)
    y_values = _sorted_coordinate_values(spatial_df[config.y_column], descending=config.y_descending)
    logger.info(
        "Grid for %s has %d x value(s) and %d y value(s)",
        parquet_uri,
        len(x_values),
        len(y_values),
    )
    logger.debug(
        "Grid details for %s: x[min=%s, max=%s], y[min=%s, max=%s], y_descending=%s",
        parquet_uri,
        x_values[0] if len(x_values) else None,
        x_values[-1] if len(x_values) else None,
        y_values[-1] if len(y_values) else None,
        y_values[0] if len(y_values) else None,
        config.y_descending,
    )
    _validate_dense_grid_shape(spatial_df, parquet_uri, x_values, y_values, config)

    target_x_values = expected_x if expected_x is not None else x_values
    target_y_values = expected_y if expected_y is not None else y_values
    if expected_x is not None and not np.array_equal(expected_x, x_values):
        logger.info(
            "Reindexing %s onto shared x grid: local=%d target=%d",
            parquet_uri,
            len(x_values),
            len(target_x_values),
        )
    if expected_y is not None and not np.array_equal(expected_y, y_values):
        logger.info(
            "Reindexing %s onto shared y grid: local=%d target=%d",
            parquet_uri,
            len(y_values),
            len(target_y_values),
        )

    data_vars: dict[str, tuple[tuple[str, ...], np.ndarray]] = {}
    variable_attrs: dict[str, dict[str, Any]] = {}
    encoded_arrays: list[np.ndarray] = []
    for column in value_columns:
        encoded_values, array_dtype, attrs = _encode_value_column(spatial_df, column, config.dtype)
        array = _build_grid_array(
            df=spatial_df,
            x_col=config.x_column,
            y_col=config.y_column,
            values=encoded_values,
            x_values=target_x_values,
            y_values=target_y_values,
            dtype=array_dtype,
        )
        encoded_arrays.append(array)
        if config.layout == "per-variable":
            data_vars[column] = (
                ("time", config.y_dim, config.x_dim),
                array,
            )
        variable_attrs[column] = attrs
        logger.debug(
            "Prepared variable %s for %s with shape %s and dtype %s",
            column,
            parquet_uri,
            array.shape,
            array_dtype,
        )

    coords: dict[str, Any] = {
        "time": np.asarray([timestamp], dtype="datetime64[ns]"),
        config.x_dim: target_x_values,
        config.y_dim: target_y_values,
    }
    if config.layout == "bands":
        if not encoded_arrays:
            raise ValueError(f"No value columns were selected for {parquet_uri}")
        common_dtype = np.result_type(*(array.dtype for array in encoded_arrays))
        band_cube = np.stack(
            [array.astype(common_dtype, copy=False) for array in encoded_arrays],
            axis=1,
        )
        data_vars["bands"] = (
            ("time", "band", config.y_dim, config.x_dim),
            band_cube,
        )
        coords["band"] = np.asarray(value_columns, dtype=str)

    dataset = xr.Dataset(data_vars=data_vars, coords=coords)
    dataset.attrs["source_input_rows"] = int(len(df.index))
    dataset.attrs["source_output_rows"] = int(len(spatial_df.index))
    dataset[config.x_dim].attrs["axis"] = "X"
    dataset[config.y_dim].attrs["axis"] = "Y"
    if config.layout == "bands":
        dataset["band"].attrs["long_name"] = "band"
        dataset["bands"].attrs["band_labels"] = list(value_columns)
        per_band_attrs = {
            column: attrs
            for column, attrs in variable_attrs.items()
            if attrs
        }
        if per_band_attrs:
            dataset["bands"].attrs["band_metadata"] = json.dumps(per_band_attrs, sort_keys=True)
    else:
        for column, attrs in variable_attrs.items():
            dataset[column].attrs.update(attrs)

    if config.crs:
        dataset["spatial_ref"] = xr.DataArray(np.int32(0))
        dataset["spatial_ref"].attrs.update(_build_spatial_ref_attrs(target_x_values, target_y_values, config.crs))
        if config.source_crs:
            dataset["spatial_ref"].attrs["source_crs"] = config.source_crs
        if config.x_snap_origin is not None:
            dataset["spatial_ref"].attrs["x_snap_origin"] = float(config.x_snap_origin)
        if config.y_snap_origin is not None:
            dataset["spatial_ref"].attrs["y_snap_origin"] = float(config.y_snap_origin)
        if config.layout == "bands":
            dataset["bands"].attrs["grid_mapping"] = "spatial_ref"
        else:
            for column in value_columns:
                dataset[column].attrs["grid_mapping"] = "spatial_ref"
        logger.info("Attached CRS metadata %s to dataset for %s", config.crs, parquet_uri)

    logger.info(
        "Built dataset for %s with variables %s",
        parquet_uri,
        [name for name in dataset.data_vars if name != "spatial_ref"],
    )
    return dataset


def _build_ingest_summary(
    *,
    parquet_uri: str,
    timestamp: np.datetime64,
    config: ConversionConfig,
    value_columns: tuple[str, ...],
    x_values: np.ndarray,
    y_values: np.ndarray,
    input_rows: int,
    output_rows: int,
) -> dict[str, Any]:
    stored_variables = ["bands"] if config.layout == "bands" else list(value_columns)
    return {
        "source": parquet_uri,
        "timestamp": str(timestamp),
        "variables": stored_variables,
        "value_columns": list(value_columns),
        "input_rows": int(input_rows),
        "output_rows": int(output_rows),
        "aggregation_ratio": (float(output_rows) / float(input_rows)) if input_rows else None,
        "shape": {
            "time": 1,
            config.y_dim: int(len(y_values)),
            config.x_dim: int(len(x_values)),
        },
    }


def _data_chunk_shape(variable: xr.DataArray, dataset: xr.Dataset, chunk_size: int) -> tuple[int, ...] | None:
    if not variable.dims or variable.dims[0] != "time":
        return None
    if len(variable.dims) == 3:
        return (
            1,
            min(chunk_size, dataset.sizes[variable.dims[1]]),
            min(chunk_size, dataset.sizes[variable.dims[2]]),
        )
    if len(variable.dims) == 4:
        return (
            1,
            1,
            min(chunk_size, dataset.sizes[variable.dims[2]]),
            min(chunk_size, dataset.sizes[variable.dims[3]]),
        )
    return None


def _data_shard_shape(variable: xr.DataArray, shard_size: int) -> tuple[int, ...] | None:
    if not variable.dims or variable.dims[0] != "time":
        return None
    if len(variable.dims) == 3:
        return (1, shard_size, shard_size)
    if len(variable.dims) == 4:
        return (1, 1, shard_size, shard_size)
    return None


def _build_chunk_encoding(
    dataset: xr.Dataset,
    chunk_size: int,
    shard_size: int | None,
    zarr_version: int,
) -> dict[str, dict[str, Any]]:
    chunks: dict[str, dict[str, Any]] = {}
    for variable_name, variable in dataset.data_vars.items():
        chunk_shape = _data_chunk_shape(variable, dataset, chunk_size)
        if chunk_shape is None:
            continue
        variable_encoding: dict[str, Any] = {"chunks": chunk_shape}
        if zarr_version == 3 and shard_size is not None:
            variable_encoding["shards"] = _data_shard_shape(variable, shard_size)
        chunks[variable_name] = variable_encoding
    return chunks


def _build_to_zarr_kwargs(
    dataset: xr.Dataset,
    mode: str,
    append_dim: str | None,
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "mode": mode,
        "append_dim": append_dim,
        "encoding": _build_chunk_encoding(
            dataset=dataset,
            chunk_size=chunk_size,
            shard_size=shard_size if zarr_version == 3 else None,
            zarr_version=zarr_version,
        ),
        "zarr_version": zarr_version,
        "zarr_format": zarr_version,
    }
    kwargs["consolidated"] = True
    return kwargs


def _create_sparse_store(
    filesystem: Any,
    output_store_uri: str,
    config: ConversionConfig,
    x_values: np.ndarray,
    y_values: np.ndarray,
    timestamps: list[np.datetime64],
    value_columns: tuple[str, ...],
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
) -> None:
    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    root = zarr.open_group(store=mapper, mode="w", zarr_format=zarr_version)

    if config.layout == "bands":
        bands_kwargs: dict[str, Any] = {}
        if zarr_version == 3:
            bands_kwargs["shards"] = (1, 1, shard_size, shard_size)
        bands = root.create_array(
            "bands",
            shape=(len(timestamps), len(value_columns), len(y_values), len(x_values)),
            chunks=(1, 1, min(chunk_size, len(y_values)), min(chunk_size, len(x_values))),
            dtype=np.dtype(config.dtype),
            fill_value=np.nan,
            dimension_names=("time", "band", config.y_dim, config.x_dim),
            **bands_kwargs,
        )
        bands.attrs["band_labels"] = list(value_columns)
        bands.attrs["grid_mapping"] = "spatial_ref"
    else:
        for column in value_columns:
            array_kwargs: dict[str, Any] = {}
            if zarr_version == 3:
                array_kwargs["shards"] = (1, shard_size, shard_size)
            array = root.create_array(
                column,
                shape=(len(timestamps), len(y_values), len(x_values)),
                chunks=(1, min(chunk_size, len(y_values)), min(chunk_size, len(x_values))),
                dtype=np.dtype(config.dtype),
                fill_value=np.nan,
                dimension_names=("time", config.y_dim, config.x_dim),
                **array_kwargs,
            )
            array.attrs["grid_mapping"] = "spatial_ref"

    root.create_array(
        config.x_dim,
        data=np.asarray(x_values, dtype=np.float64),
        chunks=(min(chunk_size, len(x_values)),),
        dimension_names=(config.x_dim,),
    ).attrs["axis"] = "X"
    root.create_array(
        config.y_dim,
        data=np.asarray(y_values, dtype=np.float64),
        chunks=(min(chunk_size, len(y_values)),),
        dimension_names=(config.y_dim,),
    ).attrs["axis"] = "Y"
    root.create_array(
        "band",
        data=np.arange(len(value_columns), dtype=np.int32),
        chunks=(max(1, min(len(value_columns), chunk_size)),),
        dimension_names=("band",),
    ).attrs["long_name"] = "band"
    root.create_array(
        "time",
        data=np.asarray([pd.Timestamp(item).value for item in timestamps], dtype=np.int64),
        chunks=(max(1, min(len(timestamps), chunk_size)),),
        dimension_names=("time",),
    ).attrs.update(
        {
            "units": "nanoseconds since 1970-01-01T00:00:00",
            "calendar": "proleptic_gregorian",
        }
    )
    spatial_ref = root.create_array(
        "spatial_ref",
        data=np.asarray(0, dtype=np.int32),
        dimension_names=(),
    )
    if config.crs:
        spatial_ref.attrs.update(_build_spatial_ref_attrs(x_values, y_values, config.crs))
    if config.source_crs:
        spatial_ref.attrs["source_crs"] = config.source_crs
    if config.x_snap_origin is not None:
        spatial_ref.attrs["x_snap_origin"] = float(config.x_snap_origin)
    if config.y_snap_origin is not None:
        spatial_ref.attrs["y_snap_origin"] = float(config.y_snap_origin)
    root.attrs.update(
        {
            "vizarr_layout": "sharded-v3" if zarr_version == 3 else "chunked-v2",
            "vizarr_inner_chunk_size": int(chunk_size),
            "vizarr_shard_size": int(shard_size) if zarr_version == 3 else None,
        }
    )


def _write_sparse_time_slice(
    filesystem: Any,
    output_store_uri: str,
    parquet_uri: str,
    frame: pd.DataFrame,
    config: ConversionConfig,
    x_values: np.ndarray,
    y_values: np.ndarray,
    time_index: int,
    value_columns: tuple[str, ...],
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
) -> dict[str, Any]:
    spatial_df = _prepare_spatial_frame(frame, config, value_columns)
    logger.info(
        "Prepared sparse-write spatial frame for %s: input_rows=%d, output_rows=%d",
        parquet_uri,
        len(frame.index),
        len(spatial_df.index),
    )
    timestamp = _extract_timestamp(frame, parquet_uri, config)

    x_index = pd.Index(x_values)
    y_index = pd.Index(y_values)
    x_positions = x_index.get_indexer(spatial_df[config.x_column].astype(float))
    y_positions = y_index.get_indexer(spatial_df[config.y_column].astype(float))
    valid = (x_positions >= 0) & (y_positions >= 0)
    if not np.all(valid):
        missing = int((~valid).sum())
        raise ValueError(f"Failed to map {missing} row(s) onto the shared target grid for {parquet_uri}")

    encoded_by_column: dict[str, np.ndarray] = {}
    per_column_attrs: dict[str, dict[str, Any]] = {}
    for column in value_columns:
        encoded_values, _array_dtype, attrs = _encode_value_column(spatial_df, column, config.dtype)
        encoded_by_column[column] = encoded_values[valid]
        per_column_attrs[column] = attrs

    positions = pd.DataFrame(
        {
            "x_pos": x_positions[valid],
            "y_pos": y_positions[valid],
        }
    )
    write_unit = shard_size if zarr_version == 3 else chunk_size
    positions["chunk_x"] = positions["x_pos"] // write_unit
    positions["chunk_y"] = positions["y_pos"] // write_unit
    positions["local_x"] = positions["x_pos"] % write_unit
    positions["local_y"] = positions["y_pos"] % write_unit

    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    root = zarr.open_group(store=mapper, mode="a", zarr_format=zarr_version)
    data_array = root["bands"] if config.layout == "bands" else None

    for (chunk_y, chunk_x), group in positions.groupby(["chunk_y", "chunk_x"], sort=False):
        row_ids = group.index.to_numpy(dtype=np.int64)
        y_start = int(chunk_y) * write_unit
        x_start = int(chunk_x) * write_unit
        y_stop = min(y_start + write_unit, len(y_values))
        x_stop = min(x_start + write_unit, len(x_values))
        chunk_height = y_stop - y_start
        chunk_width = x_stop - x_start
        local_y = group["local_y"].to_numpy(dtype=np.int64)
        local_x = group["local_x"].to_numpy(dtype=np.int64)

        if config.layout == "bands":
            block = np.full((1, len(value_columns), chunk_height, chunk_width), np.nan, dtype=np.float32)
            for band_index, column in enumerate(value_columns):
                block[0, band_index, local_y, local_x] = encoded_by_column[column][row_ids]
            data_array[time_index : time_index + 1, :, y_start:y_stop, x_start:x_stop] = block
        else:
            for column in value_columns:
                block = np.full((1, chunk_height, chunk_width), np.nan, dtype=np.float32)
                block[0, local_y, local_x] = encoded_by_column[column][row_ids]
                root[column][time_index : time_index + 1, y_start:y_stop, x_start:x_stop] = block

    if config.layout == "per-variable":
        for column, attrs in per_column_attrs.items():
            if attrs:
                root[column].attrs.update(attrs)
    elif any(per_column_attrs.values()):
        root["bands"].attrs["band_metadata"] = json.dumps(
            {column: attrs for column, attrs in per_column_attrs.items() if attrs},
            sort_keys=True,
        )

    return _build_ingest_summary(
        parquet_uri=parquet_uri,
        timestamp=timestamp,
        config=config,
        value_columns=value_columns,
        x_values=x_values,
        y_values=y_values,
        input_rows=len(frame.index),
        output_rows=len(spatial_df.index),
    )


def _write_sparse_inputs(
    filesystem: Any,
    batch_frames: dict[str, pd.DataFrame],
    batch_slices: list[InputTimeSlice],
    output_store_uri: str,
    config: ConversionConfig,
    x_values: np.ndarray,
    y_values: np.ndarray,
    timestamps: list[np.datetime64],
    time_indices: dict[tuple[str, np.datetime64], int],
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
    is_first_write: bool,
) -> list[dict[str, Any]]:
    if is_first_write:
        _create_sparse_store(
            filesystem=filesystem,
            output_store_uri=output_store_uri,
            config=config,
            x_values=x_values,
            y_values=y_values,
            timestamps=timestamps,
            value_columns=config.value_columns or (),
            chunk_size=chunk_size,
            shard_size=shard_size,
            zarr_version=zarr_version,
        )

    summaries: list[dict[str, Any]] = []
    for time_slice in batch_slices:
        parquet_uri = time_slice.source_uri
        frame = _slice_frame_for_timestamp(
            batch_frames[parquet_uri],
            config=config,
            timestamp=time_slice.timestamp,
        )
        summaries.append(
            _write_sparse_time_slice(
                filesystem=filesystem,
                output_store_uri=output_store_uri,
                parquet_uri=parquet_uri,
                frame=frame,
                config=config,
                x_values=x_values,
                y_values=y_values,
                time_index=time_indices[(time_slice.source_uri, time_slice.timestamp)],
                value_columns=config.value_columns or (),
                chunk_size=chunk_size,
                shard_size=shard_size,
                zarr_version=zarr_version,
            )
        )
    return summaries


def _write_dataset(
    filesystem: Any,
    dataset: xr.Dataset,
    output_store_uri: str,
    append: bool,
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
) -> None:
    mapper_path = _mapper_path_from_oci_uri(output_store_uri)
    store_exists = filesystem.exists(mapper_path)
    if store_exists and not append:
        raise ValueError(f"Output store already exists: {output_store_uri}. Pass --append or --overwrite.")

    mode = "a" if store_exists and append else "w"
    append_dim = "time" if store_exists and append else None
    mapper = filesystem.get_mapper(mapper_path)
    logger.info(
        "Writing dataset to %s with mode=%s append_dim=%s chunk_size=%d shard_size=%d zarr_version=%d",
        output_store_uri,
        mode,
        append_dim,
        chunk_size,
        shard_size,
        zarr_version,
    )
    try:
        dataset.to_zarr(
            mapper,
            **_build_to_zarr_kwargs(
                dataset=dataset,
                mode=mode,
                append_dim=append_dim,
                chunk_size=chunk_size,
                shard_size=shard_size,
                zarr_version=zarr_version,
            ),
        )
    except Exception as error:
        if _is_auth_error(error):
            logger.error("OCI auth failure while writing Zarr store %s", output_store_uri)
            _raise_auth_expired(error)
        raise
    logger.info("Finished writing dataset to %s", output_store_uri)


def _flush_batch(
    datasets: list[xr.Dataset],
    filesystem: Any,
    output_store_uri: str,
    is_first_write: bool,
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
) -> None:
    if not datasets:
        return

    combined = xr.concat(datasets, dim="time") if len(datasets) > 1 else datasets[0]
    logger.info(
        "Flushing batch with %d time step(s) to %s",
        int(combined.sizes["time"]),
        output_store_uri,
    )
    _write_dataset(
        filesystem=filesystem,
        dataset=combined,
        output_store_uri=output_store_uri,
        append=not is_first_write,
        chunk_size=chunk_size,
        shard_size=shard_size,
        zarr_version=zarr_version,
    )


def _consolidate_output_store(
    filesystem: Any,
    output_store_uri: str,
    zarr_version: int,
) -> None:
    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    logger.info("Consolidating metadata for %s", output_store_uri)
    zarr.consolidate_metadata(mapper, zarr_format=zarr_version)


def _append_inputs(
    connector: OCIObjectStorageConnector,
    parquet_links: list[str],
    output_store_uri: str,
    config: ConversionConfig,
    overwrite: bool,
    append: bool,
    chunk_size: int,
    shard_size: int,
    read_workers: int,
    write_batch: int,
    zarr_version: int,
) -> list[dict[str, Any]]:
    _validate_storage_layout(
        chunk_size=chunk_size,
        shard_size=shard_size,
        zarr_version=zarr_version,
    )
    filesystem = connector.get_filesystem()
    summaries: list[dict[str, Any]] = []
    expected_x: np.ndarray | None = None
    expected_y: np.ndarray | None = None
    existing_context: ExistingStoreContext | None = None

    if overwrite:
        mapper_path = _mapper_path_from_oci_uri(output_store_uri)
        if filesystem.exists(mapper_path):
            logger.warning("Removing existing output store before overwrite: %s", output_store_uri)
            filesystem.rm(mapper_path, recursive=True)
        else:
            logger.info("Overwrite requested but output store does not exist yet: %s", output_store_uri)

    normalized_links = [_normalize_oci_uri(parquet_link, connector) for parquet_link in parquet_links]
    mapper_path = _mapper_path_from_oci_uri(output_store_uri)
    if append and filesystem.exists(mapper_path):
        existing_context = _load_existing_store_context(
            connector=connector,
            output_store_uri=output_store_uri,
            config=config,
        )
        expected_x = existing_context.x_values
        expected_y = existing_context.y_values
        logger.info(
            "Loaded existing target grid from %s for append: time=%d, x=%d, y=%d",
            output_store_uri,
            len(existing_context.time_values),
            len(expected_x),
            len(expected_y),
        )
    first_uri = normalized_links[0]
    first_frame = _read_partitioned_parquet(filesystem, first_uri, columns=None)
    value_columns = _detect_value_columns(first_frame, config)
    config = replace(config, value_columns=value_columns)
    if existing_context is not None:
        _validate_expected_columns_against_existing(
            expected_columns=value_columns,
            existing_columns=existing_context.value_columns,
            output_store_uri=output_store_uri,
        )
    columns_to_read = _required_columns(config)
    logger.info("Locked value columns for all files: %s", list(value_columns))
    logger.debug("Columns selected for subsequent reads: %s", columns_to_read)
    coordinate_frames = _load_coordinate_frames(
        filesystem=filesystem,
        parquet_uris=normalized_links,
        first_frame=first_frame,
        config=config,
        max_workers=read_workers,
    )
    if existing_context is not None:
        resolved_x_origin = _existing_grid_snap_origin(existing_context.x_values)
        resolved_y_origin = _existing_grid_snap_origin(existing_context.y_values)
    else:
        resolved_x_origin, resolved_y_origin = _resolve_snap_origins(
            coordinate_frames=coordinate_frames,
            parquet_uris=normalized_links,
            config=config,
        )
    config = replace(
        config,
        x_snap_origin=resolved_x_origin,
        y_snap_origin=resolved_y_origin,
    )
    time_slices = _build_input_time_slices(
        parquet_uris=normalized_links,
        coordinate_frames=coordinate_frames,
        config=config,
    )
    timestamps = [item.timestamp for item in time_slices]
    existing_timestamp_set = (
        {item for item in existing_context.time_values.tolist()}
        if existing_context is not None
        else set()
    )
    duplicate_timestamps = [item for item in timestamps if item in existing_timestamp_set]
    if duplicate_timestamps:
        raise ValueError(
            "Refusing to append duplicate timestamp(s) already present in the output store: "
            + ", ".join(str(item) for item in sorted(set(duplicate_timestamps)))
        )
    time_indices = {
        (item.source_uri, item.timestamp): (len(existing_context.time_values) if existing_context is not None else 0) + index
        for index, item in enumerate(time_slices)
    }
    if expected_x is None or expected_y is None:
        expected_x, expected_y = _resolve_target_grid(
            parquet_uris=normalized_links,
            coordinate_frames=coordinate_frames,
            config=config,
        )
    elif existing_context is not None:
        _validate_append_frames_fit_existing_grid(
            coordinate_frames=coordinate_frames,
            parquet_uris=normalized_links,
            config=config,
            existing_context=existing_context,
            output_store_uri=output_store_uri,
        )
    use_sparse_writer = (
        expected_x is not None
        and expected_y is not None
        and (len(expected_x) * len(expected_y) > config.max_grid_cells)
    )
    if use_sparse_writer:
        logger.info(
            "Using sparse chunked writer for %s because the shared grid has %d cell(s)",
            output_store_uri,
            len(expected_x) * len(expected_y),
        )
        if append and existing_context is not None:
            _resize_sparse_store_time_axis(
                filesystem=filesystem,
                output_store_uri=output_store_uri,
                config=config,
                existing_context=existing_context,
                final_time_values=[*existing_context.time_values.tolist(), *timestamps],
            )

    first_write_done = append
    total_slices = len(time_slices)

    for batch_start in range(0, total_slices, write_batch):
        batch_slices = time_slices[batch_start : batch_start + write_batch]
        logger.info(
            "Preparing batch %d-%d of %d time slice(s) with up to %d read worker(s)",
            batch_start + 1,
            batch_start + len(batch_slices),
            total_slices,
            read_workers,
        )
        batch_frames: dict[str, pd.DataFrame] = {}
        uris_to_read: list[str] = []

        for parquet_uri in dict.fromkeys(item.source_uri for item in batch_slices):
            if parquet_uri == first_uri and batch_start == 0:
                batch_frames[parquet_uri] = first_frame
                continue
            uris_to_read.append(parquet_uri)

        if uris_to_read:
            batch_frames.update(
                _read_all_parallel(
                    parquet_uris=uris_to_read,
                    columns=columns_to_read,
                    filesystem=filesystem,
                    max_workers=read_workers,
                )
            )

        batch_datasets: list[xr.Dataset] = []
        for offset, time_slice in enumerate(batch_slices, start=batch_start + 1):
            logger.info(
                "Processing time slice %d of %d: %s @ %s",
                offset,
                total_slices,
                time_slice.source_uri,
                time_slice.timestamp,
            )
            frame = _slice_frame_for_timestamp(
                batch_frames[time_slice.source_uri],
                config=config,
                timestamp=time_slice.timestamp,
            )
            if use_sparse_writer:
                continue
            dataset = _grid_dataset(
                frame,
                parquet_uri=time_slice.source_uri,
                config=config,
                expected_x=expected_x,
                expected_y=expected_y,
            )
            expected_x = dataset[config.x_dim].values
            expected_y = dataset[config.y_dim].values
            batch_datasets.append(dataset)
            summaries.append(
                _build_ingest_summary(
                    parquet_uri=time_slice.source_uri,
                    timestamp=dataset["time"].values[0],
                    config=config,
                    value_columns=config.value_columns or (),
                    x_values=np.asarray(dataset[config.x_dim].values, dtype=np.float64),
                    y_values=np.asarray(dataset[config.y_dim].values, dtype=np.float64),
                    input_rows=int(dataset.attrs.get("source_input_rows", len(frame.index))),
                    output_rows=int(dataset.attrs.get("source_output_rows", len(frame.index))),
                )
            )
            logger.info(
                "Completed time slice %d of %d: %s @ %s",
                offset,
                total_slices,
                time_slice.source_uri,
                time_slice.timestamp,
            )

        if use_sparse_writer:
            summaries.extend(
                _write_sparse_inputs(
                    filesystem=filesystem,
                    batch_frames=batch_frames,
                    batch_slices=batch_slices,
                    output_store_uri=output_store_uri,
                    config=config,
                    x_values=expected_x,
                    y_values=expected_y,
                    timestamps=timestamps,
                    time_indices=time_indices,
                    chunk_size=chunk_size,
                    shard_size=shard_size,
                    zarr_version=zarr_version,
                    is_first_write=not first_write_done,
                )
            )
        else:
            _flush_batch(
                datasets=batch_datasets,
                filesystem=filesystem,
                    output_store_uri=output_store_uri,
                    is_first_write=not first_write_done,
                    chunk_size=chunk_size,
                    shard_size=shard_size,
                    zarr_version=zarr_version,
                )
        first_write_done = True

    _consolidate_output_store(
        filesystem=filesystem,
        output_store_uri=output_store_uri,
        zarr_version=zarr_version,
    )
    return summaries


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    args = _parse_args()
    _configure_logging(args.log_level)
    settings = get_settings()
    logger.info(
        "Starting parquet-to-zarr conversion with output=%s, layout=%s, chunk_size=%d, shard_size=%d, read_workers=%d, write_batch=%d, zarr_version=%d, max_grid_cells=%d, x_resolution=%s, y_resolution=%s, source_crs=%s, output_crs=%s, cell_aggregation=%s, string_cell_aggregation=%s, overwrite=%s, append=%s",
        args.output_store,
        args.layout,
        args.chunk_size,
        args.shard_size,
        args.read_workers,
        args.write_batch,
        args.zarr_version,
        args.max_grid_cells,
        args.x_resolution,
        args.y_resolution,
        args.source_crs,
        args.crs,
        args.cell_aggregation,
        args.string_cell_aggregation,
        args.overwrite,
        args.append,
    )
    connector = OCIObjectStorageConnector(settings)
    config = ConversionConfig(
        x_column=args.x_column,
        y_column=args.y_column,
        value_columns=tuple(item.strip() for item in args.value_columns.split(",")) if args.value_columns else None,
        layout=args.layout,
        timestamp_column=args.timestamp_column,
        timestamp_regex=args.timestamp_regex,
        x_dim=args.x_dim,
        y_dim=args.y_dim,
        y_descending=args.y_order == "descending",
        dtype=args.dtype,
        crs=args.crs,
        max_grid_cells=args.max_grid_cells,
        x_resolution=args.x_resolution,
        y_resolution=args.y_resolution,
        cell_aggregation=args.cell_aggregation,
        string_cell_aggregation=args.string_cell_aggregation,
        shard_size=args.shard_size,
        source_crs=args.source_crs,
        x_snap_origin=args.x_snap_origin,
        y_snap_origin=args.y_snap_origin,
    )

    parquet_links = _load_links(args.links_file)
    output_store_uri = _normalize_oci_uri(args.output_store, connector)
    summaries = _append_inputs(
        connector=connector,
        parquet_links=parquet_links,
        output_store_uri=output_store_uri,
        config=config,
        overwrite=args.overwrite,
        append=args.append,
        chunk_size=args.chunk_size,
        shard_size=args.shard_size,
        read_workers=args.read_workers,
        write_batch=args.write_batch,
        zarr_version=args.zarr_version,
    )
    logger.info("Conversion finished successfully for %d parquet link(s)", len(summaries))
    print(
        json.dumps(
            {
                "output_store": output_store_uri,
                "ingested": summaries,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
