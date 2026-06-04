from typing import Any

from app.config import Settings
from app.core.browse_artifacts import compute_browse_coverage
from app.core.browse_artifacts import read_browse_manifest
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.multiscale_store import extract_level_array_metadata
from app.core.multiscale_store import extract_level_attributes
from app.core.multiscale_store import extract_multiscale_paths
from app.core.multiscale_store import extract_max_zoom
from app.core.multiscale_store import extract_population_strategy
from app.core.multiscale_store import extract_prepopulated_zoom_max
from app.core.multiscale_store import extract_root_attributes
from app.core.multiscale_store import read_root_store_metadata
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.models.dataset import ChunkLayout
from app.models.dataset import DatasetServingProfile
from app.models.dataset import MultiscaleLevelProfile


def build_dataset_serving_profile(
    settings: Settings,
    connector: OCIObjectStorageConnector,
    entry: CatalogEntry,
) -> DatasetServingProfile:
    ensure_catalog_entry_metadata_ready(entry, connector)
    multiscale_store_path = entry.meta.multiscale_store_path
    store_metadata = (
        read_root_store_metadata(connector, multiscale_store_path)
        if multiscale_store_path
        else {}
    )
    multiscale_paths = _extract_multiscale_paths(store_metadata)
    multiscale_population_strategy = extract_population_strategy(store_metadata) or entry.meta.multiscale_population_strategy
    multiscale_prepopulated_zoom_max = (
        extract_prepopulated_zoom_max(store_metadata) or entry.meta.multiscale_prepopulated_zoom_max
    )
    multiscale_max_zoom = extract_max_zoom(store_metadata) or entry.meta.multiscale_max_zoom
    multiscale_levels = _build_multiscale_level_profiles(
        store_metadata=store_metadata,
        multiscale_paths=multiscale_paths,
        data_array_name=entry.data_array_name,
        connector=connector,
        store_path=multiscale_store_path,
    )
    browse_manifest = read_browse_manifest(connector, settings, entry)
    browse_zoom_levels = _collect_browse_overview_zoom_levels(browse_manifest)
    browse_overview_max_zoom = max(browse_zoom_levels) if browse_zoom_levels else None
    browse_coverage = compute_browse_coverage(settings, entry, browse_manifest)

    chunk_layout = _build_chunk_layout(entry)
    layout_validation = entry.layout_validation or entry.meta.layout_validation
    supported_rendering_modes = ["dynamic_tiles"]
    if entry.meta.zarr_proxy_root:
        supported_rendering_modes.append("proxy_zarr")
    if entry.meta.multiscale_proxy_root:
        supported_rendering_modes.append("multiscale_proxy")
    if browse_zoom_levels:
        supported_rendering_modes.append("browse_overviews")
    if multiscale_paths:
        supported_rendering_modes.append("multiscale")
    browser_gpu_ready = _multiscale_store_supports_browser_gpu(
        store_metadata=store_metadata,
        levels=multiscale_levels,
        proxy_root=entry.meta.multiscale_proxy_root,
    )
    browser_gpu_gaps = _browser_gpu_gaps(
        store_metadata=store_metadata,
        levels=multiscale_levels,
        proxy_root=entry.meta.multiscale_proxy_root,
    )
    browser_gpu_reason = "browser GPU eligible" if browser_gpu_ready else "; ".join(browser_gpu_gaps)
    if browser_gpu_ready:
        supported_rendering_modes.append("browser_gpu")

    browser_multiscale_ready = bool(
        entry.meta.multiscale_proxy_root
        and multiscale_paths
        and _multiscale_store_supports_browser_direct_read(
            store_metadata=store_metadata,
            multiscale_paths=multiscale_paths,
            data_array_name=entry.data_array_name,
            expected_time_steps=_expected_time_steps(entry),
            connector=connector,
            store_path=multiscale_store_path,
        )
    )
    seamless_rendering_ready = bool(
        entry.meta.zarr_proxy_root
        and (
            browser_multiscale_ready
            or (
                browse_overview_max_zoom is not None
                and browse_overview_max_zoom >= settings.browse_tile_max_zoom
            )
        )
    )

    gaps = _standards_compatibility_gaps(entry)
    if entry.meta.zarr_proxy_root is None:
        gaps.append("missing_browser_proxy")
    if not multiscale_paths:
        gaps.append("missing_multiscale_pyramid")
    elif not browser_multiscale_ready:
        gaps.append("multiscale_store_not_browser_readable")
    if browse_overview_max_zoom is None:
        gaps.append("missing_browse_overviews")
    elif browse_overview_max_zoom < settings.browse_tile_max_zoom:
        gaps.append("incomplete_browse_overview_coverage")

    return DatasetServingProfile(
        dataset_id=entry.id,
        zarr_format=entry.zarr_format,
        zarr_consolidated=entry.consolidated,
        zarr_proxy_root=entry.meta.zarr_proxy_root,
        multiscale_store_path=entry.meta.multiscale_store_path,
        multiscale_zarr_format=entry.meta.multiscale_zarr_format,
        multiscale_zarr_consolidated=entry.meta.multiscale_zarr_consolidated,
        multiscale_proxy_root=entry.meta.multiscale_proxy_root,
        multiscale_population_strategy=multiscale_population_strategy,
        multiscale_prepopulated_zoom_max=multiscale_prepopulated_zoom_max,
        multiscale_max_zoom=multiscale_max_zoom,
        data_array_name=entry.data_array_name,
        variable_ids=[item.id for item in entry.meta.variables],
        has_multiscale=bool(multiscale_paths),
        multiscale_paths=multiscale_paths,
        multiscale_levels=multiscale_levels,
        browse_overview_zoom_levels=browse_zoom_levels,
        browse_overview_max_zoom=browse_overview_max_zoom,
        browse_coverage=browse_coverage,
        chunk_layout=chunk_layout,
        layout_validation=layout_validation,
        supported_rendering_modes=supported_rendering_modes,
        browser_multiscale_ready=browser_multiscale_ready,
        browser_gpu_ready=browser_gpu_ready,
        browser_gpu_reason=browser_gpu_reason,
        browser_gpu_gaps=browser_gpu_gaps,
        seamless_rendering_ready=seamless_rendering_ready,
        seamless_rendering_gaps=gaps,
    )


