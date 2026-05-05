import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field

import numpy as np
from pyproj import CRS, Geod, Transformer

from app.config import Settings
from app.core.multiscale_store import multiscale_proxy_root
from app.core.multiscale_store import multiscale_store_path
from app.core.multiscale_store import probe_multiscale_store
from app.core.datasets import _stats
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.variable_display import apply_variable_display_defaults
from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.core.zarr_v3 import estimate_4d_nonempty_pixel_bounds
from app.core.zarr_v3 import load_1d_numeric_array
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import load_fixed_length_utf32_labels
from app.core.zarr_v3 import parse_array_metadata
from app.core.zarr_v3 import read_consolidated_metadata
from app.core.zarr_v3 import read_store_metadata
from app.models.dataset import CompositeStyle, DatasetBounds, DatasetMeta, VariableMeta


LANDSAT_BAND_NAMES = {
    "1": "Coastal Aerosol",
    "2": "Blue",
    "3": "Green",
    "4": "Red",
    "5": "Near Infrared",
    "6": "SWIR 1",
    "7": "SWIR 2",
}

COMPOSITE_STYLE_DEFINITIONS = (
    {
        "id": "true-color",
        "name": "True Color",
        "description": "Natural-color RGB composite using red, green, and blue bands.",
        "bands": ("red", "green", "blue"),
    },
    {
        "id": "false-color",
        "name": "False Color",
        "description": "Vegetation-focused composite using near infrared, red, and green bands.",
        "bands": ("nir", "red", "green"),
    },
)

_BAND_ALIASES = {
    "blue": {"B2", "B02", "2", "BLUE"},
    "green": {"B3", "B03", "3", "GREEN"},
    "red": {"B4", "B04", "4", "RED"},
    "nir": {"B5", "B05", "5", "NIR", "NIR1", "NEAR INFRARED", "NEAR_INFRARED"},
}


logger = logging.getLogger(__name__)
_MAX_PARALLEL_BAND_SAMPLES = 4


@dataclass
class ProjectedLayout:
    data_array_name: str
    band_array_name: str | None
    variable_array_names: dict[str, str]


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
    variable_array_names: dict[str, str] = field(default_factory=dict)
    data_array_metas: dict[str, ZarrV3ArrayMetadata] = field(default_factory=dict)
    data_array_meta: ZarrV3ArrayMetadata | None = None
    x_meta: ZarrV3ArrayMetadata | None = None
    y_meta: ZarrV3ArrayMetadata | None = None
    crs_wkt: str | None = None
    geo_transform: tuple[float, float, float, float, float, float] | None = None
    x_values: np.ndarray | None = None
    y_values: np.ndarray | None = None
    data_bounds_ready: bool = False


