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
    return xr.open_zarr(
        mapper,
        consolidated=consolidated,
    )


def read_store_json(
    connector: OCIObjectStorageConnector,
    object_path: str,
) -> str:
    filesystem = connector.get_filesystem()
    resolved = object_path.removeprefix("oci://")
    return filesystem.cat_file(resolved).decode("utf-8")
