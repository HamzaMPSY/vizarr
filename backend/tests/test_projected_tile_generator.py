import numpy as np

from app.core.dataset_catalog import CatalogEntry
from app.core.projected_tile_generator import _bilinear_sample
from app.core.projected_tile_generator import _coordinate_to_fractional_index
from app.core.projected_tile_generator import _fractional_indices_from_geotransform
from app.core.projected_tile_generator import _fractional_indices_from_north_up_geotransform_axes
from app.core.projected_tile_generator import _resolve_display_range
from app.core.projected_tile_generator import render_projected_band_array
from app.core.projected_tile_generator import _source_window_bounds_from_axis_indices
from app.core.projected_tile_generator import _source_window_bounds
from app.core.projected_tile_generator import _source_window_bounds_from_indices
from app.core.projected_tile_generator import _web_mercator_x_to_lon
from app.core.projected_tile_generator import _web_mercator_y_to_lat
from app.core.projected_tile_generator import render_projected_composite_array
from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.models.dataset import CompositeStyle
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats


def test_coordinate_to_fractional_index_for_increasing_axis() -> None:
    axis = np.array([10.0, 20.0, 30.0], dtype=np.float64)
    coords = np.array([10.0, 15.0, 30.0], dtype=np.float64)
    indices = _coordinate_to_fractional_index(axis, coords)
    assert np.allclose(indices, np.array([0.0, 0.5, 2.0], dtype=np.float64), equal_nan=True)


def test_coordinate_to_fractional_index_for_decreasing_axis() -> None:
    axis = np.array([30.0, 20.0, 10.0], dtype=np.float64)
    coords = np.array([30.0, 15.0, 10.0], dtype=np.float64)
    indices = _coordinate_to_fractional_index(axis, coords)
    assert np.allclose(indices, np.array([0.0, 1.5, 2.0], dtype=np.float64), equal_nan=True)


def test_bilinear_sample_interpolates_surface() -> None:
    data = np.array(
        [
            [0.0, 10.0],
            [20.0, 30.0],
        ],
        dtype=np.float32,
    )
    sampled = _bilinear_sample(
        data=data,
        y_idx=np.array([[0.5]], dtype=np.float64),
        x_idx=np.array([[0.5]], dtype=np.float64),
    )
    assert np.allclose(sampled, np.array([[15.0]], dtype=np.float32), equal_nan=True)


def test_bilinear_sample_ignores_nan_neighbors() -> None:
    data = np.array(
        [
            [10.0, np.nan],
            [np.nan, np.nan],
        ],
        dtype=np.float32,
    )
    sampled = _bilinear_sample(
        data=data,
        y_idx=np.array([[0.5]], dtype=np.float64),
        x_idx=np.array([[0.5]], dtype=np.float64),
    )
    assert np.allclose(sampled, np.array([[10.0]], dtype=np.float32), equal_nan=True)


def test_fractional_indices_from_geotransform_matches_pixel_centers() -> None:
    transform = (528285.0, 30.0, 0.0, 3787515.0, 0.0, -30.0)
    xs = np.array([[528300.0]], dtype=np.float64)
    ys = np.array([[3787500.0]], dtype=np.float64)
    x_idx, y_idx = _fractional_indices_from_geotransform(transform, xs, ys)
    assert x_idx is not None
    assert y_idx is not None
    assert np.allclose(x_idx, np.array([[0.0]], dtype=np.float64), equal_nan=True)
    assert np.allclose(y_idx, np.array([[0.0]], dtype=np.float64), equal_nan=True)


def test_fractional_indices_from_north_up_geotransform_axes_matches_pixel_centers() -> None:
    transform = (29.95, 0.1, 0.0, 10.05, 0.0, -0.1)
    lon_values = np.array([30.0, 30.1], dtype=np.float64)
    lat_values = np.array([10.0, 9.9], dtype=np.float64)

    x_idx, y_idx = _fractional_indices_from_north_up_geotransform_axes(
        transform,
        lon_values=lon_values,
        lat_values=lat_values,
    )

    assert x_idx is not None
    assert y_idx is not None
    assert np.allclose(x_idx, np.array([0.0, 1.0], dtype=np.float64), equal_nan=True)
    assert np.allclose(y_idx, np.array([0.0, 1.0], dtype=np.float64), equal_nan=True)


