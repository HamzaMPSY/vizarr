from __future__ import annotations

import argparse
import logging
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Iterator
from typing import Sequence

import numpy as np
from pyproj import CRS
import zarr

from app.config import get_settings
from app.core.oci_object_storage import OCIObjectStorageConnector


logger = logging.getLogger(__name__)


DEFAULT_BANDS = ("B02", "B03", "B04", "B08")
DEFAULT_CHUNK_SIZE = 512
DEFAULT_SHARD_SIZE = 4096
DEFAULT_OUTPUT_BUCKET = "Ayoub"
SAFE_TIMESTAMP_RE = re.compile(r"_(?P<timestamp>\d{8}T\d{6})_")
SAFE_TILE_RE = re.compile(r"_(?P<tile>T\d{2}[A-Z]{3})_")
SENTINEL_BANDS = (
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B10",
    "B11",
    "B12",
)


@dataclass(frozen=True)
class SentinelBandObject:
    band: str
    object_name: str
    resolution_m: int | None
    size: int | None = None


@dataclass(frozen=True)
class SafeConversionConfig:
    source_bucket: str
    safe_prefix: str
    output_bucket: str
    output_store: str
    source_namespace: str | None
    output_namespace: str | None
    bands: tuple[str, ...]
    resolution_m: int
    dtype: str
    chunk_size: int
    shard_size: int
    zarr_version: int
    overwrite: bool
    dry_run: bool
    max_list_objects: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert one Sentinel Copernicus SAFE folder from OCI Object Storage into "
            "a Vizarr-compatible projected Zarr cube."
        )
    )
    parser.add_argument("--source-bucket", required=True, help="OCI bucket containing the SAFE folder.")
    parser.add_argument("--safe-prefix", required=True, help="Object prefix ending in .SAFE/ to convert.")
    parser.add_argument(
        "--source-namespace",
        help="OCI source namespace. Defaults to the namespace discovered from the configured OCI session.",
    )
    parser.add_argument(
        "--output-bucket",
        help=(
            "OCI destination bucket. Defaults to OCI_BUCKET when configured, otherwise Ayoub "
            "to match the current cube bucket."
        ),
    )
    parser.add_argument(
        "--output-namespace",
        help="OCI destination namespace. Defaults to the namespace discovered from the configured OCI session.",
    )
    parser.add_argument(
        "--output-store",
        help=(
            "Destination Zarr store path or full OCI URI. If omitted, writes to "
            "cubes/<SAFE-product-name>.zarr in the output bucket."
        ),
    )
    parser.add_argument(
        "--bands",
        default=",".join(DEFAULT_BANDS),
        help=f"Comma-separated Sentinel bands to ingest. Default: {','.join(DEFAULT_BANDS)}.",
    )
    parser.add_argument(
        "--resolution",
        type=_positive_int,
        default=10,
        help="Resolution in metres to select for L2A SAFE products. Default: 10.",
    )
    parser.add_argument(
        "--dtype",
        choices=("float32", "float64"),
        default="float32",
        help="Output Zarr numeric dtype. Default: float32.",
    )
    parser.add_argument(
        "--chunk-size",
        type=_positive_int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Y/X chunk size for the output Zarr arrays. Default: {DEFAULT_CHUNK_SIZE}.",
    )
    parser.add_argument(
        "--shard-size",
        type=_positive_int,
        default=DEFAULT_SHARD_SIZE,
        help=f"Y/X shard size for Zarr v3. Default: {DEFAULT_SHARD_SIZE}.",
    )
    parser.add_argument(
        "--zarr-version",
        type=int,
        choices=(2, 3),
        default=3,
        help="Output Zarr format version. Default: 3.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace the destination store if it exists.")
    parser.add_argument("--dry-run", action="store_true", help="List and validate inputs without writing Zarr.")
    parser.add_argument(
        "--max-list-objects",
        type=_positive_int,
        default=5000,
        help="Maximum number of source objects to scan below the SAFE prefix. Default: 5000.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity. Default: INFO.",
    )
    return parser.parse_args()


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {raw!r}")
    return value


def _normalize_safe_prefix(prefix: str) -> str:
    cleaned = prefix.strip().lstrip("/")
    if not cleaned:
        raise ValueError("SAFE prefix must not be empty")
    return cleaned if cleaned.endswith("/") else f"{cleaned}/"


