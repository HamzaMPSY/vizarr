import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Geod, Transformer

from app.config import Settings
from app.core.datasets import _stats
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.core.zarr_v3 import load_1d_numeric_array
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import load_fixed_length_utf32_labels
from app.core.zarr_v3 import parse_array_metadata
from app.core.zarr_v3 import read_consolidated_metadata
from app.core.zarr_v3 import read_store_metadata
from app.models.dataset import DatasetBounds, DatasetMeta, VariableMeta


LANDSAT_BAND_NAMES = {
    "1": "Coastal Aerosol",
    "2": "Blue",
    "3": "Green",
    "4": "Red",
    "5": "Near Infrared",
    "6": "SWIR 1",
    "7": "SWIR 2",
}


logger = logging.getLogger(__name__)
_MAX_PARALLEL_BAND_SAMPLES = 4


@dataclass
class CatalogEntry:
    id: str
    path: str
    meta: DatasetMeta
    zarr_format: int
    consolidated: bool
    data_array_name: str
    band_array_name: str
    band_names: list[str]
    band_indices: dict[str, int]
    data_array_meta: ZarrV3ArrayMetadata | None = None
    x_meta: ZarrV3ArrayMetadata | None = None
    y_meta: ZarrV3ArrayMetadata | None = None
    crs_wkt: str | None = None
    geo_transform: tuple[float, float, float, float, float, float] | None = None
    x_values: np.ndarray | None = None
    y_values: np.ndarray | None = None


