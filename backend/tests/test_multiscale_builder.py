import numpy as np
from types import SimpleNamespace

from app.core.multiscale_builder import _downsample_2d_mean
from app.core.multiscale_builder import _maximum_pyramid_zoom
from app.core.multiscale_builder import _mercator_tile_range_for_bounds
from app.core.multiscale_builder import _prepopulate_pyramid_levels
from app.core.multiscale_builder import _render_prepopulated_tile
from app.core.multiscale_builder import _resolve_prepopulated_zoom_max
from app.core.multiscale_builder import _resolve_level_zero_sampling
from app.models.dataset import DatasetBounds


class _Entry:
    def __init__(self, native_resolution_m: float | None, bounds: DatasetBounds | None) -> None:
        self.meta = type("Meta", (), {"native_resolution_m": native_resolution_m, "bounds": bounds})()


def test_resolve_level_zero_sampling_defaults_to_overview_sized_level_zero() -> None:
    y_indices, x_indices, factor = _resolve_level_zero_sampling(
        height=138614,
        width=93066,
        max_browser_dimension=4096,
        full_resolution=False,
    )

    assert factor == 64
    assert len(y_indices) == 2167
    assert len(x_indices) == 1456
    assert y_indices[0] == 0
    assert x_indices[0] == 0
    assert y_indices[-1] == 138613
    assert x_indices[-1] == 93065


def test_resolve_level_zero_sampling_can_keep_full_resolution() -> None:
    y_indices, x_indices, factor = _resolve_level_zero_sampling(
        height=8,
        width=6,
        max_browser_dimension=4,
        full_resolution=True,
    )

    assert factor == 1
    assert y_indices.tolist() == list(range(8))
    assert x_indices.tolist() == list(range(6))


def test_downsample_2d_mean_handles_odd_edges() -> None:
    values = np.asarray(
        [
            [1.0, 2.0, 3.0],
            [5.0, 6.0, 7.0],
            [9.0, 10.0, 11.0],
        ],
        dtype=np.float32,
    )

    reduced = _downsample_2d_mean(values)

    assert reduced.shape == (2, 2)
    np.testing.assert_allclose(
        reduced,
        np.asarray(
            [
                [3.5, 5.0],
                [9.5, 11.0],
            ],
            dtype=np.float32,
        ),
    )


def test_maximum_pyramid_zoom_uses_native_resolution() -> None:
    entry = _Entry(
        native_resolution_m=10.0,
        bounds=DatasetBounds(west=29.9, south=-2.4, east=30.9, north=-1.0),
    )

    assert _maximum_pyramid_zoom(type("Settings", (), {"browse_tile_max_zoom": 8})(), entry) == 14


def test_maximum_pyramid_zoom_honors_explicit_override() -> None:
    entry = _Entry(
        native_resolution_m=1.1,
        bounds=DatasetBounds(west=30.39, south=-2.09, east=30.81, north=-1.05),
    )

    assert _maximum_pyramid_zoom(type("Settings", (), {"browse_tile_max_zoom": 8})(), entry, explicit_max_zoom=17) == 17


def test_mercator_tile_range_for_bounds_returns_single_world_tile_at_zoom_zero() -> None:
    bounds = DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0)

    assert _mercator_tile_range_for_bounds(bounds, 0) == (0, 0, 0, 0)


def test_resolve_prepopulated_zoom_max_uses_tile_budget() -> None:
    entry = _Entry(
        native_resolution_m=10.0,
        bounds=DatasetBounds(west=29.9, south=-2.4, east=30.9, north=-1.0),
    )

    result = _resolve_prepopulated_zoom_max(
        entry=entry,
        zoom_levels=list(range(0, 15)),
        explicit_max_zoom=None,
        tile_budget=128,
    )

    assert result == 11


def test_resolve_prepopulated_zoom_max_honors_explicit_cutoff() -> None:
    entry = _Entry(
        native_resolution_m=10.0,
        bounds=DatasetBounds(west=29.9, south=-2.4, east=30.9, north=-1.0),
    )

    result = _resolve_prepopulated_zoom_max(
        entry=entry,
        zoom_levels=list(range(0, 15)),
        explicit_max_zoom=9,
        tile_budget=128,
    )

    assert result == 9