def _safe_name_from_prefix(prefix: str) -> str:
    normalized = _normalize_safe_prefix(prefix)
    name = normalized.rstrip("/").rsplit("/", 1)[-1]
    if not name.endswith(".SAFE"):
        raise ValueError(f"SAFE prefix must point at a .SAFE folder, got {prefix!r}")
    return name


def _derive_output_store(safe_prefix: str) -> str:
    safe_name = _safe_name_from_prefix(safe_prefix)
    return f"cubes/{safe_name.removesuffix('.SAFE')}.zarr"


def _parse_safe_timestamp(safe_prefix: str) -> np.datetime64:
    safe_name = _safe_name_from_prefix(safe_prefix)
    match = SAFE_TIMESTAMP_RE.search(safe_name)
    if match is None:
        raise ValueError(f"Could not parse sensing timestamp from SAFE name {safe_name!r}")
    parsed = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%S")
    return np.datetime64(parsed, "ns")


def _parse_safe_tile_id(safe_prefix: str) -> str | None:
    safe_name = _safe_name_from_prefix(safe_prefix)
    match = SAFE_TILE_RE.search(safe_name)
    return match.group("tile") if match else None


def _parse_bands(raw_bands: str) -> tuple[str, ...]:
    bands = tuple(item.strip().upper() for item in raw_bands.split(",") if item.strip())
    if not bands:
        raise ValueError("At least one band must be requested")
    unsupported = [band for band in bands if band not in SENTINEL_BANDS]
    if unsupported:
        raise ValueError(f"Unsupported Sentinel band(s): {', '.join(unsupported)}")
    duplicates = sorted({band for band in bands if bands.count(band) > 1})
    if duplicates:
        raise ValueError(f"Duplicate band(s) requested: {', '.join(duplicates)}")
    return bands


def _build_oci_uri(bucket: str, namespace: str, object_path: str) -> str:
    clean_bucket = bucket.strip()
    clean_namespace = namespace.strip()
    clean_path = object_path.strip().lstrip("/")
    if not clean_bucket:
        raise ValueError("OCI bucket must not be empty")
    if not clean_namespace:
        raise ValueError("OCI namespace must not be empty")
    if not clean_path:
        raise ValueError("OCI object path must not be empty")
    return f"oci://{clean_bucket}@{clean_namespace}/{clean_path}"


def _mapper_path_from_oci_uri(oci_uri: str) -> str:
    return oci_uri.removeprefix("oci://")


def _normalize_output_store_uri(
    output_store: str,
    *,
    output_bucket: str,
    output_namespace: str,
) -> str:
    if output_store.startswith("oci://"):
        return output_store
    return _build_oci_uri(output_bucket, output_namespace, output_store)


def _resolution_from_path(object_name: str) -> int | None:
    upper = object_name.upper()
    match = re.search(r"(?:/R|_)(?P<resolution>10|20|60)M(?:/|_|\.)", upper)
    if match is None:
        return None
    return int(match.group("resolution"))


def _band_from_path(object_name: str) -> str | None:
    base_name = os.path.basename(object_name).upper()
    for band in sorted(SENTINEL_BANDS, key=len, reverse=True):
        if re.search(rf"(?:^|_){re.escape(band)}(?:_|\.|$)", base_name):
            return band
    return None


def _object_rank(object_name: str, target_resolution_m: int) -> tuple[int, int, int, str]:
    upper = object_name.upper()
    resolution = _resolution_from_path(upper)
    resolution_rank = 0 if resolution == target_resolution_m else 1 if resolution is None else 2
    image_data_rank = 0 if "/IMG_DATA/" in upper else 1
    suffix_rank = 0 if f"_{target_resolution_m}M.JP2" in upper else 1
    return resolution_rank, image_data_rank, suffix_rank, object_name


def _discover_safe_band_objects(
    object_names: Sequence[str],
    *,
    bands: Sequence[str],
    resolution_m: int,
) -> tuple[SentinelBandObject, ...]:
    candidates: dict[str, list[str]] = {band: [] for band in bands}
    for object_name in object_names:
        if not object_name.lower().endswith((".jp2", ".j2k")):
            continue
        if "/IMG_DATA/" not in object_name.upper():
            continue
        band = _band_from_path(object_name)
        if band in candidates:
            candidates[band].append(object_name)

    missing = [band for band, paths in candidates.items() if not paths]
    if missing:
        raise ValueError(f"SAFE prefix is missing requested band object(s): {', '.join(missing)}")

    selected: list[SentinelBandObject] = []
    for band in bands:
        ranked = sorted(candidates[band], key=lambda path: _object_rank(path, resolution_m))
        object_name = ranked[0]
        resolution = _resolution_from_path(object_name)
        if resolution is not None and resolution != resolution_m:
            raise ValueError(
                f"Band {band} was found only at {resolution} m, but --resolution={resolution_m} was requested"
            )
        selected.append(
            SentinelBandObject(
                band=band,
                object_name=object_name,
                resolution_m=resolution,
            )
        )
    return tuple(selected)


