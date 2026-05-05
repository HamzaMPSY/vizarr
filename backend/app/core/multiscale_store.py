import json
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.core.oci_object_storage import OCIObjectStorageConnector


@dataclass(frozen=True)
class MultiscaleStoreSummary:
    path: str
    zarr_format: int
    consolidated: bool
    population_strategy: str | None = None
    prepopulated_zoom_max: int | None = None
    max_zoom: int | None = None


def multiscale_store_path(settings: Settings, source_store_path: str) -> str | None:
    root = getattr(settings, "oci_multiscale_prefix_root", "").strip("/").rstrip("/")
    store_path = source_store_path.strip("/")
    if not root or not store_path:
        return None
    return f"{root}/{store_path}"


def multiscale_proxy_root(dataset_id: str) -> str:
    return f"/api/zarr/multiscale/{dataset_id}"


def probe_multiscale_store(
    connector: OCIObjectStorageConnector,
    store_path: str | None,
) -> MultiscaleStoreSummary | None:
    if not store_path:
        return None
    try:
        metadata = read_root_store_metadata(connector, store_path)
    except FileNotFoundError:
        return None
    return MultiscaleStoreSummary(
        path=store_path,
        zarr_format=int(metadata.get("zarr_format", 0) or 0),
        consolidated=_is_consolidated_store(metadata),
        population_strategy=extract_population_strategy(metadata),
        prepopulated_zoom_max=extract_prepopulated_zoom_max(metadata),
        max_zoom=extract_max_zoom(metadata),
    )


def read_root_store_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> dict[str, Any]:
    normalized = store_path.rstrip("/")
    for candidate in ("zarr.json", ".zmetadata", ".zgroup"):
        object_path = connector.build_oci_uri(f"{normalized}/{candidate}")
        try:
            raw = connector.read_text(object_path, use_cache=True)
        except FileNotFoundError:
            continue
        payload = json.loads(raw)
        if candidate == "zarr.json":
            return payload
        if candidate == ".zmetadata":
            root_attrs = payload.get("metadata", {}).get(".zattrs", {})
            result: dict[str, Any] = {"zarr_format": 2}
            if isinstance(root_attrs, dict):
                result["attributes"] = root_attrs
            result["consolidated_metadata"] = payload.get("metadata", {})
            return result

        result = dict(payload)
        result.setdefault("zarr_format", 2)
        attrs_path = connector.build_oci_uri(f"{normalized}/.zattrs")
        try:
            attrs_raw = connector.read_text(attrs_path, use_cache=True)
        except FileNotFoundError:
            return result
        attrs = json.loads(attrs_raw)
        if isinstance(attrs, dict):
            result["attributes"] = attrs
        return result
    raise FileNotFoundError(store_path)


def extract_root_attributes(store_metadata: dict[str, Any]) -> dict[str, Any]:
    attributes = store_metadata.get("attributes", {})
    if isinstance(attributes, dict):
        return attributes
    return {}


def extract_multiscale_paths(store_metadata: dict[str, Any]) -> list[str]:
    multiscales = extract_root_attributes(store_metadata).get("multiscales")
    if not isinstance(multiscales, list):
        return []

    paths: list[str] = []
    for item in multiscales:
        if not isinstance(item, dict):
            continue
        datasets = item.get("datasets")
        if not isinstance(datasets, list):
            continue
        for dataset in datasets:
            if not isinstance(dataset, dict):
                continue
            path = dataset.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return sorted(set(paths), key=_multiscale_path_sort_key)


def extract_population_strategy(store_metadata: dict[str, Any]) -> str | None:
    value = extract_root_attributes(store_metadata).get("population_strategy")
    return value if isinstance(value, str) and value else None


def extract_prepopulated_zoom_max(store_metadata: dict[str, Any]) -> int | None:
    value = extract_root_attributes(store_metadata).get("prepopulated_zoom_max")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_max_zoom(store_metadata: dict[str, Any]) -> int | None:
    value = extract_root_attributes(store_metadata).get("max_zoom")
    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    zooms: list[int] = []
    for path in extract_multiscale_paths(store_metadata):
        try:
            zooms.append(int(path))
        except ValueError:
            continue
    if not zooms:
        return None
    return max(zooms)


def extract_consolidated_metadata(store_metadata: dict[str, Any]) -> dict[str, Any]:
    metadata = store_metadata.get("consolidated_metadata", {})
    if isinstance(metadata, dict):
        return metadata
    return {}


def extract_level_attributes(
    store_metadata: dict[str, Any],
    level_path: str,
    *,
    connector: OCIObjectStorageConnector | None = None,
    store_path: str | None = None,
) -> dict[str, Any]:
    metadata = extract_consolidated_metadata(store_metadata)
    marker = object()
    attrs = metadata.get(f"{level_path}/.zattrs", marker)
    if isinstance(attrs, dict):
        return attrs
    if connector is not None and store_path:
        object_path = connector.build_oci_uri(f"{store_path.rstrip('/')}/{level_path}/.zattrs")
        try:
            raw = connector.read_text(object_path, use_cache=True)
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    return {}


def extract_level_array_metadata(
    store_metadata: dict[str, Any],
    level_path: str,
    data_array_name: str,
    *,
    connector: OCIObjectStorageConnector | None = None,
    store_path: str | None = None,
) -> dict[str, Any]:
    metadata = extract_consolidated_metadata(store_metadata)
    marker = object()
    array_metadata = metadata.get(f"{level_path}/{data_array_name}/.zarray", marker)
    if isinstance(array_metadata, dict):
        return array_metadata
    if connector is not None and store_path:
        object_path = connector.build_oci_uri(f"{store_path.rstrip('/')}/{level_path}/{data_array_name}/.zarray")
        try:
            raw = connector.read_text(object_path, use_cache=True)
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_consolidated_store(store_metadata: dict[str, Any]) -> bool:
    zarr_format = int(store_metadata.get("zarr_format", 0) or 0)
    if zarr_format == 3:
        return "consolidated_metadata" in store_metadata
    return "consolidated_metadata" in store_metadata


def _multiscale_path_sort_key(path: str) -> tuple[int, int | str]:
    try:
        return (0, int(path))
    except ValueError:
        return (1, path)
