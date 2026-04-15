import json

import xarray as xr

from app.config import Settings
from app.core.oci_object_storage import OCIObjectStorageConnector


def _resolve_zarr_uri(settings: Settings, connector: OCIObjectStorageConnector) -> str:
    if settings.oci_zarr_path:
        if settings.oci_zarr_path.startswith("oci://"):
            return settings.oci_zarr_path
        return connector.build_oci_uri(settings.oci_zarr_path)
    return connector.build_oci_uri(settings.oci_prefix)


def open_oci_zarr_dataset(
    settings: Settings,
) -> tuple[OCIObjectStorageConnector, xr.Dataset]:
    connector = OCIObjectStorageConnector(settings)
    dataset = open_dataset_from_path(
        connector=connector,
        zarr_path=_resolve_zarr_uri(settings, connector),
        consolidated=settings.oci_zarr_consolidated,
    )
    return connector, dataset


def open_dataset_from_path(
    connector: OCIObjectStorageConnector,
    zarr_path: str,
    consolidated: bool,
) -> xr.Dataset:
    filesystem = connector.get_filesystem()
    zarr_uri = zarr_path
    mapper_path = zarr_uri.removeprefix("oci://")
    mapper = filesystem.get_mapper(mapper_path)
    open_kwargs = _build_open_zarr_kwargs(
        connector=connector,
        zarr_path=zarr_path,
        consolidated=consolidated,
    )
    return xr.open_zarr(
        mapper,
        **open_kwargs,
    )


def read_store_json(
    connector: OCIObjectStorageConnector,
    object_path: str,
) -> str:
    return connector.read_text(object_path, use_cache=True)


def _read_root_zarr_json(
    connector: OCIObjectStorageConnector,
    zarr_path: str,
) -> dict | None:
    object_path = zarr_path.rstrip("/") + "/zarr.json"
    try:
        raw = read_store_json(connector=connector, object_path=object_path)
    except FileNotFoundError:
        return None
    return json.loads(raw)


def _build_open_zarr_kwargs(
    connector: OCIObjectStorageConnector,
    zarr_path: str,
    consolidated: bool,
) -> dict:
    root_metadata = _read_root_zarr_json(connector=connector, zarr_path=zarr_path)
    if root_metadata is None:
        return {
            "consolidated": consolidated,
        }

    zarr_format = int(root_metadata.get("zarr_format", 0))
    if zarr_format != 3:
        return {
            "consolidated": consolidated,
        }

    has_consolidated_metadata = "consolidated_metadata" in root_metadata
    return {
        "consolidated": consolidated and has_consolidated_metadata,
        "zarr_version": 3,
        "zarr_format": 3,
    }
