from types import SimpleNamespace

import numpy as np

from app.core.dataset_catalog import CatalogEntry
from app.core.tilejson import build_dataset_tilejson
from app.models.dataset import DatasetBounds
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats


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
                    id="NDVI",
                    name="NDVI",
                    unit="1",
                    time_steps=4,
                    stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
                )
            ],
            bounds=DatasetBounds(west=30.39, south=-2.08, east=30.81, north=-1.04),
            native_resolution_m=1.1,
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
    )


def test_build_dataset_tilejson_requires_zoom_in_when_browse_is_too_sparse(monkeypatch) -> None:
    entry = _entry()
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.tilejson.ensure_catalog_entry_ready", lambda current_entry, _connector: current_entry)
    monkeypatch.setattr(
        "app.core.tilejson.build_dataset_serving_profile",
        lambda *_args, **_kwargs: SimpleNamespace(
            browse_overview_max_zoom=8,
            multiscale_max_zoom=17,
        ),
    )
    sparse = np.full((64, 64), np.nan, dtype=np.float32)
    sparse[10, 10] = 0.5
    monkeypatch.setattr(
        "app.core.tilejson.get_or_create_browse_overview",
        lambda **_kwargs: (sparse, (0.0, 0.0, 1.0, 1.0), "local"),
    )

    tilejson = build_dataset_tilejson(
        settings,
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        time_index=0,
        tile_template="http://example.test/tiles/{z}/{x}/{y}",
    )

    assert tilejson.minzoom == 9
    assert tilejson.detail_minzoom == 9
    assert tilejson.maxzoom == 19
    assert tilejson.has_coarse_fallback is False
    assert tilejson.coarse_representation is None


def test_build_dataset_tilejson_uses_browse_as_optional_coarse_fallback(monkeypatch) -> None:
    entry = _entry()
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.tilejson.ensure_catalog_entry_ready", lambda current_entry, _connector: current_entry)
    monkeypatch.setattr(
        "app.core.tilejson.build_dataset_serving_profile",
        lambda *_args, **_kwargs: SimpleNamespace(
            browse_overview_max_zoom=8,
            multiscale_max_zoom=17,
        ),
    )
    dense = np.full((64, 64), 0.5, dtype=np.float32)
    monkeypatch.setattr(
        "app.core.tilejson.get_or_create_browse_overview",
        lambda **_kwargs: (dense, (0.0, 0.0, 1.0, 1.0), "local"),
    )

    tilejson = build_dataset_tilejson(
        settings,
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        time_index=0,
        tile_template="http://example.test/tiles/{z}/{x}/{y}",
    )

    assert tilejson.minzoom == 0
    assert tilejson.detail_minzoom == 9
    assert tilejson.maxzoom == 19
    assert tilejson.has_coarse_fallback is True
    assert tilejson.coarse_representation == "browse"


def test_build_dataset_tilejson_prefers_time_specific_bounds(monkeypatch) -> None:
    entry = _entry()
    settings = SimpleNamespace(browse_tile_max_zoom=8)
    tighter_bounds = DatasetBounds(west=30.62, south=-2.43, east=30.89, north=-1.62)

    monkeypatch.setattr("app.core.tilejson.ensure_catalog_entry_ready", lambda current_entry, _connector: current_entry)
    monkeypatch.setattr(
        "app.core.tilejson.build_dataset_serving_profile",
        lambda *_args, **_kwargs: SimpleNamespace(
            browse_overview_max_zoom=8,
            multiscale_max_zoom=17,
        ),
    )
    monkeypatch.setattr("app.core.tilejson._time_specific_bounds", lambda **_kwargs: tighter_bounds)
    monkeypatch.setattr(
        "app.core.tilejson.get_or_create_browse_overview",
        lambda **_kwargs: (np.full((64, 64), np.nan, dtype=np.float32), (0.0, 0.0, 1.0, 1.0), "local"),
    )

    tilejson = build_dataset_tilejson(
        settings,
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        time_index=0,
        tile_template="http://example.test/tiles/{z}/{x}/{y}",
    )

    assert tilejson.bounds == [30.62, -2.43, 30.89, -1.62]