def _extract_multiscale_paths(store_metadata: dict[str, Any]) -> list[str]:
    return extract_multiscale_paths(store_metadata)


def _collect_browse_overview_zoom_levels(manifest: dict[str, Any] | None) -> list[int]:
    if not isinstance(manifest, dict):
        return []

    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        return []

    levels: set[int] = set()
    for variable_entry in variables.values():
        if not isinstance(variable_entry, dict):
            continue
        overviews = variable_entry.get("overviews")
        if not isinstance(overviews, dict):
            continue
        for overview_entry in overviews.values():
            if not isinstance(overview_entry, dict):
                continue
            overview_levels = overview_entry.get("levels")
            if not isinstance(overview_levels, dict):
                continue
            for level in overview_levels:
                try:
                    levels.add(int(level))
                except ValueError:
                    continue
    return sorted(levels)


def _standards_compatibility_gaps(entry: CatalogEntry) -> list[str]:
    gaps: list[str] = []
    layout_validation = entry.layout_validation or entry.meta.layout_validation
    if layout_validation is not None and not layout_validation.accepted:
        for issue in layout_validation.issues:
            if issue.code not in gaps:
                gaps.append(issue.code)
    metadata = entry.data_array_meta
    if metadata is None:
        gaps.append("missing_data_array_metadata")
    else:
        dimension_gap = _dimension_metadata_gap(metadata.dimension_names)
        if dimension_gap is not None:
            gaps.append(dimension_gap)
    if not entry.crs_wkt:
        gaps.append("missing_crs_metadata")
    if entry.geo_transform is None and (entry.x_meta is None or entry.y_meta is None):
        gaps.append("missing_spatial_transform")
    return gaps


def _dimension_metadata_gap(dimension_names: tuple[str, ...]) -> str | None:
    if not dimension_names:
        return "missing_dimension_metadata"
    if len(dimension_names) == 2 and dimension_names == ("y", "x"):
        return None
    if len(dimension_names) == 3 and dimension_names == ("time", "y", "x"):
        return None
    if len(dimension_names) == 4 and dimension_names[0] == "time" and dimension_names[-2:] == ("y", "x"):
        return None
    return "unsupported_dimension_order"