def test_source_window_bounds_from_indices_ignores_out_of_range_samples() -> None:
    x_idx = np.array([[-20.0, 1.25, 2.75, 80.0]], dtype=np.float64)
    y_idx = np.array([[10.0, 3.5, 4.25, 99.0]], dtype=np.float64)
    bounds = _source_window_bounds_from_indices(x_idx=x_idx, y_idx=y_idx, width=8, height=8)
    assert bounds == (0, 5, 2, 7)


def test_source_window_bounds_from_axis_indices_ignores_out_of_range_samples() -> None:
    x_idx = np.array([-20.0, 1.25, 2.75, 80.0], dtype=np.float64)
    y_idx = np.array([10.0, 3.5, 4.25, 99.0], dtype=np.float64)
    bounds = _source_window_bounds_from_axis_indices(x_idx=x_idx, y_idx=y_idx, width=8, height=8)
    assert bounds == (0, 5, 2, 7)


def test_web_mercator_conversions_match_known_axes() -> None:
    xs = np.array([0.0], dtype=np.float64)
    ys = np.array([0.0], dtype=np.float64)
    assert np.allclose(_web_mercator_x_to_lon(xs), np.array([0.0], dtype=np.float64))
    assert np.allclose(_web_mercator_y_to_lat(ys), np.array([0.0], dtype=np.float64))


def test_source_window_bounds_returns_none_when_projected_points_miss_dataset() -> None:
    x_values = np.array([100.0, 110.0, 120.0], dtype=np.float64)
    y_values = np.array([220.0, 210.0, 200.0], dtype=np.float64)
    xs = np.array([[10.0, 20.0]], dtype=np.float64)
    ys = np.array([[15.0, 25.0]], dtype=np.float64)
    assert _source_window_bounds(x_values=x_values, y_values=y_values, xs=xs, ys=ys) is None


def test_resolve_display_range_prefers_dataset_defaults_when_available() -> None:
    data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    actual_vmin, actual_vmax = _resolve_display_range(
        data=data,
        fallback_vmin=10.0,
        fallback_vmax=20.0,
        vmin=None,
        vmax=None,
    )
    assert (actual_vmin, actual_vmax) == (10.0, 20.0)


def test_render_projected_composite_array_stacks_rgb_channels(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[
                VariableMeta(
                    id="B4",
                    name="B4 Red",
                    unit="DN",
                    time_steps=1,
                    stats=VariableStats(min=0.0, max=100.0, p02=0.0, p98=100.0),
                ),
                VariableMeta(
                    id="B3",
                    name="B3 Green",
                    unit="DN",
                    time_steps=1,
                    stats=VariableStats(min=0.0, max=100.0, p02=0.0, p98=100.0),
                ),
                VariableMeta(
                    id="B2",
                    name="B2 Blue",
                    unit="DN",
                    time_steps=1,
                    stats=VariableStats(min=0.0, max=100.0, p02=0.0, p98=100.0),
                ),
            ],
            composite_styles=[
                CompositeStyle(
                    id="true-color",
                    name="True Color",
                    description="Natural color",
                    bands=["B4", "B3", "B2"],
                )
            ],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["B4", "B3", "B2"],
        band_indices={"B4": 0, "B3": 1, "B2": 2},
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    channel_values = {"B4": 100.0, "B3": 50.0, "B2": 0.0}

    def _render_band(*, variable, **_kwargs):
        return np.full((2, 2), channel_values[variable], dtype=np.float32)

    monkeypatch.setattr("app.core.projected_tile_generator.render_projected_band_array", _render_band)

    composite = render_projected_composite_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        composite_id="true-color",
        bbox=(0.0, 0.0, 1.0, 1.0),
        width=2,
        height=2,
        time_index=0,
    )

    assert composite.shape == (2, 2, 3)
    assert np.all(composite[..., 0] == 255)
    assert np.all((composite[..., 1] >= 127) & (composite[..., 1] <= 128))
    assert np.all(composite[..., 2] == 0)


def test_render_projected_band_array_fast_latlon_path_skips_coordinate_axes(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(1, 1, 2, 2),
            chunk_shape=(1, 1, 2, 2),
            data_type="float32",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("time", "band", "y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 1.5, 0.0, -1.0),
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full coordinate hydration should not run")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_4d_window",
        lambda **_kwargs: np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._is_fast_latlon_entry",
        lambda _entry: True,
    )

    rendered = render_projected_band_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        bbox=(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244),
        width=2,
        height=2,
        time_index=0,
    )

    assert rendered.shape == (2, 2)


