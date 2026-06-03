import hashlib
import json
from collections import OrderedDict
from datetime import UTC
from datetime import datetime
from threading import Lock
from typing import Any

from app.config import Settings
from app.core.dataset_catalog import CatalogEntry
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.models.dataset import BrowseCoverage


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
    zoom: int,
) -> str:
    digest = _overview_digest(settings, entry, variable, time_index, zoom)
    return f"{browse_artifact_root(settings, entry)}/overviews/{variable}-{time_index}-z{zoom}-{digest}.npz"


def browse_manifest_contains_overview(
    manifest: dict[str, Any] | None,
    *,
    variable: str,
    time_index: int,
    zoom: int,
) -> bool:
    return browse_manifest_overview_path(manifest, variable=variable, time_index=time_index, zoom=zoom) is not None


def browse_manifest_overview_path(
    manifest: dict[str, Any] | None,
    *,
    variable: str,
    time_index: int,
    zoom: int,
) -> str | None:
    if manifest is None:
        return None
    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        return None
    variable_entry = variables.get(variable)
    if not isinstance(variable_entry, dict):
        return None
    overviews = variable_entry.get("overviews")
    if not isinstance(overviews, dict):
        return None
    overview_entry = overviews.get(str(time_index))
    if not isinstance(overview_entry, dict):
        return None
    levels = overview_entry.get("levels")
    if isinstance(levels, dict):
        level_entry = levels.get(str(zoom))
        if isinstance(level_entry, dict):
            path = level_entry.get("path")
            return path if isinstance(path, str) else None
    path = overview_entry.get("path")
    return path if isinstance(path, str) else None


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
        "last_generated_at": datetime.now(tz=UTC).isoformat(),
        "variables": variables,
    }


def compute_browse_coverage(
    settings: Settings,
    entry: CatalogEntry,
    manifest: dict[str, Any] | None,
) -> BrowseCoverage:
    expected_zoom_levels = list(range(0, int(settings.browse_tile_max_zoom) + 1))
    expected_variables = _expected_browse_variables(entry)
    available_zoom_levels = _collect_manifest_zoom_levels(manifest)
    expected_artifact_count = 0
    available_artifact_count = 0
    missing_variables: list[str] = []
    missing_time_steps: dict[str, list[int]] = {}

    for variable in expected_variables:
        time_indices = _expected_time_indices(entry, variable)
        variable_available = 0
        for time_index in time_indices:
            expected_artifact_count += len(expected_zoom_levels)
            missing_zoom_levels = [
                zoom
                for zoom in expected_zoom_levels
                if not browse_manifest_contains_overview(
                    manifest,
                    variable=variable,
                    time_index=time_index,
                    zoom=zoom,
                )
            ]
            present_count = len(expected_zoom_levels) - len(missing_zoom_levels)
            variable_available += present_count
            available_artifact_count += present_count
            if missing_zoom_levels:
                missing_time_steps.setdefault(variable, []).append(time_index)
        if variable_available == 0:
            missing_variables.append(variable)

    status = _browse_generation_status(
        manifest=manifest,
        expected_artifact_count=expected_artifact_count,
        available_artifact_count=available_artifact_count,
    )
    return BrowseCoverage(
        expected_zoom_levels=expected_zoom_levels,
        available_zoom_levels=available_zoom_levels,
        missing_variables=missing_variables,
        missing_time_steps=missing_time_steps,
        last_generated_at=_manifest_generated_at(manifest),
        generation_status=status,
        expected_artifact_count=expected_artifact_count,
        available_artifact_count=available_artifact_count,
    )


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
    zoom: int,
) -> str:
    return hashlib.sha1(
        f"{entry.id}:{variable}:{time_index}:{zoom}:{settings.planner_version}:{settings.browse_overview_max_size}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def _manifest_cache_set(path: str, manifest: dict[str, Any]) -> None:
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE[path] = manifest
        _MANIFEST_CACHE.move_to_end(path)
        while len(_MANIFEST_CACHE) > _MANIFEST_CACHE_MAX_ENTRIES:
            _MANIFEST_CACHE.popitem(last=False)


def _expected_browse_variables(entry: CatalogEntry) -> list[str]:
    if entry.meta.variables:
        return [item.id for item in entry.meta.variables]
    return list(entry.band_names)


def _expected_time_indices(entry: CatalogEntry, variable: str) -> list[int]:
    variable_meta = next((item for item in entry.meta.variables if item.id == variable), None)
    if variable_meta is not None:
        count = max(int(variable_meta.time_steps), 1)
    elif entry.meta.time_values:
        count = max(len(entry.meta.time_values), 1)
    else:
        count = 1
    return list(range(count))


def _collect_manifest_zoom_levels(manifest: dict[str, Any] | None) -> list[int]:
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
            level_entries = overview_entry.get("levels")
            if not isinstance(level_entries, dict):
                continue
            for level in level_entries:
                try:
                    levels.add(int(level))
                except (TypeError, ValueError):
                    continue
    return sorted(levels)


def _browse_generation_status(
    *,
    manifest: dict[str, Any] | None,
    expected_artifact_count: int,
    available_artifact_count: int,
) -> str:
    if isinstance(manifest, dict):
        manifest_status = manifest.get("generation_status")
        if manifest_status in {"queued", "running", "failed"}:
            return str(manifest_status)
    if expected_artifact_count <= 0 or available_artifact_count == 0:
        return "missing"
    if available_artifact_count < expected_artifact_count:
        return "partial"
    return "complete"


def _manifest_generated_at(manifest: dict[str, Any] | None) -> datetime | None:
    if not isinstance(manifest, dict):
        return None
    raw_value = manifest.get("last_generated_at") or manifest.get("generated_at")
    if not isinstance(raw_value, str):
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
