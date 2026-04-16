from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import get_or_build_catalog
from app.core.datasets import DatasetRegistry
from app.index.planner_index import CubeIndexRecord
from app.models.dataset import DatasetMeta


def build_index_records(app) -> list[CubeIndexRecord]:
    settings = app.state.settings
    browse_styles = [
        item.strip()
        for item in settings.browse_enabled_styles.split(",")
        if item.strip()
    ]

    if settings.storage_backend == "oci_zarr" and getattr(app.state, "storage_connector", None) is not None:
        catalog = get_or_build_catalog(app)
        records: list[CubeIndexRecord] = []
        for entry in catalog.values():
            records.extend(
                _records_from_catalog_entry(
                    entry=entry,
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
    entry: CatalogEntry,
    browse_styles: list[str],
    version: str,
) -> list[CubeIndexRecord]:
    return _records_from_meta(
        collection_id=entry.meta.id,
        meta=entry.meta,
        source_path=entry.path,
        serving_path=f"{entry.path}#serving",
        browse_path_root=f"{entry.path}#browse",
        browse_styles=browse_styles,
        version=version,
        crs=entry.crs_wkt,
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
            crs=crs,
        ),
    ]
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
                style=style,
                crs=crs,
            )
        )
    return records