def _encode_dataset_id(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _build_label(raw_label: str) -> tuple[str, str]:
    normalized = raw_label.strip()
    suffix = normalized[1:] if normalized.upper().startswith("B") else normalized
    friendly = LANDSAT_BAND_NAMES.get(suffix)
    if friendly:
        return normalized, f"{normalized} {friendly}"
    return normalized, normalized


def _default_band_names(count: int) -> list[str]:
    return [f"B{index}" for index in range(1, count + 1)]


def _select_projected_array_names(metadata: dict[str, dict]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for array_name, node in metadata.items():
        shape = node.get("shape")
        dimension_names = tuple(node.get("dimension_names", []))
        if not isinstance(shape, list) or len(shape) != 4:
            continue
        if "x" not in dimension_names or "y" not in dimension_names or "time" not in dimension_names:
            continue

        non_spatial_dims = [name for name in dimension_names if name not in {"time", "x", "y"}]
        if len(non_spatial_dims) != 1:
            continue
        candidates.append((array_name, non_spatial_dims[0]))

    if not candidates:
        raise ValueError("Dataset does not expose a supported projected 4D array with dims time/*/y/x")

    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def _build_variable_meta(
    band_names: list[str],
    stats_samples: list[np.ndarray] | None = None,
    time_steps: int = 1,
) -> list[VariableMeta]:
    variables: list[VariableMeta] = []
    for band_index, band_name in enumerate(band_names):
        band_id, band_title = _build_label(band_name)
        sample = (
            stats_samples[band_index]
            if stats_samples is not None and band_index < len(stats_samples)
            else np.asarray([0.0, 1.0], dtype=np.float32)
        )
        variables.append(
            VariableMeta(
                id=band_id,
                name=band_title,
                unit="DN",
                time_steps=time_steps,
                stats=_stats(sample),
            )
        )
    return variables


def _sample_band_stats(
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
    band_index: int,
) -> np.ndarray:
    if entry.data_array_meta is None:
        return np.asarray([0.0, 1.0], dtype=np.float32)

    _, _, height, width = entry.data_array_meta.shape
    _, _, chunk_height, chunk_width = entry.data_array_meta.chunk_shape
    y_start = max((height // 2) - (chunk_height // 2), 0)
    x_start = max((width // 2) - (chunk_width // 2), 0)
    y_stop = min(y_start + chunk_height, height)
    x_stop = min(x_start + chunk_width, width)

    sample = load_4d_window(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=entry.data_array_meta,
        time_index=0,
        band_index=band_index,
        y_start=y_start,
        y_stop=y_stop,
        x_start=x_start,
        x_stop=x_stop,
    ).astype(np.float32)

    finite = sample[np.isfinite(sample)]
    if entry.data_array_meta.fill_value is not None:
        fill_value = np.float32(entry.data_array_meta.fill_value)
        finite = finite[finite != fill_value]
    if finite.size == 0:
        return np.asarray([0.0, 1.0], dtype=np.float32)
    return finite


def _compute_bounds(
    x_values: np.ndarray,
    y_values: np.ndarray,
    crs_wkt: str | None,
    geo_transform: tuple[float, float, float, float, float, float] | None,
) -> DatasetBounds | None:
    if x_values.size == 0 or y_values.size == 0:
        return None

    source_crs = CRS.from_wkt(crs_wkt) if crs_wkt else CRS.from_epsg(4326)
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)

    if geo_transform is not None:
        origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = geo_transform
        width = len(x_values)
        height = len(y_values)
        xs = [
            origin_x,
            origin_x + width * pixel_width,
            origin_x + width * pixel_width + height * rot_x,
            origin_x + height * rot_x,
        ]
        ys = [
            origin_y,
            origin_y + width * rot_y,
            origin_y + width * rot_y + height * pixel_height,
            origin_y + height * pixel_height,
        ]
    else:
        x_step = float(x_values[1] - x_values[0]) if len(x_values) > 1 else 0.0
        y_step = float(y_values[1] - y_values[0]) if len(y_values) > 1 else 0.0
        xs = [
            float(x_values[0] - x_step / 2.0),
            float(x_values[-1] + x_step / 2.0),
            float(x_values[-1] + x_step / 2.0),
            float(x_values[0] - x_step / 2.0),
        ]
        ys = [
            float(y_values[0] - y_step / 2.0),
            float(y_values[0] - y_step / 2.0),
            float(y_values[-1] + y_step / 2.0),
            float(y_values[-1] + y_step / 2.0),
        ]

    lon_values, lat_values = transformer.transform(
        xs,
        ys,
    )

    west = max(min(lon_values), -180.0)
    east = min(max(lon_values), 180.0)
    south = max(min(lat_values), -85.0511)
    north = min(max(lat_values), 85.0511)
    return DatasetBounds(
        west=west,
        south=south,
        east=east,
        north=north,
    )


def _parse_geo_transform(value: str | None) -> tuple[float, float, float, float, float, float] | None:
    if not value:
        return None
    parts = value.split()
    if len(parts) != 6:
        return None
    return tuple(float(part) for part in parts)  # type: ignore[return-value]


def _resolve_direct_store_path(settings: Settings) -> str | None:
    oci_zarr_path = getattr(settings, "oci_zarr_path", "")
    if oci_zarr_path:
        return oci_zarr_path.removeprefix("oci://").split("/", 1)[-1]

    prefix = getattr(settings, "oci_prefix", "").rstrip("/")
    if prefix.endswith(".zarr"):
        return prefix
    return None


def has_direct_store_target(settings: Settings) -> bool:
    return _resolve_direct_store_path(settings) is not None


def _read_dataset_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict, dict]:
    store_metadata, metadata = read_consolidated_metadata(
        connector=connector,
        store_path=store_path,
    )
    if metadata:
        return store_metadata, metadata
    return read_store_metadata(
        connector=connector,
        store_path=store_path,
    )


def build_catalog_index(settings: Settings, connector: OCIObjectStorageConnector) -> dict[str, CatalogEntry]:
    direct_store_path = _resolve_direct_store_path(settings)
    stores = [] if direct_store_path is not None else connector.list_zarr_stores(prefix=settings.oci_prefix, limit=10000)
    catalog: dict[str, CatalogEntry] = {}

    store_paths: list[tuple[str, int | None, bool | None]]
    if direct_store_path is not None:
        store_paths = [(direct_store_path, None, None)]
    else:
        store_paths = [(store.path, store.zarr_format, store.consolidated) for store in stores]

    for store_path, store_zarr_format, store_consolidated in store_paths:
        if not store_path.endswith(".zarr"):
            continue

        try:
            store_metadata, metadata = _read_dataset_metadata(
                connector=connector,
                store_path=store_path,
            )
            zarr_format = int(store_metadata.get("zarr_format", store_zarr_format or 0))
            if zarr_format != 3:
                continue
            data_array_name, band_array_name = _select_projected_array_names(metadata)
        except Exception as exc:
            logger.warning("Skipping unsupported dataset store %s: %s", store_path, exc)
            continue

        dataset_id = _encode_dataset_id(store_path)
        dataset_name = store_path.split("/")[-1]
        dataset_description = f"OCI Zarr store at {store_path}"

        catalog[dataset_id] = CatalogEntry(
            id=dataset_id,
            path=store_path,
            meta=DatasetMeta(
                id=dataset_id,
                name=dataset_name,
                description=dataset_description,
                variables=[],
            ),
            zarr_format=zarr_format,
            consolidated=bool(
                store_consolidated if store_consolidated is not None else "consolidated_metadata" in store_metadata
            ),
            data_array_name=data_array_name,
            band_array_name=band_array_name,
            band_names=[],
            band_indices={},
        )

    return catalog


def build_dataset_manifest(catalog: dict[str, CatalogEntry]) -> list[DatasetMeta]:
    return [entry.meta.model_copy(deep=True) for entry in catalog.values()]


def warm_catalog_index(app, eager_entry_state: bool = False) -> dict[str, CatalogEntry]:
    settings = app.state.settings
    connector = app.state.storage_connector
    if connector is None:
        app.state.dataset_catalog = {}
        app.state.dataset_manifest = []
        return {}

    catalog = build_catalog_index(settings=settings, connector=connector)
    if eager_entry_state:
        for entry in catalog.values():
            try:
                ensure_catalog_entry_ready(entry, connector)
            except Exception as exc:
                logger.warning("Skipping eager catalog warm-up for %s: %s", entry.path, exc)
    app.state.dataset_catalog = catalog
    app.state.dataset_manifest = build_dataset_manifest(catalog)
    logger.info("Built OCI dataset manifest with %d dataset(s)", len(app.state.dataset_manifest))
    return catalog


def ensure_catalog_entry_metadata_ready(entry: CatalogEntry, connector: OCIObjectStorageConnector) -> CatalogEntry:
    if entry.data_array_meta is None or entry.x_meta is None or entry.y_meta is None or not entry.band_names:
        _, metadata = _read_dataset_metadata(
            connector=connector,
            store_path=entry.path,
        )
        data_array_name, band_array_name = _select_projected_array_names(metadata)
        if band_array_name not in metadata:
            raise ValueError(f"Dataset {entry.id} is missing band coordinate array '{band_array_name}'")
        if "x" not in metadata or "y" not in metadata:
            raise ValueError(f"Dataset {entry.id} is missing x/y coordinate arrays")

        entry.data_array_name = data_array_name
        entry.band_array_name = band_array_name
        entry.data_array_meta = parse_array_metadata(metadata[data_array_name])
        band_meta = parse_array_metadata(metadata[band_array_name])
        entry.x_meta = parse_array_metadata(metadata["x"])
        entry.y_meta = parse_array_metadata(metadata["y"])
        spatial_ref_attrs = metadata.get("spatial_ref", {}).get("attributes", {})
        entry.crs_wkt = spatial_ref_attrs.get("crs_wkt")
        entry.geo_transform = _parse_geo_transform(spatial_ref_attrs.get("GeoTransform"))

        encoded_band_labels = entry.data_array_meta.attributes.get("band_labels")
        if not isinstance(encoded_band_labels, list):
            encoded_band_labels = None

        try:
            entry.band_names = load_fixed_length_utf32_labels(
                connector=connector,
                store_path=entry.path,
                array_name=band_array_name,
                metadata=band_meta,
            )
        except (FileNotFoundError, ValueError):
            if encoded_band_labels:
                entry.band_names = [str(label) for label in encoded_band_labels]
            else:
                entry.band_names = _default_band_names(int(metadata[band_array_name]["shape"][0]))

        if not entry.band_names:
            entry.band_names = _default_band_names(int(metadata[band_array_name]["shape"][0]))

        time_steps = int(metadata[data_array_name]["shape"][0]) if metadata[data_array_name]["shape"] else 1
        entry.band_indices = {name: index for index, name in enumerate(entry.band_names)}
        if len(entry.band_names) <= 1:
            stats_samples = [
                _sample_band_stats(connector, entry, band_index)
                for band_index in range(len(entry.band_names))
            ]
        else:
            with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_BAND_SAMPLES, len(entry.band_names))) as executor:
                stats_samples = list(
                    executor.map(
                        lambda band_index: _sample_band_stats(connector, entry, band_index),
                        range(len(entry.band_names)),
                    )
                )
        entry.meta.variables = _build_variable_meta(
            entry.band_names,
            stats_samples=stats_samples,
            time_steps=time_steps,
        )

    return entry


def ensure_catalog_entry_bounds_ready(entry: CatalogEntry, connector: OCIObjectStorageConnector) -> CatalogEntry:
    ensure_catalog_entry_metadata_ready(entry, connector)

    if entry.x_values is None:
        if entry.x_meta is None:
            raise ValueError(f"Dataset {entry.id} is missing x metadata")
        entry.x_values = load_1d_numeric_array(
            connector=connector,
            store_path=entry.path,
            array_name="x",
            metadata=entry.x_meta,
        )

    if entry.y_values is None:
        if entry.y_meta is None:
            raise ValueError(f"Dataset {entry.id} is missing y metadata")
        entry.y_values = load_1d_numeric_array(
            connector=connector,
            store_path=entry.path,
            array_name="y",
            metadata=entry.y_meta,
        )

    if entry.data_array_meta is None:
        raise ValueError(f"Dataset {entry.id} is missing data array metadata")

    if entry.meta.bounds is None and entry.x_values is not None and entry.y_values is not None:
        entry.meta.bounds = _compute_bounds(
            x_values=entry.x_values,
            y_values=entry.y_values,
            crs_wkt=entry.crs_wkt,
            geo_transform=entry.geo_transform,
        )
    if entry.meta.native_resolution_m is None and entry.x_values is not None and entry.y_values is not None:
        entry.meta.native_resolution_m = _estimate_native_resolution_m(
            x_values=entry.x_values,
            y_values=entry.y_values,
            crs_wkt=entry.crs_wkt,
            geo_transform=entry.geo_transform,
            bounds=entry.meta.bounds,
        )

    return entry


def ensure_catalog_entry_ready(entry: CatalogEntry, connector: OCIObjectStorageConnector) -> CatalogEntry:
    return ensure_catalog_entry_bounds_ready(entry, connector)


def get_or_build_catalog(app) -> dict[str, CatalogEntry]:
    existing = getattr(app.state, "dataset_catalog", None)
    if existing:
        return existing

    return warm_catalog_index(app)


def _estimate_native_resolution_m(
    *,
    x_values: np.ndarray,
    y_values: np.ndarray,
    crs_wkt: str | None,
    geo_transform: tuple[float, float, float, float, float, float] | None,
    bounds: DatasetBounds | None,
) -> float | None:
    x_step = _axis_resolution(x_values, preferred=abs(geo_transform[1]) if geo_transform is not None else None)
    y_step = _axis_resolution(y_values, preferred=abs(geo_transform[5]) if geo_transform is not None else None)
    samples = [value for value in (x_step, y_step) if value is not None and value > 0]
    if not samples:
        return None

    if crs_wkt:
        crs = CRS.from_wkt(crs_wkt)
        if crs.is_projected and crs.axis_info:
            conversion = float(crs.axis_info[0].unit_conversion_factor or 1.0)
            return float(sum(samples) / len(samples) * conversion)

    center_lon = 0.0 if bounds is None else (bounds.west + bounds.east) / 2.0
    center_lat = 0.0 if bounds is None else (bounds.south + bounds.north) / 2.0
    geod = Geod(ellps="WGS84")
    metric_samples: list[float] = []
    if x_step is not None and x_step > 0:
        metric_samples.append(abs(geod.line_length([center_lon, center_lon + x_step], [center_lat, center_lat])))
    if y_step is not None and y_step > 0:
        metric_samples.append(abs(geod.line_length([center_lon, center_lon], [center_lat, center_lat + y_step])))
    metric_samples = [value for value in metric_samples if value > 0]
    if not metric_samples:
        return None
    return float(sum(metric_samples) / len(metric_samples))


def _axis_resolution(values: np.ndarray, preferred: float | None = None) -> float | None:
    if preferred is not None and preferred > 0:
        return float(preferred)
    if values.size < 2:
        return None
    diffs = np.abs(np.diff(values.astype(np.float64, copy=False)))
    finite = diffs[np.isfinite(diffs) & (diffs > 0)]
    if finite.size == 0:
        return None
    return float(np.median(finite))
