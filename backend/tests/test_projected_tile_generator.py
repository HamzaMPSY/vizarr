import numpy as np

from app.core.projected_tile_generator import _bilinear_sample
from app.core.projected_tile_generator import _coordinate_to_fractional_index
from app.core.projected_tile_generator import _fractional_indices_from_geotransform
from app.core.projected_tile_generator import _fractional_indices_from_north_up_geotransform_axes
from app.core.projected_tile_generator import _resolve_display_range
from app.core.projected_tile_generator import _source_window_bounds_from_axis_indices
from app.core.projected_tile_generator import _source_window_bounds
from app.core.projected_tile_generator import _source_window_bounds_from_indices
from app.core.projected_tile_generator import _web_mercator_x_to_lon
from app.core.projected_tile_generator import _web_mercator_y_to_lat


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
