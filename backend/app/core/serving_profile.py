from typing import Any

from app.config import Settings
from app.core.browse_artifacts import read_browse_manifest
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.multiscale_store import extract_level_array_metadata
from app.core.multiscale_store import extract_multiscale_paths
from app.core.multiscale_store import extract_max_zoom
from app.core.multiscale_store import extract_population_strategy
from app.core.multiscale_store import extract_prepopulated_zoom_max
from app.core.multiscale_store import read_root_store_metadata
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.models.dataset import ChunkLayout
from app.models.dataset import DatasetServingProfile


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
    browse_manifest = read_browse_manifest(connector, settings, entry)
    browse_zoom_levels = _collect_browse_overview_zoom_levels(browse_manifest)
    browse_overview_max_zoom = max(browse_zoom_levels) if browse_zoom_levels else None

    chunk_layout = _build_chunk_layout(entry)
    supported_rendering_modes = ["dynamic_tiles"]
    if entry.meta.zarr_proxy_root:
        supported_rendering_modes.append("proxy_zarr")
    if entry.meta.multiscale_proxy_root:
        supported_rendering_modes.append("multiscale_proxy")
    if browse_zoom_levels:
        supported_rendering_modes.append("browse_overviews")
    if multiscale_paths:
        supported_rendering_modes.append("multiscale")

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

    gaps: list[str] = []
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
        browse_overview_zoom_levels=browse_zoom_levels,
        browse_overview_max_zoom=browse_overview_max_zoom,
        chunk_layout=chunk_layout,
        supported_rendering_modes=supported_rendering_modes,
        browser_multiscale_ready=browser_multiscale_ready,
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