def _encode_dataset_id(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _build_label(raw_label: str) -> tuple[str, str]:
    normalized = raw_label.strip()
    suffix = normalized[1:] if normalized.upper().startswith("B") else normalized
    friendly = LANDSAT_BAND_NAMES.get(suffix)
    if friendly:
        return normalized, f"{normalized} {friendly}"
    return normalized, normalized


def _normalized_band_token(value: str) -> str:
    return value.strip().upper().replace("-", " ").replace("_", " ")


def _band_ids_by_role(band_names: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for band_name in band_names:
        band_id, band_title = _build_label(band_name)
        candidates = {
            _normalized_band_token(band_name),
            _normalized_band_token(band_id),
            _normalized_band_token(band_title),
        }
        candidates.update(item.replace(" ", "") for item in tuple(candidates))
        for role, aliases in _BAND_ALIASES.items():
            alias_tokens = {_normalized_band_token(alias) for alias in aliases}
            alias_tokens.update(item.replace(" ", "") for item in tuple(alias_tokens))
            if role not in resolved and candidates.intersection(alias_tokens):
                resolved[role] = band_id
    return resolved


def build_composite_styles(band_names: list[str]) -> list[CompositeStyle]:
    ids_by_role = _band_ids_by_role(band_names)
    styles: list[CompositeStyle] = []
    for definition in COMPOSITE_STYLE_DEFINITIONS:
        roles = tuple(definition["bands"])
        if all(role in ids_by_role for role in roles):
            styles.append(
                CompositeStyle(
                    id=str(definition["id"]),
                    name=str(definition["name"]),
                    description=str(definition["description"]),
                    bands=[ids_by_role[role] for role in roles],
                )
            )
    return styles


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


def _select_projected_layout(metadata: dict[str, dict]) -> ProjectedLayout:
    try:
        data_array_name, band_array_name = _select_projected_array_names(metadata)
        return ProjectedLayout(
            data_array_name=data_array_name,
            band_array_name=band_array_name,
            variable_array_names={},
        )
    except ValueError:
        pass

    variable_arrays: dict[str, str] = {}
    for array_name, node in sorted(metadata.items()):
        shape = node.get("shape")
        dimension_names = tuple(node.get("dimension_names", []))
        if not isinstance(shape, list):
            continue
        if len(shape) == 3 and dimension_names == ("time", "y", "x"):
            variable_arrays[array_name] = array_name
        elif len(shape) == 2 and dimension_names == ("y", "x"):
            variable_arrays[array_name] = array_name

    if variable_arrays:
        first_array_name = next(iter(variable_arrays.values()))
        return ProjectedLayout(
            data_array_name=first_array_name,
            band_array_name=None,
            variable_array_names=variable_arrays,
        )

    raise ValueError("Dataset does not expose a supported projected layout with dims time/*/y/x, time/y/x, or y/x")


def _build_variable_meta(
    band_names: list[str],
    stats_samples: list[np.ndarray] | None = None,
    time_steps: int = 1,
) -> list[VariableMeta]:
    variables: list[VariableMeta] = []
    for band_index, band_name in enumerate(band_names):
        band_id, band_title = _build_label(band_name)
        display_vmin, display_vmax, default_colormap = apply_variable_display_defaults(
            variable_id=band_id,
            variable_name=band_title,
        )
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
                display_vmin=display_vmin,
                display_vmax=display_vmax,
                default_colormap=default_colormap,
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


def _compute_bounds_from_grid_shape(
    *,
    width: int,
    height: int,
    crs_wkt: str | None,
    geo_transform: tuple[float, float, float, float, float, float] | None,
) -> DatasetBounds | None:
    if width <= 0 or height <= 0 or geo_transform is None:
        return None

    source_crs = CRS.from_wkt(crs_wkt) if crs_wkt else CRS.from_epsg(4326)
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    origin_x, pixel_width, rot_x, origin_y, rot_y, pixel_height = geo_transform
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
    lon_values, lat_values = transformer.transform(xs, ys)
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


def _compute_bounds_from_pixel_window(
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


def _refine_bounds_from_nonempty_data(
    *,
    entry: CatalogEntry,
    connector: OCIObjectStorageConnector,
) -> DatasetBounds | None:
    if not entry.meta.variables:
        return None

    variable_id = entry.meta.variables[0].id
    time_steps = max(int(entry.data_array_meta.shape[0]) if entry.data_array_meta is not None else 0, 1)
    source_bounds = _refine_bounds_from_nonempty_source_data(
        entry=entry,
        connector=connector,
        variable_id=variable_id,
        time_steps=time_steps,
    )
    if source_bounds is not None:
        return source_bounds

    from app.config import get_settings
    from app.core.browse_tiles import get_or_create_browse_overview

    settings = get_settings()
    target_zoom = min(getattr(settings, "browse_tile_max_zoom", 5), 5)
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    refined_west: float | None = None
    refined_south: float | None = None
    refined_east: float | None = None
    refined_north: float | None = None

    for time_index in range(time_steps):
        try:
            overview, overview_bbox, _source = get_or_create_browse_overview(
                settings=settings,
                connector=connector,
                entry=entry,
                variable=variable_id,
                time_index=time_index,
                zoom=target_zoom,
                allow_build=True,
            )
        except Exception:
            logger.exception("Failed to build overview-based bounds for %s time %s", entry.id, time_index)
            continue

        finite = np.isfinite(overview)
        if not np.any(finite):
            continue

        rows, cols = np.where(finite)
        height, width = overview.shape
        west, south, east, north = overview_bbox
        pixel_width = (east - west) / width
        pixel_height = (north - south) / height

        data_west = west + (int(cols.min()) * pixel_width)
        data_east = west + ((int(cols.max()) + 1) * pixel_width)
        data_north = north - (int(rows.min()) * pixel_height)
        data_south = north - ((int(rows.max()) + 1) * pixel_height)

        lon_values, lat_values = transformer.transform(
            [data_west, data_east, data_east, data_west],
            [data_south, data_south, data_north, data_north],
        )
        current_west = max(min(lon_values), -180.0)
        current_east = min(max(lon_values), 180.0)
        current_south = max(min(lat_values), -85.0511)
        current_north = min(max(lat_values), 85.0511)

        refined_west = current_west if refined_west is None else min(refined_west, current_west)
        refined_south = current_south if refined_south is None else min(refined_south, current_south)
        refined_east = current_east if refined_east is None else max(refined_east, current_east)
        refined_north = current_north if refined_north is None else max(refined_north, current_north)

    if None in {refined_west, refined_south, refined_east, refined_north}:
        return None
    return DatasetBounds(
        west=refined_west,
        south=refined_south,
        east=refined_east,
        north=refined_north,
    )


def _refine_bounds_from_nonempty_source_data(
    *,
    entry: CatalogEntry,
    connector: OCIObjectStorageConnector,
    variable_id: str,
    time_steps: int,
) -> DatasetBounds | None:
    if entry.data_array_meta is None or entry.geo_transform is None:
        return None
    if len(entry.data_array_meta.shape) != 4:
        return None

    band_index = entry.band_indices.get(variable_id)
    if band_index is None:
        return None

    pixel_window = estimate_4d_nonempty_pixel_bounds(
        connector=connector,
        store_path=entry.path,
        array_name=entry.data_array_name,
        metadata=entry.data_array_meta,
        time_indices=list(range(time_steps)),
        band_index=band_index,
    )
    if pixel_window is None:
        return None

    x_start, x_stop, y_start, y_stop = pixel_window
    return _compute_bounds_from_pixel_window(
        x_start=x_start,
        x_stop=x_stop,
        y_start=y_start,
        y_stop=y_stop,
        crs_wkt=entry.crs_wkt,
        geo_transform=entry.geo_transform,
    )


def _parse_geo_transform(value: str | None) -> tuple[float, float, float, float, float, float] | None:
    if not value:
        return None
    parts = value.split()
    if len(parts) != 6:
        return None
    return tuple(float(part) for part in parts)  # type: ignore[return-value]


def _crs_authority_from_wkt(crs_wkt: str | None) -> str | None:
    if not crs_wkt:
        return None
    try:
        crs = CRS.from_wkt(crs_wkt)
    except Exception:
        return None
    epsg = crs.to_epsg()
    if epsg is not None:
        return f"EPSG:{epsg}"
    authority = crs.to_authority()
    if authority is None:
        return None
    name, code = authority
    return f"{name}:{code}"


def _apply_crs_metadata(entry: CatalogEntry) -> None:
    entry.meta.crs_wkt = entry.crs_wkt
    entry.meta.crs_authority = _crs_authority_from_wkt(entry.crs_wkt)


def _estimate_native_resolution_from_geotransform(
    *,
    geo_transform: tuple[float, float, float, float, float, float] | None,
    crs_wkt: str | None,
    bounds: DatasetBounds | None,
) -> float | None:
    if geo_transform is None:
        return None
    _origin_x, pixel_width, _rot_x, _origin_y, _rot_y, pixel_height = geo_transform
    samples = [value for value in (abs(pixel_width), abs(pixel_height)) if value > 0]
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
    metric_samples = [
        abs(geod.line_length([center_lon, center_lon + sample], [center_lat, center_lat]))
        for sample in [abs(pixel_width)]
        if sample > 0
    ]
    if abs(pixel_height) > 0:
        metric_samples.append(abs(geod.line_length([center_lon, center_lon], [center_lat, center_lat + abs(pixel_height)])))
    metric_samples = [value for value in metric_samples if value > 0]
    if not metric_samples:
        return None
    return float(sum(metric_samples) / len(metric_samples))


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
            layout = _select_projected_layout(metadata)
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
                zarr_format=zarr_format,
                zarr_consolidated=bool(
                    store_consolidated if store_consolidated is not None else "consolidated_metadata" in store_metadata
                ),
                zarr_proxy_root=f"/api/zarr/{dataset_id}",
            ),
            zarr_format=zarr_format,
            consolidated=bool(
                store_consolidated if store_consolidated is not None else "consolidated_metadata" in store_metadata
            ),
            data_array_name=layout.data_array_name,
            band_array_name=layout.band_array_name or "",
            band_names=list(layout.variable_array_names.keys()),
            band_indices={name: index for index, name in enumerate(layout.variable_array_names.keys())},
            variable_array_names=layout.variable_array_names,
            data_array_metas={
                array_name: parse_array_metadata(metadata[array_name])
                for array_name in {layout.data_array_name, *layout.variable_array_names.values()}
            },
            data_array_meta=parse_array_metadata(metadata[layout.data_array_name]),
            x_meta=parse_array_metadata(metadata["x"]) if "x" in metadata else None,
            y_meta=parse_array_metadata(metadata["y"]) if "y" in metadata else None,
            crs_wkt=metadata.get("spatial_ref", {}).get("attributes", {}).get("crs_wkt"),
            geo_transform=_parse_geo_transform(metadata.get("spatial_ref", {}).get("attributes", {}).get("GeoTransform")),
        )
        entry = catalog[dataset_id]
        _apply_crs_metadata(entry)
        multiscale_summary = probe_multiscale_store(
            connector=connector,
            store_path=multiscale_store_path(settings, store_path),
        )
        if multiscale_summary is not None:
            entry.meta.multiscale_store_path = multiscale_summary.path
            entry.meta.multiscale_zarr_format = multiscale_summary.zarr_format
            entry.meta.multiscale_zarr_consolidated = multiscale_summary.consolidated
            entry.meta.multiscale_proxy_root = multiscale_proxy_root(dataset_id)
            entry.meta.multiscale_population_strategy = multiscale_summary.population_strategy
            entry.meta.multiscale_prepopulated_zoom_max = multiscale_summary.prepopulated_zoom_max
            entry.meta.multiscale_max_zoom = multiscale_summary.max_zoom
        if entry.data_array_meta is not None and len(entry.data_array_meta.shape) >= 3:
            bounds = _compute_bounds_from_grid_shape(
                width=int(entry.data_array_meta.shape[-1]),
                height=int(entry.data_array_meta.shape[-2]),
                crs_wkt=entry.crs_wkt,
                geo_transform=entry.geo_transform,
            )
            entry.meta.bounds = bounds
            entry.meta.native_resolution_m = _estimate_native_resolution_from_geotransform(
                geo_transform=entry.geo_transform,
                crs_wkt=entry.crs_wkt,
                bounds=bounds,
            )
            time_node = metadata.get("time")
            if isinstance(time_node, dict):
                time_meta = parse_array_metadata(time_node)
                time_values = load_1d_numeric_array(
                    connector=connector,
                    store_path=entry.path,
                    array_name="time",
                    metadata=time_meta,
                )
                entry.meta.time_values = _time_labels_from_values(time_values, time_node.get("attributes", {}))

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
    if (
        entry.data_array_meta is None
        or entry.x_meta is None
        or entry.y_meta is None
        or not entry.band_names
        or not entry.band_indices
        or not entry.meta.variables
    ):
        _, metadata = _read_dataset_metadata(
            connector=connector,
            store_path=entry.path,
        )
        layout = _select_projected_layout(metadata)
        if layout.band_array_name is not None and layout.band_array_name not in metadata:
            raise ValueError(f"Dataset {entry.id} is missing band coordinate array '{layout.band_array_name}'")
        if "x" not in metadata or "y" not in metadata:
            raise ValueError(f"Dataset {entry.id} is missing x/y coordinate arrays")

        entry.data_array_name = layout.data_array_name
        entry.band_array_name = layout.band_array_name or ""
        entry.variable_array_names = layout.variable_array_names
        entry.data_array_metas = {
            array_name: parse_array_metadata(metadata[array_name])
            for array_name in {layout.data_array_name, *layout.variable_array_names.values()}
        }
        entry.data_array_meta = entry.data_array_metas[layout.data_array_name]
        band_meta = parse_array_metadata(metadata[layout.band_array_name]) if layout.band_array_name is not None else None
        entry.x_meta = parse_array_metadata(metadata["x"])
        entry.y_meta = parse_array_metadata(metadata["y"])
        spatial_ref_attrs = metadata.get("spatial_ref", {}).get("attributes", {})
        entry.crs_wkt = spatial_ref_attrs.get("crs_wkt")
        entry.geo_transform = _parse_geo_transform(spatial_ref_attrs.get("GeoTransform"))
        _apply_crs_metadata(entry)

        encoded_band_labels = entry.data_array_meta.attributes.get("band_labels")
        if not isinstance(encoded_band_labels, list):
            encoded_band_labels = None

        if layout.band_array_name is None:
            entry.band_names = list(layout.variable_array_names.keys())
        else:
            assert band_meta is not None
            try:
                entry.band_names = load_fixed_length_utf32_labels(
                    connector=connector,
                    store_path=entry.path,
                    array_name=layout.band_array_name,
                    metadata=band_meta,
                )
            except (FileNotFoundError, ValueError):
                if encoded_band_labels:
                    entry.band_names = [str(label) for label in encoded_band_labels]
                else:
                    entry.band_names = _default_band_names(int(metadata[layout.band_array_name]["shape"][0]))

        if not entry.band_names:
            if layout.band_array_name is None:
                entry.band_names = list(layout.variable_array_names.keys())
            else:
                entry.band_names = _default_band_names(int(metadata[layout.band_array_name]["shape"][0]))

        data_shape = metadata[layout.data_array_name]["shape"]
        data_dimension_names = tuple(metadata[layout.data_array_name].get("dimension_names", []))
        time_steps = int(data_shape[0]) if data_shape and data_dimension_names[0:1] == ("time",) else 1
        entry.band_indices = {name: index for index, name in enumerate(entry.band_names)}
        if "time" in metadata:
            time_meta = parse_array_metadata(metadata["time"])
            time_values = load_1d_numeric_array(
                connector=connector,
                store_path=entry.path,
                array_name="time",
                metadata=time_meta,
            )
            entry.meta.time_values = _time_labels_from_values(time_values, metadata["time"].get("attributes", {}))
        entry.meta.variables = _build_variable_meta(
            entry.band_names,
            # Keep variable discovery cheap on remote OCI stores. Interactive rendering
            # uses explicit display defaults and tile-time ranges, so blocking the
            # sidebar on remote stats sampling is not worth the latency cost.
            stats_samples=None,
            time_steps=time_steps,
        )
        entry.meta.composite_styles = build_composite_styles(entry.band_names)

    return entry


def ensure_catalog_entry_bounds_ready(entry: CatalogEntry, connector: OCIObjectStorageConnector) -> CatalogEntry:
    ensure_catalog_entry_metadata_ready(entry, connector)

    if (
        entry.meta.bounds is None
        and entry.data_array_meta is not None
        and len(entry.data_array_meta.shape) >= 3
        and entry.geo_transform is not None
    ):
        entry.meta.bounds = _compute_bounds_from_grid_shape(
            width=int(entry.data_array_meta.shape[-1]),
            height=int(entry.data_array_meta.shape[-2]),
            crs_wkt=entry.crs_wkt,
            geo_transform=entry.geo_transform,
        )
    if not entry.data_bounds_ready:
        refined_bounds = _refine_bounds_from_nonempty_data(
            entry=entry,
            connector=connector,
        )
        if refined_bounds is not None:
            entry.meta.bounds = refined_bounds
        entry.data_bounds_ready = True
    if entry.meta.native_resolution_m is None and entry.geo_transform is not None:
        entry.meta.native_resolution_m = _estimate_native_resolution_from_geotransform(
            geo_transform=entry.geo_transform,
            crs_wkt=entry.crs_wkt,
            bounds=entry.meta.bounds,
        )

    if entry.meta.bounds is not None and entry.meta.native_resolution_m is not None:
        return entry

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


def _time_labels_from_values(values: np.ndarray, attributes: dict[str, object]) -> list[str]:
    if values.size == 0:
        return []

    units = str(attributes.get("units", ""))
    if values.dtype == np.dtype(np.int64) and units.startswith("nanoseconds since 1970-01-01T00:00:00"):
        return [str(value).split("T", 1)[0] for value in values.astype("datetime64[ns]")]
    if np.issubdtype(values.dtype, np.datetime64):
        return [str(value).split("T", 1)[0] for value in values.astype("datetime64[ns]")]
    return [str(value.item() if hasattr(value, "item") else value) for value in values]
