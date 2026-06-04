from app.config import Settings
from app.core.browse_artifacts import browse_artifact_root
from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import get_or_build_catalog
from app.core.datasets import DatasetRegistry
from app.index.planner_index import CubeIndexRecord
from app.models.dataset import DatasetMeta


def build_index_records(app, *, allow_catalog_build: bool = True) -> list[CubeIndexRecord]:
    settings = app.state.settings
    browse_styles = [
        item.strip()
        for item in settings.browse_enabled_styles.split(",")
        if item.strip()
    ]

    if settings.storage_backend == "oci_zarr" and getattr(app.state, "storage_connector", None) is not None:
        catalog = getattr(app.state, "dataset_catalog", None)
        if catalog is None and allow_catalog_build:
            catalog = get_or_build_catalog(app)
        records: list[CubeIndexRecord] = []
        if catalog is not None:
            for entry in catalog.values():
                records.extend(
                    _records_from_catalog_entry(
                        settings=settings,
                        entry=entry,
                        browse_styles=browse_styles,
                        version=settings.planner_version,
                    )
                )
            if records:
                return records

        manifest = getattr(app.state, "dataset_manifest", None)
        if manifest:
            for item in manifest:
                meta = item if isinstance(item, DatasetMeta) else DatasetMeta.model_validate(item)
                records.extend(
                    _records_from_manifest_meta(
                        meta=meta,
                        browse_styles=browse_styles,
                        version=settings.planner_version,
                    )
                )
            if records:
                return records

    registry = app.state.registry
    return _records_from_registry(
        registry=registry,
        browse_styles=browse_styles,
        version=settings.planner_version,
    )


def _records_from_registry(
    *,
    registry: DatasetRegistry,
    browse_styles: list[str],
    version: str,
) -> list[CubeIndexRecord]:
    return _records_from_meta(
        collection_id=registry.meta.id,
        meta=registry.meta,
        source_path=f"synthetic://{registry.meta.id}/source",
        serving_path=f"synthetic://{registry.meta.id}/serving",
        browse_path_root=f"synthetic://{registry.meta.id}/browse",
        browse_styles=browse_styles,
        version=version,
        crs="EPSG:4326",
    )


def _records_from_catalog_entry(
    *,
    settings: Settings,
    entry: CatalogEntry,
    browse_styles: list[str],
    version: str,
) -> list[CubeIndexRecord]:
    return _records_from_meta(
        collection_id=entry.meta.id,
        meta=entry.meta,
        source_path=entry.path,
        serving_path=entry.meta.zarr_proxy_root or f"/api/zarr/{entry.id}",
        browse_path_root=browse_artifact_root(settings, entry),
        browse_styles=browse_styles,
        version=version,
        crs=entry.crs_wkt,
    )


def _records_from_manifest_meta(
    *,
    meta: DatasetMeta,
    browse_styles: list[str],
    version: str,
) -> list[CubeIndexRecord]:
    serving_path = meta.zarr_proxy_root or f"/api/zarr/{meta.id}"
    return _records_from_meta(
        collection_id=meta.id,
        meta=meta,
        source_path=serving_path,
        serving_path=serving_path,
        browse_path_root=serving_path,
        browse_styles=browse_styles,
        version=version,
        crs=meta.crs_wkt or meta.crs_authority,
    )


def _records_from_meta(
    *,
    collection_id: str,
    meta: DatasetMeta,
    source_path: str,
    serving_path: str,
    browse_path_root: str,
    browse_styles: list[str],
    version: str,
    crs: str | None,
    native_resolution_m: float | None = None,
) -> list[CubeIndexRecord]:
    bands = tuple(item.id for item in meta.variables)
    records = [
        CubeIndexRecord(
            cube_id=f"{collection_id}:browse",
            collection_id=collection_id,
            representation="browse",
            path=browse_path_root,
            bands=bands,
            version=version,
            bbox_wgs84=meta.bounds,
            native_resolution_m=native_resolution_m if native_resolution_m is not None else meta.native_resolution_m,
            crs=crs,
        ),
        CubeIndexRecord(
            cube_id=f"{collection_id}:source",
            collection_id=collection_id,
            representation="source",
            path=source_path,
            bands=bands,
            version=version,
            bbox_wgs84=meta.bounds,
            native_resolution_m=native_resolution_m if native_resolution_m is not None else meta.native_resolution_m,
            crs=crs,
        ),
        CubeIndexRecord(
            cube_id=f"{collection_id}:serving",
            collection_id=collection_id,
            representation="serving",
            path=serving_path,
            bands=bands,
            version=version,
            bbox_wgs84=meta.bounds,
            native_resolution_m=native_resolution_m if native_resolution_m is not None else meta.native_resolution_m,
            crs=crs,
        ),
    ]
    if meta.multiscale_store_path:
        records.append(
            CubeIndexRecord(
                cube_id=f"{collection_id}:pyramid",
                collection_id=collection_id,
                representation="pyramid",
                path=meta.multiscale_store_path,
                bands=bands,
                version=version,
                bbox_wgs84=meta.bounds,
                native_resolution_m=native_resolution_m if native_resolution_m is not None else meta.native_resolution_m,
                crs=crs,
                population_strategy=meta.multiscale_population_strategy,
                prepopulated_zoom_max=meta.multiscale_prepopulated_zoom_max,
                multiscale_max_zoom=meta.multiscale_max_zoom,
            )
        )
    for style in browse_styles:
        records.append(
            CubeIndexRecord(
                cube_id=f"{collection_id}:browse:{style}",
                collection_id=collection_id,
                representation="browse",
                path=f"{browse_path_root}/{style}",
                bands=bands,
                version=version,
                bbox_wgs84=meta.bounds,
                native_resolution_m=native_resolution_m if native_resolution_m is not None else meta.native_resolution_m,
                style=style,
                crs=crs,
            )
        )
    return records
