import hashlib
import json
from collections import OrderedDict
from threading import Lock
from typing import Any

from app.config import Settings
from app.core.dataset_catalog import CatalogEntry
from app.core.oci_object_storage import OCIObjectStorageConnector


_MANIFEST_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MANIFEST_CACHE_LOCK = Lock()
_MANIFEST_CACHE_MAX_ENTRIES = 32
_MANIFEST_NAME = "manifest.json"


def browse_artifact_root(settings: Settings, entry: CatalogEntry) -> str:
    root = settings.oci_browse_prefix_root.strip("/").rstrip("/")
    store_path = entry.path.strip("/")
    if not root:
        return store_path
    return f"{root}/{store_path}"


def browse_manifest_path(settings: Settings, entry: CatalogEntry) -> str:
    return f"{browse_artifact_root(settings, entry)}/{_MANIFEST_NAME}"


def browse_overview_object_path(
    settings: Settings,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> str:
    digest = _overview_digest(settings, entry, variable, time_index)
    return f"{browse_artifact_root(settings, entry)}/overviews/{variable}-{time_index}-{digest}.npz"


def browse_manifest_contains_overview(
    manifest: dict[str, Any] | None,
    *,
    variable: str,
    time_index: int,
) -> bool:
    if manifest is None:
        return False
    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        return False
    variable_entry = variables.get(variable)
    if not isinstance(variable_entry, dict):
        return False
    overviews = variable_entry.get("overviews")
    if not isinstance(overviews, dict):
        return False
    overview_entry = overviews.get(str(time_index))
    return isinstance(overview_entry, dict) and isinstance(overview_entry.get("path"), str)


def build_browse_manifest(
    settings: Settings,
    entry: CatalogEntry,
    variables: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dataset_id": entry.id,
        "store_path": entry.path,
        "planner_version": settings.planner_version,
        "overview_max_size": settings.browse_overview_max_size,
        "variables": variables,
    }


def read_browse_manifest(
    connector: OCIObjectStorageConnector,
    settings: Settings,
    entry: CatalogEntry,
    *,
    use_cache: bool = True,
) -> dict[str, Any] | None:
    path = browse_manifest_path(settings, entry)
    if use_cache:
        with _MANIFEST_CACHE_LOCK:
            cached = _MANIFEST_CACHE.get(path)
            if cached is not None:
                _MANIFEST_CACHE.move_to_end(path)
                return cached

    try:
        manifest = json.loads(connector.read_text(path, use_cache=use_cache))
    except FileNotFoundError:
        return None

    if use_cache:
        _manifest_cache_set(path, manifest)
    return manifest


def write_browse_manifest(
    connector: OCIObjectStorageConnector,
    settings: Settings,
    entry: CatalogEntry,
    manifest: dict[str, Any],
) -> str:
    path = browse_manifest_path(settings, entry)
    connector.write_text(path, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    _manifest_cache_set(path, manifest)
    return path


def clear_browse_manifest_cache() -> None:
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE.clear()


def _overview_digest(
    settings: Settings,
    entry: CatalogEntry,
    variable: str,
    time_index: int,
) -> str:
    return hashlib.sha1(
        f"{entry.id}:{variable}:{time_index}:{settings.planner_version}:{settings.browse_overview_max_size}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def _manifest_cache_set(path: str, manifest: dict[str, Any]) -> None:
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE[path] = manifest
        _MANIFEST_CACHE.move_to_end(path)
        while len(_MANIFEST_CACHE) > _MANIFEST_CACHE_MAX_ENTRIES:
            _MANIFEST_CACHE.popitem(last=False)