def _build_chunk_layout(entry: CatalogEntry) -> ChunkLayout | None:
    metadata = entry.data_array_meta
    if metadata is None:
        return None
    sharding = metadata.sharding
    if sharding is None:
        return ChunkLayout(
            sharded=False,
            shard_shape=list(metadata.chunk_shape),
            inner_chunk_shape=list(metadata.chunk_shape),
        )
    return ChunkLayout(
        sharded=True,
        shard_shape=list(metadata.chunk_shape),
        inner_chunk_shape=list(sharding.chunk_shape),
    )


def _build_multiscale_level_profiles(
    *,
    store_metadata: dict[str, Any],
    multiscale_paths: list[str],
    data_array_name: str | None,
    connector: OCIObjectStorageConnector,
    store_path: str | None,
) -> list[MultiscaleLevelProfile]:
    if not multiscale_paths or not data_array_name or not store_path:
        return []

    browse_zoom_levels = _extract_browse_zoom_levels(store_metadata)
    levels: list[MultiscaleLevelProfile] = []
    for index, path in enumerate(multiscale_paths):
        attrs = extract_level_attributes(
            store_metadata,
            path,
            connector=connector,
            store_path=store_path,
        )
        array_metadata = extract_level_array_metadata(
            store_metadata,
            path,
            data_array_name,
            connector=connector,
            store_path=store_path,
        )
        shape = _int_list(array_metadata.get("shape"))
        chunks = _int_list(array_metadata.get("chunks"))
        dtype = array_metadata.get("dtype")
        compressor = array_metadata.get("compressor")
        filters = array_metadata.get("filters")
        order = array_metadata.get("order")
        dimension_separator = array_metadata.get("dimension_separator")
        bbox_wgs84 = _float_bbox(attrs.get("bbox_wgs84"))
        bbox_epsg3857 = _float_bbox(attrs.get("bbox_epsg3857"))
        browse_zoom = browse_zoom_levels[index] if index < len(browse_zoom_levels) else None
        gaps = _browser_level_gaps(
            shape=shape,
            chunks=chunks,
            dtype=dtype,
            compressor=compressor,
            filters=filters,
            order=order,
            bbox_wgs84=bbox_wgs84,
            bbox_epsg3857=bbox_epsg3857,
            browse_zoom=browse_zoom,
        )
        browser_readable = not any(
            gap in gaps
            for gap in {
                "missing_shape",
                "missing_chunks",
                "unsupported_dtype",
                "unsupported_compressor",
                "unsupported_filters",
                "unsupported_order",
                "unsupported_temporal_or_band_chunks",
                "unsupported_spatial_chunks",
            }
        )
        browser_gpu_compatible = (
            browser_readable
            and "missing_bounds" not in gaps
            and "missing_browse_zoom" not in gaps
        )
        levels.append(
            MultiscaleLevelProfile(
                path=path,
                browse_zoom=browse_zoom,
                bbox_wgs84=bbox_wgs84,
                bbox_epsg3857=bbox_epsg3857,
                shape=shape,
                chunks=chunks,
                dtype=dtype if isinstance(dtype, str) else None,
                compressor=compressor,
                filters=filters,
                order=order if isinstance(order, str) else None,
                dimension_separator="/" if dimension_separator == "/" else ".",
                browser_readable=browser_readable,
                browser_gpu_compatible=browser_gpu_compatible,
                gaps=gaps,
            )
        )
    return levels


def _extract_browse_zoom_levels(store_metadata: dict[str, Any]) -> list[int | None]:
    values = extract_root_attributes(store_metadata).get("browse_zoom_levels")
    if not isinstance(values, list):
        return []
    result: list[int | None] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            result.append(None)
    return result