def test_render_projected_band_array_supports_direct_3d_variable(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="NDVI",
        band_array_name="",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
        variable_array_names={"NDVI": "NDVI"},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(1, 2, 2),
            chunk_shape=(1, 2, 2),
            data_type="float32",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("time", "y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 1.5, 0.0, -1.0),
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full coordinate hydration should not run")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_3d_window",
        lambda **_kwargs: np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_4d_window",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("4D loader should not be used")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._is_fast_latlon_entry",
        lambda _entry: True,
    )

    rendered = render_projected_band_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        bbox=(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244),
        width=2,
        height=2,
        time_index=0,
    )

    assert rendered.shape == (2, 2)


def test_render_projected_band_array_supports_static_2d_variable(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/static.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="static.zarr",
            description="Static dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="DEM",
        band_array_name="",
        band_names=["DEM"],
        band_indices={"DEM": 0},
        variable_array_names={"DEM": "DEM"},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(2, 2),
            chunk_shape=(2, 2),
            data_type="float32",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 1.5, 0.0, -1.0),
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full coordinate hydration should not run")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_2d_window",
        lambda **_kwargs: np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_3d_window",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("3D loader should not be used")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_4d_window",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("4D loader should not be used")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._is_fast_latlon_entry",
        lambda _entry: True,
    )

    rendered = render_projected_band_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        variable="DEM",
        bbox=(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244),
        width=2,
        height=2,
        time_index=0,
    )

    assert rendered.shape == (2, 2)


def test_render_projected_band_array_fast_latlon_uses_decimated_window_for_large_overview(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(1, 1, 8, 8),
            chunk_shape=(1, 1, 8, 8),
            data_type="float32",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("time", "band", "y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 7.5, 0.0, -1.0),
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full coordinate hydration should not run")),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._is_fast_latlon_entry",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._source_window_bounds_from_axis_indices",
        lambda **_kwargs: (0, 8, 0, 8),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator.load_4d_window",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full window load should be skipped for decimated overview reads")),
    )

    decimated_calls: list[tuple[int, int]] = []

    def _load_decimated(**kwargs):
        decimated_calls.append((kwargs["y_step"], kwargs["x_step"]))
        return (
            np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
            np.array([0.0, 7.0], dtype=np.float64),
            np.array([0.0, 7.0], dtype=np.float64),
        )

    monkeypatch.setattr("app.core.projected_tile_generator.load_4d_window_decimated", _load_decimated)
    monkeypatch.setattr(
        "app.core.projected_tile_generator._bilinear_sample",
        lambda data, y_idx, x_idx: np.zeros(y_idx.shape, dtype=np.float32) + data[0, 0],
    )

    rendered = render_projected_band_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        bbox=(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244),
        width=2,
        height=2,
        time_index=0,
        max_source_oversample=1.0,
    )

    assert decimated_calls == [(4, 4)]
    assert rendered.shape == (2, 2)


def test_render_projected_band_array_forwards_parallelism_override(monkeypatch) -> None:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(1, 1, 8, 8),
            chunk_shape=(1, 1, 8, 8),
            data_type="float32",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("time", "band", "y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 7.5, 0.0, -1.0),
    )

    monkeypatch.setattr(
        "app.core.projected_tile_generator.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._is_fast_latlon_entry",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._source_window_bounds_from_axis_indices",
        lambda **_kwargs: (0, 8, 0, 8),
    )
    monkeypatch.setattr(
        "app.core.projected_tile_generator._bilinear_sample",
        lambda data, y_idx, x_idx: np.zeros(y_idx.shape, dtype=np.float32) + data[0, 0],
    )

    captured: dict[str, int | None] = {}

    def _load_decimated(**kwargs):
        captured["max_parallel_chunk_reads"] = kwargs["max_parallel_chunk_reads"]
        return (
            np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
            np.array([0.0, 7.0], dtype=np.float64),
            np.array([0.0, 7.0], dtype=np.float64),
        )

    monkeypatch.setattr("app.core.projected_tile_generator.load_4d_window_decimated", _load_decimated)

    render_projected_band_array(
        connector=None,  # type: ignore[arg-type]
        entry=entry,
        variable="NDVI",
        bbox=(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244),
        width=2,
        height=2,
        time_index=0,
        max_source_oversample=1.0,
        max_parallel_chunk_reads=1,
    )

    assert captured["max_parallel_chunk_reads"] == 1
