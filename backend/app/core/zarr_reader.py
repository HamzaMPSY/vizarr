import json
from typing import Any

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


def normalize_zarr_v2_metadata_entries(metadata_entries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge v2 .zarray/.zattrs pairs into catalog-readable array nodes."""
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in metadata_entries.items():
        normalized_key = key.strip("/")
        if not normalized_key.endswith("/.zarray") or not isinstance(value, dict):
            continue

        array_name = normalized_key[: -len("/.zarray")]
        attrs = metadata_entries.get(f"{array_name}/.zattrs", {})
        attributes = dict(attrs) if isinstance(attrs, dict) else {}
        node = dict(value)
        node.setdefault("zarr_format", 2)
        node["attributes"] = attributes
        dimension_names = attributes.get("_ARRAY_DIMENSIONS")
        if isinstance(dimension_names, list) and all(isinstance(item, str) for item in dimension_names):
            node["dimension_names"] = list(dimension_names)
        normalized[array_name] = node
    return normalized


def read_zarr_v2_store_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    normalized = store_path.rstrip("/")
    try:
        raw = read_store_json(
            connector=connector,
            object_path=connector.build_oci_uri(f"{normalized}/.zmetadata"),
        )
    except FileNotFoundError:
        return _read_unconsolidated_zarr_v2_store_metadata(
            connector=connector,
            store_path=normalized,
        )

    payload = json.loads(raw)
    entries = payload.get("metadata", {})
    if not isinstance(entries, dict):
        raise ValueError("Zarr v2 consolidated metadata is missing a metadata object")

    store_metadata = dict(entries.get(".zgroup", {})) if isinstance(entries.get(".zgroup"), dict) else {}
    store_metadata.setdefault("zarr_format", 2)
    root_attrs = entries.get(".zattrs")
    if isinstance(root_attrs, dict):
        store_metadata["attributes"] = dict(root_attrs)
    store_metadata["consolidated_metadata"] = entries
    return store_metadata, normalize_zarr_v2_metadata_entries(entries)


def _read_unconsolidated_zarr_v2_store_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    raw_group = read_store_json(
        connector=connector,
        object_path=connector.build_oci_uri(f"{store_path}/.zgroup"),
    )
    store_metadata = json.loads(raw_group)
    if not isinstance(store_metadata, dict):
        raise ValueError("Zarr v2 group metadata root must be an object")
    store_metadata.setdefault("zarr_format", 2)

    try:
        raw_attrs = read_store_json(
            connector=connector,
            object_path=connector.build_oci_uri(f"{store_path}/.zattrs"),
        )
    except FileNotFoundError:
        pass
    else:
        root_attrs = json.loads(raw_attrs)
        if isinstance(root_attrs, dict):
            store_metadata["attributes"] = root_attrs

    metadata_entries: dict[str, Any] = {}
    store_prefix = store_path.rstrip("/") + "/"
    for child_prefix in connector.list_prefixes(prefix=store_prefix):
        array_name = child_prefix.removeprefix(store_prefix).rstrip("/")
        if not array_name:
            continue
        array_prefix = child_prefix.rstrip("/")
        try:
            raw_array = read_store_json(
                connector=connector,
                object_path=connector.build_oci_uri(f"{array_prefix}/.zarray"),
            )
        except FileNotFoundError:
            continue
        metadata_entries[f"{array_name}/.zarray"] = json.loads(raw_array)
        try:
            raw_array_attrs = read_store_json(
                connector=connector,
                object_path=connector.build_oci_uri(f"{array_prefix}/.zattrs"),
            )
        except FileNotFoundError:
            continue
        attrs = json.loads(raw_array_attrs)
        if isinstance(attrs, dict):
            metadata_entries[f"{array_name}/.zattrs"] = attrs

    return store_metadata, normalize_zarr_v2_metadata_entries(metadata_entries)


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