def _browser_level_gaps(
    *,
    shape: list[int],
    chunks: list[int],
    dtype: Any,
    compressor: Any,
    filters: Any,
    order: Any,
    bbox_wgs84: list[float] | None,
    bbox_epsg3857: list[float] | None,
    browse_zoom: int | None,
) -> list[str]:
    gaps: list[str] = []
    if len(shape) != 4:
        gaps.append("missing_shape")
    if len(chunks) != 4:
        gaps.append("missing_chunks")
    if dtype != "<f4":
        gaps.append("unsupported_dtype")
    if compressor is not None:
        gaps.append("unsupported_compressor")
    if not _filters_empty(filters):
        gaps.append("unsupported_filters")
    if order != "C":
        gaps.append("unsupported_order")
    if len(chunks) == 4 and (chunks[0] != 1 or chunks[1] != 1):
        gaps.append("unsupported_temporal_or_band_chunks")
    if len(chunks) == 4 and (chunks[2] != 256 or chunks[3] != 256):
        gaps.append("unsupported_spatial_chunks")
    if bbox_wgs84 is None and bbox_epsg3857 is None:
        gaps.append("missing_bounds")
    if browse_zoom is None:
        gaps.append("missing_browse_zoom")
    return gaps


def _filters_empty(filters: Any) -> bool:
    return filters is None or filters == []


def _float_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _multiscale_store_supports_browser_gpu(
    *,
    store_metadata: dict[str, Any],
    levels: list[MultiscaleLevelProfile],
    proxy_root: str | None,
) -> bool:
    return bool(
        proxy_root
        and int(store_metadata.get("zarr_format", 0) or 0) == 2
        and "consolidated_metadata" in store_metadata
        and levels
        and all(level.browser_gpu_compatible for level in levels)
    )


def _browser_gpu_gaps(
    *,
    store_metadata: dict[str, Any],
    levels: list[MultiscaleLevelProfile],
    proxy_root: str | None,
) -> list[str]:
    gaps: list[str] = []
    if not proxy_root:
        gaps.append("missing_multiscale_proxy")
    if int(store_metadata.get("zarr_format", 0) or 0) != 2:
        gaps.append("unsupported_multiscale_zarr_format")
    if "consolidated_metadata" not in store_metadata:
        gaps.append("missing_consolidated_metadata")
    if not levels:
        gaps.append("missing_multiscale_levels")
    for level in levels:
        if not level.browser_gpu_compatible:
            level_label = level.path or "<unknown>"
            gaps.extend(f"level:{level_label}:{gap}" for gap in level.gaps)
    return gaps


def _multiscale_store_supports_browser_direct_read(
    *,
    store_metadata: dict[str, Any],
    multiscale_paths: list[str],
    data_array_name: str | None,
    expected_time_steps: int,
    connector: OCIObjectStorageConnector,
    store_path: str | None,
) -> bool:
    if int(store_metadata.get("zarr_format", 0) or 0) != 2:
        return False
    if not data_array_name or not store_path:
        return False
    return all(
        _array_supports_browser_direct_read(
            extract_level_array_metadata(
                store_metadata,
                path,
                data_array_name,
                connector=connector,
                store_path=store_path,
            ),
            expected_time_steps=expected_time_steps,
        )
        for path in multiscale_paths
    )


def _array_supports_browser_direct_read(
    array_metadata: dict[str, Any],
    *,
    expected_time_steps: int,
) -> bool:
    shape = _int_list(array_metadata.get("shape"))
    chunks = _int_list(array_metadata.get("chunks"))
    return bool(
        len(shape) == 4
        and len(chunks) == 4
        and shape[0] >= expected_time_steps
        and shape[1] >= 1
        and shape[2] >= chunks[2] >= 1
        and shape[3] >= chunks[3] >= 1
        and array_metadata.get("dtype") == "<f4"
        and array_metadata.get("compressor") is None
        and array_metadata.get("filters") is None
        and array_metadata.get("order") == "C"
        and chunks[0] == 1
        and chunks[1] == 1
        and chunks[2] == 256
        and chunks[3] == 256
    )


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            return []
    return result


def _expected_time_steps(entry: CatalogEntry) -> int:
    if entry.meta.time_values:
        return max(len(entry.meta.time_values), 1)
    if entry.meta.variables:
        return max(max(variable.time_steps, 1) for variable in entry.meta.variables)
    return 1
