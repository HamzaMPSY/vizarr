import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.browse_artifacts import browse_manifest_path
from app.core.browse_artifacts import browse_manifest_contains_overview
from app.core.browse_artifacts import browse_overview_object_path
from app.core.browse_artifacts import clear_browse_manifest_cache
from app.core.browse_tiles import browse_overview_exists
from app.core.browse_tiles import build_and_store_browse_overviews
from app.core.browse_tiles import get_or_create_browse_overview
from app.core.browse_tiles import prewarm_browse_overviews
from app.core.dataset_catalog import CatalogEntry
from app.models.dataset import DatasetBounds
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta


class _FakeConnector:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.text_payloads: dict[str, str] = {}
        self.write_bytes_calls: list[str] = []
        self.write_text_calls: list[str] = []

    def read_bytes(self, path: str, *, use_cache: bool = False) -> bytes:
        if path not in self.payloads:
            raise FileNotFoundError(path)
        return self.payloads[path]

    def read_text(self, path: str, *, use_cache: bool = True) -> str:
        if path not in self.text_payloads:
            raise FileNotFoundError(path)
        return self.text_payloads[path]

    def write_bytes(self, path: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.write_bytes_calls.append(path)
        self.payloads[path] = payload

    def write_text(self, path: str, payload: str, *, content_type: str = "application/json") -> None:
        self.write_text_calls.append(path)
        self.text_payloads[path] = payload

    def object_exists(self, path: str) -> bool:
        return path in self.payloads


def _entry() -> CatalogEntry:
    return CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[
                VariableMeta(
                    id="B1",
                    name="B1",
                    unit="DN",
                    time_steps=1,
                    stats={"min": 0.0, "max": 1.0, "p02": 0.0, "p98": 1.0},
                ),
                VariableMeta(
                    id="B2",
                    name="B2",
                    unit="DN",
                    time_steps=1,
                    stats={"min": 0.0, "max": 1.0, "p02": 0.0, "p98": 1.0},
                ),
            ],
        ),
        zarr_format=3,
        consolidated=False,
        data_array_name="bands",
        band_array_name="band",
        band_names=["B1", "B2"],
        band_indices={"B1": 0, "B2": 1},
    )


def test_prewarm_browse_overviews_warms_first_variable_only_by_default(monkeypatch) -> None:
    entry = _entry()
    warmed: list[str] = []

    monkeypatch.setattr("app.core.browse_tiles.browse_overview_exists", lambda **_kwargs: False)
    monkeypatch.setattr(
        "app.core.browse_tiles.get_or_create_browse_overview",
        lambda **kwargs: warmed.append(kwargs["variable"]) or (np.zeros((1, 1), dtype=np.float32), (0.0, 0.0, 1.0, 1.0)),
    )

    count = prewarm_browse_overviews(
        SimpleNamespace(browse_tile_max_zoom=8),
        object(),  # type: ignore[arg-type]
        {"dataset-1": entry},
    )

    assert count == 1
    assert warmed == ["B1"]


def test_prewarm_browse_overviews_can_warm_all_variables(monkeypatch) -> None:
    entry = _entry()
    warmed: list[str] = []

    monkeypatch.setattr("app.core.browse_tiles.browse_overview_exists", lambda **_kwargs: False)
    monkeypatch.setattr(
        "app.core.browse_tiles.get_or_create_browse_overview",
        lambda **kwargs: warmed.append(kwargs["variable"]) or (np.zeros((1, 1), dtype=np.float32), (0.0, 0.0, 1.0, 1.0)),
    )

    count = prewarm_browse_overviews(
        SimpleNamespace(browse_tile_max_zoom=8),
        object(),  # type: ignore[arg-type]
        {"dataset-1": entry},
        all_variables=True,
    )

    assert count == 2
    assert warmed == ["B1", "B2"]