def _list_source_objects(
    connector: OCIObjectStorageConnector,
    *,
    bucket: str,
    namespace: str,
    prefix: str,
    max_objects: int,
) -> list[str]:
    results: list[str] = []
    next_start_with = None
    while len(results) < max_objects:
        response = connector._run_with_auth_retry(
            lambda: connector.client.list_objects(
                namespace_name=namespace,
                bucket_name=bucket,
                prefix=prefix,
                start=next_start_with,
                limit=min(1000, max_objects - len(results)),
            ),
            operation_name=f"list_objects({bucket}/{prefix})",
        )
        results.extend(item.name for item in response.data.objects)
        next_start_with = response.data.next_start_with
        if not next_start_with:
            break
    if len(results) >= max_objects and next_start_with:
        raise ValueError(
            f"SAFE object listing reached --max-list-objects={max_objects}; increase the limit to scan the full folder"
        )
    return results


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


def _is_auth_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, PermissionError):
            return True
        status = getattr(current, "status", None)
        code = getattr(current, "code", None)
        if status == 401 or code == "NotAuthenticated":
            return True
        current = current.__cause__ or current.__context__
    return False


def _require_north_up_transform(transform: Any) -> None:
    if not math.isclose(float(transform.b), 0.0) or not math.isclose(float(transform.d), 0.0):
        raise ValueError("Rotated Sentinel rasters are not supported by this converter yet")
    if not float(transform.a) > 0:
        raise ValueError("Expected positive pixel width in source raster transform")
    if not float(transform.e) < 0:
        raise ValueError("Expected negative pixel height in source raster transform")