def test_render_prepopulated_tile_uses_extended_browse_overviews(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_get_or_create_browse_overview(**kwargs):
        calls.append(kwargs)
        return np.ones((256, 256), dtype=np.float32), (0.0, 0.0, 1.0, 1.0), "oci"

    monkeypatch.setattr(
        "app.core.multiscale_builder.get_or_create_browse_overview",
        _fake_get_or_create_browse_overview,
    )
    monkeypatch.setattr(
        "app.core.multiscale_builder.sample_web_mercator_array",
        lambda *args, **kwargs: np.full((256, 256), 7.0, dtype=np.float32),
    )
    monkeypatch.setattr(
        "app.core.multiscale_builder.render_projected_band_array",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct render path should not run")),
    )

    tile = _render_prepopulated_tile(
        settings=SimpleNamespace(browse_tile_max_zoom=8),
        connector=object(),  # type: ignore[arg-type]
        entry=SimpleNamespace(id="dataset-1"),
        variable="B1",
        time_index=0,
        zoom=12,
        x=0,
        y=0,
        overview_cache={},
        browse_overview_max_zoom=12,
    )

    assert tile.shape == (256, 256)
    assert calls[0]["max_zoom_override"] == 12


def test_prepopulate_pyramid_levels_builds_extended_browse_overviews_once(monkeypatch) -> None:
    build_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.core.multiscale_builder.build_and_store_browse_overviews",
        lambda **kwargs: build_calls.append(kwargs) or {"generated": 4},
    )
    monkeypatch.setattr(
        "app.core.multiscale_builder._render_prepopulated_tile",
        lambda **kwargs: np.zeros((256, 256), dtype=np.float32),
    )

    level_arrays = {9: np.zeros((1, 1, 256, 256), dtype=np.float32)}
    tile_ranges = {"9": {"tile_x_min": 0, "tile_x_max": 0, "tile_y_min": 0, "tile_y_max": 0}}
    entry = SimpleNamespace(
        id="dataset-1",
        meta=SimpleNamespace(variables=[SimpleNamespace(id="B1")]),
        data_array_meta=SimpleNamespace(shape=(1, 1, 256, 256)),
    )

    _prepopulate_pyramid_levels(
        settings=SimpleNamespace(browse_tile_max_zoom=8),
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        level_arrays=level_arrays,
        tile_ranges=tile_ranges,
        prepopulated_zoom_max=12,
        target_dtype=np.dtype("float32"),
    )

    assert build_calls[0]["zoom_levels"] == [9, 10, 11, 12]
    assert build_calls[0]["max_zoom_override"] == 12


def test_prepopulate_pyramid_levels_uses_band_name_fallback_when_variable_meta_is_missing(monkeypatch) -> None:
    render_calls: list[str] = []

    monkeypatch.setattr(
        "app.core.multiscale_builder.build_and_store_browse_overviews",
        lambda **kwargs: {"generated": 0},
    )
    monkeypatch.setattr(
        "app.core.multiscale_builder._render_prepopulated_tile",
        lambda **kwargs: render_calls.append(str(kwargs["variable"])) or np.ones((256, 256), dtype=np.float32),
    )

    level_arrays = {9: np.zeros((1, 1, 256, 256), dtype=np.float32)}
    tile_ranges = {"9": {"tile_x_min": 0, "tile_x_max": 0, "tile_y_min": 0, "tile_y_max": 0}}
    entry = SimpleNamespace(
        id="dataset-1",
        band_names=["NDVI"],
        meta=SimpleNamespace(variables=[]),
        data_array_meta=SimpleNamespace(shape=(1, 1, 256, 256)),
    )

    _prepopulate_pyramid_levels(
        settings=SimpleNamespace(browse_tile_max_zoom=8),
        connector=object(),  # type: ignore[arg-type]
        entry=entry,
        level_arrays=level_arrays,
        tile_ranges=tile_ranges,
        prepopulated_zoom_max=9,
        target_dtype=np.dtype("float32"),
    )

    assert render_calls == ["NDVI"]
    np.testing.assert_allclose(level_arrays[9][0, 0], 1.0)