def test_browse_overview_exists_checks_disk_cache(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()
    cache_dir = tmp_path / entry.id
    cache_dir.mkdir(parents=True)
    cache_file = next(iter(cache_dir.glob("*.npz")), None)
    assert cache_file is None

    digest = "ignored"
    path = cache_dir / f"B1-0-{digest}.npz"
    np.savez_compressed(path, data=np.zeros((1, 1), dtype=np.float32), bbox=np.asarray([0.0, 0.0, 1.0, 1.0]))

    monkeypatch_path = path
    # Align the helper's computed path with the test file.
    from app.core import browse_tiles as browse_tiles_module

    original = browse_tiles_module._overview_cache_path
    browse_tiles_module._overview_cache_path = lambda *_args, **_kwargs: monkeypatch_path
    try:
        assert browse_overview_exists(
            settings=settings,
            connector=connector,
            entry=entry,
            variable="B1",
            time_index=0,
            zoom=8,
        ) is True
    finally:
        browse_tiles_module._overview_cache_path = original


def test_browse_overview_exists_exact_rejects_lower_manifest_level(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()
    clear_browse_manifest_cache()
    manifest_path = browse_manifest_path(settings, entry)
    connector.text_payloads[manifest_path] = json.dumps(
        {
            "variables": {
                "B1": {
                    "overviews": {
                        "0": {
                            "path": browse_overview_object_path(settings, entry, "B1", 0, 1),
                            "levels": {
                                "1": {"path": browse_overview_object_path(settings, entry, "B1", 0, 1)}
                            },
                        }
                    }
                }
            }
        }
    )

    assert browse_overview_exists(
        settings=settings,
        connector=connector,
        entry=entry,
        variable="B1",
        time_index=0,
        zoom=8,
        exact=True,
    ) is False
    assert browse_overview_exists(
        settings=settings,
        connector=connector,
        entry=entry,
        variable="B1",
        time_index=0,
        zoom=8,
    ) is True


def test_get_or_create_browse_overview_prefers_oci_manifest_artifact(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=False,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()
    clear_browse_manifest_cache()
    object_path = browse_overview_object_path(settings, entry, "B1", 0, 3)
    payload_path = browse_manifest_path(settings, entry)
    connector.payloads[object_path] = _serialized_overview()
    connector.text_payloads[payload_path] = json.dumps(
        {
            "variables": {
                "B1": {
                    "overviews": {
                        "0": {
                            "path": object_path,
                            "levels": {
                                "3": {"path": object_path}
                            },
                        }
                    }
                }
            }
        }
    )

    overview, bbox, source = get_or_create_browse_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable="B1",
        time_index=0,
        zoom=3,
    )

    assert source == "oci"
    assert overview.shape == (1, 1)
    assert bbox == (0.0, 0.0, 1.0, 1.0)


def test_get_or_create_browse_overview_can_use_extended_zoom_override(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=False,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()
    clear_browse_manifest_cache()
    object_path = browse_overview_object_path(settings, entry, "B1", 0, 12)
    payload_path = browse_manifest_path(settings, entry)
    connector.payloads[object_path] = _serialized_overview()
    connector.text_payloads[payload_path] = json.dumps(
        {
            "variables": {
                "B1": {
                    "overviews": {
                        "0": {
                            "path": object_path,
                            "levels": {
                                "12": {"path": object_path}
                            },
                        }
                    }
                }
            }
        }
    )

    overview, bbox, source = get_or_create_browse_overview(
        settings=settings,
        connector=connector,
        entry=entry,
        variable="B1",
        time_index=0,
        zoom=12,
        max_zoom_override=12,
    )

    assert source == "oci"
    assert overview.shape == (1, 1)
    assert bbox == (0.0, 0.0, 1.0, 1.0)


def test_get_or_create_browse_overview_skips_runtime_build_when_disabled(tmp_path: Path, monkeypatch) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=True,
        browse_request_build_enabled=False,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()

    def _unexpected_build(**_kwargs):
        raise AssertionError("request path should not build browse artifacts")

    monkeypatch.setattr("app.core.browse_tiles._build_overview", _unexpected_build)

    try:
        get_or_create_browse_overview(
            settings=settings,
            connector=connector,
            entry=entry,
            variable="B1",
            time_index=0,
            zoom=3,
            allow_build=settings.browse_request_build_enabled,
        )
    except FileNotFoundError as exc:
        assert "No durable browse overview is available" in str(exc)
    else:
        raise AssertionError("expected missing browse overview to fail when runtime build is disabled")


def test_build_and_store_browse_overviews_writes_objects_and_manifest(tmp_path: Path, monkeypatch) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=True,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()

    monkeypatch.setattr(
        "app.core.browse_tiles._build_overview",
        lambda **_kwargs: (np.zeros((2, 2), dtype=np.float32), (0.0, 0.0, 2.0, 2.0)),
    )

    summary = build_and_store_browse_overviews(
        settings=settings,
        connector=connector,
        entry=entry,
        variables=["B1"],
        time_indices=[0],
        zoom_levels=[2, 3],
    )

    assert summary["generated"] == 2
    assert summary["zoom_levels"] == [2, 3]
    assert connector.write_bytes_calls == [
        browse_overview_object_path(settings, entry, "B1", 0, 3),
        browse_overview_object_path(settings, entry, "B1", 0, 2),
    ]
    assert connector.write_text_calls == [browse_manifest_path(settings, entry)]
    manifest = json.loads(connector.text_payloads[browse_manifest_path(settings, entry)])
    assert manifest["variables"]["B1"]["overviews"]["0"]["path"] == browse_overview_object_path(settings, entry, "B1", 0, 2)
    assert manifest["variables"]["B1"]["overviews"]["0"]["levels"]["3"]["path"] == browse_overview_object_path(
        settings, entry, "B1", 0, 3
    )


def test_build_and_store_browse_overviews_builds_highest_zoom_once_then_derives_lower_levels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=True,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()
    built_zooms: list[int] = []

    def _build_once(**kwargs):
        zoom = kwargs["zoom"]
        built_zooms.append(zoom)
        if zoom != 3:
            raise AssertionError(f"unexpected full build for zoom {zoom}")
        return np.arange(64, dtype=np.float32).reshape(8, 8), (0.0, 0.0, 8.0, 8.0)

    monkeypatch.setattr("app.core.browse_tiles._build_overview", _build_once)

    summary = build_and_store_browse_overviews(
        settings=settings,
        connector=connector,
        entry=entry,
        variables=["B1"],
        time_indices=[0],
        zoom_levels=[1, 2, 3],
    )

    expected_paths = [
        browse_overview_object_path(settings, entry, "B1", 0, 3),
        browse_overview_object_path(settings, entry, "B1", 0, 2),
        browse_overview_object_path(settings, entry, "B1", 0, 1),
    ]
    assert built_zooms == [3]
    assert summary["generated"] == 3
    assert summary["reused"] == 0
    assert connector.write_bytes_calls == expected_paths
    for path in expected_paths:
        assert path in connector.payloads


def test_build_and_store_browse_overviews_preserves_manifest_entries_for_derived_pyramid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=True,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    connector = _FakeConnector()

    monkeypatch.setattr(
        "app.core.browse_tiles._build_overview",
        lambda **kwargs: (
            np.arange(64, dtype=np.float32).reshape(8, 8),
            (0.0, 0.0, 8.0, 8.0),
        ),
    )

    build_and_store_browse_overviews(
        settings=settings,
        connector=connector,
        entry=entry,
        variables=["B1"],
        time_indices=[0],
        zoom_levels=[1, 2, 3],
    )

    manifest_path = browse_manifest_path(settings, entry)
    manifest = json.loads(connector.text_payloads[manifest_path])
    levels = manifest["variables"]["B1"]["overviews"]["0"]["levels"]

    assert connector.write_text_calls == [manifest_path]
    assert set(levels) == {"1", "2", "3"}
    for zoom in (1, 2, 3):
        assert levels[str(zoom)]["path"] == browse_overview_object_path(settings, entry, "B1", 0, zoom)
        assert browse_manifest_contains_overview(manifest, variable="B1", time_index=0, zoom=zoom) is True
    assert manifest["variables"]["B1"]["overviews"]["0"]["path"] == browse_overview_object_path(settings, entry, "B1", 0, 1)
    assert manifest["variables"]["B1"]["overviews"]["0"]["max_zoom_path"] == browse_overview_object_path(
        settings, entry, "B1", 0, 3
    )


def test_select_mosaic_tile_range_raises_internal_zoom_when_target_zoom_is_too_coarse(monkeypatch) -> None:
    ranges = {
        8: (0, 0, 0, 1),
        9: (0, 1, 0, 2),
        10: (0, 2, 0, 4),
        11: (0, 5, 0, 8),
    }

    monkeypatch.setattr(
        "app.core.browse_tiles._intersecting_xyz_tile_range",
        lambda _bbox, zoom: ranges.get(zoom),
    )

    from app.core import browse_tiles as browse_tiles_module

    assert browse_tiles_module._select_mosaic_tile_range((0.0, 0.0, 1.0, 1.0), 8) == (11, ranges[11])


def test_select_mosaic_tile_range_returns_last_safe_zoom_before_tile_budget_is_exceeded(monkeypatch) -> None:
    ranges = {
        8: (0, 0, 0, 1),
        9: (0, 1, 0, 2),
        10: (0, 2, 0, 4),
        11: (0, 11, 0, 10),
    }

    monkeypatch.setattr(
        "app.core.browse_tiles._intersecting_xyz_tile_range",
        lambda _bbox, zoom: ranges.get(zoom),
    )

    from app.core import browse_tiles as browse_tiles_module

    assert browse_tiles_module._select_mosaic_tile_range((0.0, 0.0, 1.0, 1.0), 8) == (10, ranges[10])


def test_build_overview_fast_latlon_path_bypasses_mosaic(tmp_path: Path, monkeypatch) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
        oci_browse_prefix_root="browse",
        browse_dev_fallback_enabled=True,
        browse_tile_max_zoom=8,
    )
    entry = _entry()
    entry.meta.bounds = DatasetBounds(west=0.0, south=0.0, east=1.0, north=1.0)

    from app.core import browse_tiles as browse_tiles_module

    monkeypatch.setattr(
        "app.core.browse_tiles.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr("app.core.browse_tiles._is_fast_latlon_entry", lambda _entry: True)
    monkeypatch.setattr(
        "app.core.browse_tiles._select_mosaic_tile_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mosaic path should be skipped")),
    )

    captured: dict[str, object] = {}

    def _render(**kwargs):
        captured["max_source_oversample"] = kwargs["max_source_oversample"]
        captured["max_parallel_chunk_reads"] = kwargs["max_parallel_chunk_reads"]
        return np.ones((2, 2), dtype=np.float32)

    monkeypatch.setattr("app.core.browse_tiles.render_projected_band_array", _render)

    overview, bbox = browse_tiles_module._build_overview(
        settings=settings,
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        variable="B1",
        time_index=0,
        zoom=3,
    )

    assert overview.shape == (2, 2)
    assert bbox[0] < bbox[2]
    assert captured["max_source_oversample"] == 1.0
    assert captured["max_parallel_chunk_reads"] == 1


def _serialized_overview() -> bytes:
    payload = BytesIO()
    np.savez_compressed(payload, data=np.zeros((1, 1), dtype=np.float32), bbox=np.asarray([0.0, 0.0, 1.0, 1.0]))
    return payload.getvalue()