def _coords_from_transform(transform: Any, *, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    _require_north_up_transform(transform)
    x = float(transform.c) + (np.arange(width, dtype=np.float64) + 0.5) * float(transform.a)
    y = float(transform.f) + (np.arange(height, dtype=np.float64) + 0.5) * float(transform.e)
    return x, y


def _geotransform_from_transform(transform: Any) -> str:
    return (
        f"{float(transform.c)} {float(transform.a)} {float(transform.b)} "
        f"{float(transform.f)} {float(transform.d)} {float(transform.e)}"
    )


def _spatial_ref_attrs(crs_value: Any, transform: Any) -> dict[str, Any]:
    crs = CRS.from_user_input(crs_value)
    return {
        "crs_wkt": crs.to_wkt(),
        "GeoTransform": _geotransform_from_transform(transform),
    }


def _array_attrs(dimensions: tuple[str, ...], attrs: dict[str, Any] | None, *, zarr_version: int) -> dict[str, Any]:
    result = dict(attrs or {})
    if zarr_version == 2:
        result["_ARRAY_DIMENSIONS"] = list(dimensions)
    return result


def _create_output_store(
    filesystem: Any,
    output_store_uri: str,
    *,
    bands: Sequence[str],
    width: int,
    height: int,
    x_values: np.ndarray,
    y_values: np.ndarray,
    timestamp: np.datetime64,
    crs: Any,
    transform: Any,
    dtype: str,
    chunk_size: int,
    shard_size: int,
    zarr_version: int,
    metadata: dict[str, Any],
):
    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    root = zarr.open_group(store=mapper, mode="w", zarr_format=zarr_version)
    dtype_value = np.dtype(dtype)

    data_kwargs: dict[str, Any] = {
        "name": "bands",
        "shape": (1, len(bands), height, width),
        "chunks": (1, 1, min(chunk_size, height), min(chunk_size, width)),
        "dtype": dtype_value,
        "fill_value": np.nan,
        "attributes": _array_attrs(
            ("time", "band", "y", "x"),
            {
                "band_labels": list(bands),
                "grid_mapping": "spatial_ref",
                "units": "DN",
            },
            zarr_version=zarr_version,
        ),
    }
    if zarr_version == 3:
        data_kwargs["dimension_names"] = ("time", "band", "y", "x")
        data_kwargs["shards"] = (1, 1, shard_size, shard_size)
    root.create_array(**data_kwargs)

    _create_coord_array(
        root,
        "x",
        x_values.astype(np.float64),
        ("x",),
        {"axis": "X", "standard_name": "projection_x_coordinate"},
        zarr_version=zarr_version,
    )
    _create_coord_array(
        root,
        "y",
        y_values.astype(np.float64),
        ("y",),
        {"axis": "Y", "standard_name": "projection_y_coordinate"},
        zarr_version=zarr_version,
    )
    _create_coord_array(
        root,
        "band",
        np.arange(len(bands), dtype=np.int32),
        ("band",),
        {"long_name": "band", "band_labels": list(bands)},
        zarr_version=zarr_version,
    )
    _create_coord_array(
        root,
        "time",
        np.asarray([timestamp.astype("datetime64[ns]").astype(np.int64)], dtype=np.int64),
        ("time",),
        {
            "units": "nanoseconds since 1970-01-01T00:00:00",
            "calendar": "proleptic_gregorian",
        },
        zarr_version=zarr_version,
    )

    spatial_ref_kwargs: dict[str, Any] = {
        "name": "spatial_ref",
        "data": np.asarray(0, dtype=np.int32),
        "attributes": _spatial_ref_attrs(crs, transform),
    }
    if zarr_version == 3:
        spatial_ref_kwargs["dimension_names"] = ()
    else:
        spatial_ref_kwargs["attributes"] = _array_attrs((), spatial_ref_kwargs["attributes"], zarr_version=zarr_version)
    root.create_array(**spatial_ref_kwargs)

    root.attrs.update(
        {
            "source_format": "Sentinel SAFE",
            "vizarr_layout": "sentinel-safe-bands",
            "vizarr_inner_chunk_size": int(chunk_size),
            "vizarr_shard_size": int(shard_size) if zarr_version == 3 else None,
            **metadata,
        }
    )
    return root


def _create_coord_array(
    root,
    name: str,
    values: np.ndarray,
    dimensions: tuple[str, ...],
    attrs: dict[str, Any],
    *,
    zarr_version: int,
):
    kwargs: dict[str, Any] = {
        "name": name,
        "data": values,
        "chunks": (min(max(values.shape[0], 1), 1024),),
        "attributes": _array_attrs(dimensions, attrs, zarr_version=zarr_version),
    }
    if zarr_version == 3:
        kwargs["dimension_names"] = dimensions
    return root.create_array(**kwargs)


@contextmanager
def _temporary_object_file(filesystem: Any, mapper_path: str, *, suffix: str = ".jp2") -> Iterator[str]:
    fd, tmp_path = tempfile.mkstemp(prefix="vizarr-safe-", suffix=suffix)
    os.close(fd)
    try:
        logger.info("Downloading source object to temporary file: %s", mapper_path)
        with filesystem.open(mapper_path, "rb") as source, open(tmp_path, "wb") as destination:
            shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _read_raster_profile(filesystem: Any, mapper_path: str) -> dict[str, Any]:
    try:
        import rasterio
    except ImportError as error:
        raise RuntimeError(
            "rasterio is required for Sentinel SAFE conversion. Rebuild the backend image after installing requirements."
        ) from error

    with _temporary_object_file(filesystem, mapper_path) as tmp_path:
        with rasterio.open(tmp_path) as dataset:
            if dataset.count < 1:
                raise ValueError(f"Raster object has no readable bands: {mapper_path}")
            if dataset.crs is None:
                raise ValueError(f"Raster object is missing CRS metadata: {mapper_path}")
            _require_north_up_transform(dataset.transform)
            return {
                "width": int(dataset.width),
                "height": int(dataset.height),
                "crs": dataset.crs,
                "transform": dataset.transform,
                "dtype": dataset.dtypes[0],
                "nodata": dataset.nodata,
            }


def _open_target_array(
    connector: OCIObjectStorageConnector,
    output_store_uri: str,
    *,
    zarr_version: int,
):
    filesystem = connector.get_filesystem()
    mapper = filesystem.get_mapper(_mapper_path_from_oci_uri(output_store_uri))
    root = zarr.open_group(store=mapper, mode="a", zarr_format=zarr_version)
    return root["bands"]


def _write_target_window_with_auth_retry(
    *,
    connector: OCIObjectStorageConnector,
    output_store_uri: str,
    target_array: Any,
    zarr_version: int,
    band_index: int,
    row_start: int,
    row_stop: int,
    col_start: int,
    col_stop: int,
    block: np.ndarray,
):
    max_attempts = 3
    current_target = target_array
    for attempt in range(1, max_attempts + 1):
        try:
            current_target[0, band_index, row_start:row_stop, col_start:col_stop] = block
            return current_target
        except Exception as error:
            if not _is_auth_error(error) or attempt >= max_attempts:
                raise
            logger.warning(
                "OCI auth failed while writing band index %d window rows=%d:%d cols=%d:%d; "
                "refreshing session and retrying (%d/%d)",
                band_index,
                row_start,
                row_stop,
                col_start,
                col_stop,
                attempt,
                max_attempts - 1,
            )
            connector.refresh()
            current_target = _open_target_array(
                connector,
                output_store_uri,
                zarr_version=zarr_version,
            )
    return current_target


def _write_band_windows(
    *,
    connector: OCIObjectStorageConnector,
    filesystem: Any,
    source_mapper_path: str,
    output_store_uri: str,
    target_array: Any,
    zarr_version: int,
    band_index: int,
    expected_width: int,
    expected_height: int,
    expected_crs: Any,
    expected_transform: Any,
    dtype: str,
) -> int:
    try:
        import rasterio
    except ImportError as error:
        raise RuntimeError(
            "rasterio is required for Sentinel SAFE conversion. Rebuild the backend image after installing requirements."
        ) from error

    pixels_written = 0
    current_target = target_array
    with _temporary_object_file(filesystem, source_mapper_path) as tmp_path:
        with rasterio.open(tmp_path) as dataset:
            if dataset.width != expected_width or dataset.height != expected_height:
                raise ValueError(
                    f"Band shape mismatch for {source_mapper_path}: "
                    f"expected {expected_width}x{expected_height}, got {dataset.width}x{dataset.height}. "
                    "Select bands at one resolution; resampling is not implemented in this converter yet."
                )
            if dataset.crs != expected_crs:
                raise ValueError(f"Band CRS mismatch for {source_mapper_path}: expected {expected_crs}, got {dataset.crs}")
            if dataset.transform != expected_transform:
                raise ValueError(
                    f"Band transform mismatch for {source_mapper_path}. "
                    "Select bands at one resolution; resampling is not implemented in this converter yet."
                )

            for _block_index, window in dataset.block_windows(1):
                data = dataset.read(1, window=window, masked=True, out_dtype=dtype)
                block = data.filled(np.nan).astype(dtype, copy=False)
                row_start = int(window.row_off)
                col_start = int(window.col_off)
                row_stop = row_start + int(window.height)
                col_stop = col_start + int(window.width)
                current_target = _write_target_window_with_auth_retry(
                    connector=connector,
                    output_store_uri=output_store_uri,
                    target_array=current_target,
                    zarr_version=zarr_version,
                    band_index=band_index,
                    row_start=row_start,
                    row_stop=row_stop,
                    col_start=col_start,
                    col_stop=col_stop,
                    block=block,
                )
                pixels_written += int(window.height) * int(window.width)
    return pixels_written


def _conversion_config_from_args(args: argparse.Namespace, connector: OCIObjectStorageConnector) -> SafeConversionConfig:
    settings = get_settings()
    safe_prefix = _normalize_safe_prefix(args.safe_prefix)
    output_bucket = args.output_bucket or settings.oci_bucket or DEFAULT_OUTPUT_BUCKET
    output_namespace = args.output_namespace or connector.namespace
    output_store = args.output_store or _derive_output_store(safe_prefix)
    return SafeConversionConfig(
        source_bucket=args.source_bucket,
        safe_prefix=safe_prefix,
        output_bucket=output_bucket,
        output_store=output_store,
        source_namespace=args.source_namespace or connector.namespace,
        output_namespace=output_namespace,
        bands=_parse_bands(args.bands),
        resolution_m=args.resolution,
        dtype=args.dtype,
        chunk_size=args.chunk_size,
        shard_size=args.shard_size,
        zarr_version=args.zarr_version,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        max_list_objects=args.max_list_objects,
    )


def convert_safe_to_zarr(
    connector: OCIObjectStorageConnector,
    config: SafeConversionConfig,
) -> dict[str, Any]:
    _validate_storage_layout(
        chunk_size=config.chunk_size,
        shard_size=config.shard_size,
        zarr_version=config.zarr_version,
    )
    if config.source_namespace is None or config.output_namespace is None:
        raise ValueError("Source and output OCI namespaces must be resolved before conversion")

    source_objects = _list_source_objects(
        connector,
        bucket=config.source_bucket,
        namespace=config.source_namespace,
        prefix=config.safe_prefix,
        max_objects=config.max_list_objects,
    )
    if not source_objects:
        raise FileNotFoundError(f"No objects found under {config.source_bucket}/{config.safe_prefix}")

    selected_bands = _discover_safe_band_objects(
        source_objects,
        bands=config.bands,
        resolution_m=config.resolution_m,
    )
    output_store_uri = _normalize_output_store_uri(
        config.output_store,
        output_bucket=config.output_bucket,
        output_namespace=config.output_namespace,
    )
    timestamp = _parse_safe_timestamp(config.safe_prefix)
    safe_name = _safe_name_from_prefix(config.safe_prefix)
    tile_id = _parse_safe_tile_id(config.safe_prefix)

    filesystem = connector.get_filesystem()
    profile_mapper_path = f"{config.source_bucket}@{config.source_namespace}/{selected_bands[0].object_name}"
    profile = _read_raster_profile(filesystem, profile_mapper_path)
    x_values, y_values = _coords_from_transform(
        profile["transform"],
        width=profile["width"],
        height=profile["height"],
    )

    summary: dict[str, Any] = {
        "source_bucket": config.source_bucket,
        "source_namespace": config.source_namespace,
        "safe_prefix": config.safe_prefix,
        "safe_name": safe_name,
        "tile_id": tile_id,
        "output_store": output_store_uri,
        "bands": [item.band for item in selected_bands],
        "band_objects": [item.object_name for item in selected_bands],
        "shape": [1, len(selected_bands), profile["height"], profile["width"]],
        "crs": str(profile["crs"]),
        "timestamp": str(timestamp),
        "dtype": config.dtype,
        "dry_run": config.dry_run,
    }
    if config.dry_run:
        logger.info("Dry run summary: %s", summary)
        return summary

    output_mapper_path = _mapper_path_from_oci_uri(output_store_uri)
    if filesystem.exists(output_mapper_path):
        if not config.overwrite:
            raise ValueError(f"Output store already exists: {output_store_uri}. Pass --overwrite to replace it.")
        logger.info("Removing existing output store before overwrite: %s", output_store_uri)
        filesystem.rm(output_mapper_path, recursive=True)

    metadata = {
        "source_bucket": config.source_bucket,
        "source_namespace": config.source_namespace,
        "source_prefix": config.safe_prefix,
        "safe_name": safe_name,
        "sentinel_tile_id": tile_id,
        "sentinel_resolution_m": int(config.resolution_m),
        "created_at_unix": int(time.time()),
    }
    root = _create_output_store(
        filesystem,
        output_store_uri,
        bands=[item.band for item in selected_bands],
        width=profile["width"],
        height=profile["height"],
        x_values=x_values,
        y_values=y_values,
        timestamp=timestamp,
        crs=profile["crs"],
        transform=profile["transform"],
        dtype=config.dtype,
        chunk_size=config.chunk_size,
        shard_size=config.shard_size,
        zarr_version=config.zarr_version,
        metadata=metadata,
    )
    target_array = root["bands"]
    for band_index, band_object in enumerate(selected_bands):
        source_mapper_path = f"{config.source_bucket}@{config.source_namespace}/{band_object.object_name}"
        logger.info("Writing band %s from %s", band_object.band, source_mapper_path)
        _write_band_windows(
            connector=connector,
            filesystem=filesystem,
            source_mapper_path=source_mapper_path,
            output_store_uri=output_store_uri,
            target_array=target_array,
            zarr_version=config.zarr_version,
            band_index=band_index,
            expected_width=profile["width"],
            expected_height=profile["height"],
            expected_crs=profile["crs"],
            expected_transform=profile["transform"],
            dtype=config.dtype,
        )

    logger.info("Consolidating output metadata for %s", output_store_uri)
    zarr.consolidate_metadata(filesystem.get_mapper(output_mapper_path), zarr_format=config.zarr_version)
    summary["dry_run"] = False
    return summary


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s: %(message)s")
    connector = OCIObjectStorageConnector(get_settings())
    config = _conversion_config_from_args(args, connector)
    summary = convert_safe_to_zarr(connector, config)
    logger.info("Conversion complete: %s", summary)
    print(summary)


if __name__ == "__main__":
    main()
