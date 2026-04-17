from types import SimpleNamespace

from app.core.browse_artifacts import browse_artifact_root
from app.core.browse_artifacts import browse_manifest_contains_overview
from app.core.browse_artifacts import browse_manifest_path
from app.core.browse_artifacts import browse_overview_object_path
from app.core.browse_artifacts import build_browse_manifest
from app.core.browse_artifacts import clear_browse_manifest_cache
from app.core.browse_artifacts import read_browse_manifest
from app.core.browse_artifacts import write_browse_manifest
from app.core.dataset_catalog import CatalogEntry
from app.models.dataset import DatasetMeta


class _FakeConnector:
    def __init__(self) -> None:
        self.payloads: dict[str, str] = {}
        self.read_calls: list[str] = []
        self.write_calls: list[tuple[str, str]] = []

    def read_text(self, path: str, *, use_cache: bool = True) -> str:
        self.read_calls.append(path)
        if path not in self.payloads:
            raise FileNotFoundError(path)
        return self.payloads[path]

    def write_text(self, path: str, payload: str, *, content_type: str = "application/json") -> None:
        self.write_calls.append((path, content_type))
        self.payloads[path] = payload


def _entry() -> CatalogEntry:
    return CatalogEntry(
        id="dataset-1",
        path="cubes/landsat/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=False,
        data_array_name="bands",
        band_array_name="band",
        band_names=[],
        band_indices={},
    )


def test_browse_artifact_paths_follow_oci_browse_prefix_root() -> None:
    settings = SimpleNamespace(
        oci_browse_prefix_root="browse",
        planner_version="v1",
        browse_overview_max_size=1536,
    )
    entry = _entry()

    assert browse_artifact_root(settings, entry) == "browse/cubes/landsat/example.zarr"
    assert browse_manifest_path(settings, entry) == "browse/cubes/landsat/example.zarr/manifest.json"
    assert browse_overview_object_path(settings, entry, "B4", 0, 3).startswith(
        "browse/cubes/landsat/example.zarr/overviews/B4-0-z3-"
    )


def test_browse_manifest_roundtrip_uses_connector_storage() -> None:
    clear_browse_manifest_cache()
    connector = _FakeConnector()
    settings = SimpleNamespace(
        oci_browse_prefix_root="browse",
        planner_version="v1",
        browse_overview_max_size=1536,
    )
    entry = _entry()
    manifest = build_browse_manifest(
        settings,
        entry,
        {
            "B4": {
                "overviews": {
                    "0": {
                        "path": browse_overview_object_path(settings, entry, "B4", 0, 3),
                        "bbox": [0.0, 0.0, 1.0, 1.0],
                        "levels": {
                            "3": {
                                "path": browse_overview_object_path(settings, entry, "B4", 0, 3),
                                "bbox": [0.0, 0.0, 1.0, 1.0],
                                "zoom": 3,
                            }
                        },
                    }
                }
            }
        },
    )

    path = write_browse_manifest(connector, settings, entry, manifest)
    first = read_browse_manifest(connector, settings, entry)
    second = read_browse_manifest(connector, settings, entry)

    assert path == "browse/cubes/landsat/example.zarr/manifest.json"
    assert first == manifest
    assert second == manifest
    assert connector.write_calls == [(path, "application/json")]
    assert connector.read_calls == []


def test_browse_manifest_contains_overview_detects_registered_variable() -> None:
    manifest = {
        "variables": {
            "B4": {
                "overviews": {
                    "0": {
                        "path": "browse/cubes/landsat/example.zarr/overviews/B4-0-z0-abcd.npz",
                        "levels": {
                            "0": {"path": "browse/cubes/landsat/example.zarr/overviews/B4-0-z0-abcd.npz"}
                        },
                    }
                }
            }
        }
    }

    assert browse_manifest_contains_overview(manifest, variable="B4", time_index=0, zoom=0) is True
    assert browse_manifest_contains_overview(manifest, variable="B5", time_index=0, zoom=0) is False
