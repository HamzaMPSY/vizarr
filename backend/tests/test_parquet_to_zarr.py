from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from app.tools.parquet_to_zarr import ConversionConfig
from app.tools.parquet_to_zarr import ExistingStoreContext
from app.tools.parquet_to_zarr import _build_ingest_summary
from app.tools.parquet_to_zarr import _build_input_time_slices
from app.tools.parquet_to_zarr import _build_regular_axis
from app.tools.parquet_to_zarr import _build_to_zarr_kwargs
from app.tools.parquet_to_zarr import _build_grid_array
from app.tools.parquet_to_zarr import _build_spatial_ref_attrs
from app.tools.parquet_to_zarr import _coerce_numeric_series
from app.tools.parquet_to_zarr import _detect_value_columns
from app.tools.parquet_to_zarr import _encode_value_column
from app.tools.parquet_to_zarr import _extract_existing_value_columns
from app.tools.parquet_to_zarr import _extract_timestamps
from app.tools.parquet_to_zarr import _initial_read_columns
from app.tools.parquet_to_zarr import _is_transient_read_error
from app.tools.parquet_to_zarr import _read_table_with_retries
from app.tools.parquet_to_zarr import _grid_dataset
from app.tools.parquet_to_zarr import _minimum_positive_step
from app.tools.parquet_to_zarr import _prepare_spatial_frame
from app.tools.parquet_to_zarr import _resolve_point_preserving_resolutions
from app.tools.parquet_to_zarr import _resolve_snap_origins
from app.tools.parquet_to_zarr import _resolve_target_grid
from app.tools.parquet_to_zarr import _regular_step
from app.tools.parquet_to_zarr import _required_columns
from app.tools.parquet_to_zarr import _snap_to_resolution
from app.tools.parquet_to_zarr import _slice_frame_for_timestamp
from app.tools.parquet_to_zarr import _validate_storage_layout
from app.tools.parquet_to_zarr import _validate_append_frames_fit_existing_grid
from app.tools.parquet_to_zarr import _validate_expected_columns_against_existing


def test_regular_step_detects_even_spacing() -> None:
    values = np.array([10.0, 20.0, 30.0], dtype=np.float64)
    assert _regular_step(values) == 10.0


def test_minimum_positive_step_ignores_duplicate_values() -> None:
    values = np.array([10.0, 10.0, 10.05, 10.07], dtype=np.float64)
    assert _minimum_positive_step(values) == pytest.approx(0.02)


def test_minimum_positive_step_ignores_sub_precision_jitter() -> None:
    values = np.array([10.0, 10.0 + 1e-13, 10.05], dtype=np.float64)
    assert _minimum_positive_step(values) == pytest.approx(0.05)


def test_validate_storage_layout_rejects_non_multiple_shard_size() -> None:
    with pytest.raises(ValueError, match="integer multiple"):
        _validate_storage_layout(chunk_size=256, shard_size=3000, zarr_version=3)


def test_detect_value_columns_skips_coordinate_and_partition_fields() -> None:
    frame = pd.DataFrame(
        {
            "x": [0.0],
            "y": [1.0],
            "year": [2025],
            "month": [12],
            "signal": [4.0],
        }
    )
    config = ConversionConfig(
        x_column="x",
        y_column="y",
        value_columns=None,
        layout="bands",
        timestamp_column=None,
        timestamp_regex=r"ts=(\d{4}-\d{2}-\d{2})",
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )
    assert _detect_value_columns(frame, config) == ("signal",)


def test_coerce_numeric_series_accepts_decimal_objects() -> None:
    series = pd.Series([Decimal("0.1"), Decimal("0.2"), None], dtype=object)

    coerced = _coerce_numeric_series(series, "NDVI")

    assert coerced is not None
    assert pd.api.types.is_float_dtype(coerced)
    assert coerced.tolist()[:2] == pytest.approx([0.1, 0.2])


def test_encode_value_column_treats_decimal_objects_as_numeric() -> None:
    frame = pd.DataFrame({"NDVI": [Decimal("0.1"), Decimal("0.2"), None]}, dtype=object)

    encoded, dtype, attrs = _encode_value_column(frame, "NDVI", "float32")

    assert dtype == "float32"
    assert attrs == {}
    np.testing.assert_allclose(encoded[:2], np.array([0.1, 0.2], dtype=np.float32))
    assert np.isnan(encoded[2])


def test_is_transient_read_error_detects_remote_disconnect() -> None:
    error = ConnectionError("Remote end closed connection without response")

    assert _is_transient_read_error(error) is True


def test_read_table_with_retries_retries_transient_failure(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.parquet_to_zarr.time.sleep", lambda *_args, **_kwargs: None)
    attempts = {"count": 0}

    def flaky_read():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("Connection aborted.")
        return "ok"

    result = _read_table_with_retries(flaky_read, "oci://Ayoub@test/maize.parquet")

    assert result == "ok"
    assert attempts["count"] == 2


def test_build_spatial_ref_attrs_includes_geotransform() -> None:
    attrs = _build_spatial_ref_attrs(
        x_values=np.array([100.0, 130.0], dtype=np.float64),
        y_values=np.array([500.0, 470.0], dtype=np.float64),
        crs_value="EPSG:32629",
    )
    assert attrs["GeoTransform"] == "85.0 30.0 0.0 515.0 0.0 -30.0"
    assert "crs_wkt" in attrs


def test_required_columns_include_coords_timestamp_and_explicit_values() -> None:
    config = ConversionConfig(
        x_column="lon",
        y_column="lat",
        value_columns=("band_1", "band_2"),
        layout="bands",
        timestamp_column="acquired_at",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )
    assert _required_columns(config) == ["lon", "lat", "acquired_at", "band_1", "band_2"]


def test_initial_read_columns_skips_full_scan_when_values_are_explicit() -> None:
    config = ConversionConfig(
        x_column="lon",
        y_column="lat",
        value_columns=("band_1", "band_2"),
        layout="bands",
        timestamp_column="acquired_at",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    assert _initial_read_columns(config) == ["lon", "lat", "acquired_at", "band_1", "band_2"]


def test_extract_timestamps_returns_multiple_sorted_values_from_column() -> None:
    frame = pd.DataFrame(
        {
            "START_DATE": ["2025-01-22", "2025-01-15", "2025-01-22", "2025-01-08"],
        }
    )
    config = ConversionConfig(
        x_column="x",
        y_column="y",
        value_columns=("value",),
        layout="bands",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    timestamps = _extract_timestamps(frame, "oci://Ayoub@test/maize.parquet", config)

    assert timestamps == [
        np.datetime64("2025-01-08T00:00:00.000000000"),
        np.datetime64("2025-01-15T00:00:00.000000000"),
        np.datetime64("2025-01-22T00:00:00.000000000"),
    ]


def test_slice_frame_for_timestamp_filters_single_time_slice() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.0, 20.0, 30.0],
            "LATITUDE": [50.0, 40.0, 30.0],
            "START_DATE": ["2025-01-08", "2025-01-15", "2025-01-08"],
            "NDVI": [0.1, 0.2, 0.3],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="bands",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    sliced = _slice_frame_for_timestamp(frame, config, np.datetime64("2025-01-08T00:00:00.000000000"))

    assert sliced["START_DATE"].tolist() == ["2025-01-08", "2025-01-08"]
    assert sliced["NDVI"].tolist() == [0.1, 0.3]


def test_build_input_time_slices_expands_single_parquet_with_multiple_timestamps() -> None:
    parquet_uri = "oci://Ayoub@test/maize.parquet"
    coordinate_frames = {
        parquet_uri: pd.DataFrame(
            {
                "LONGITUDE": [10.0, 20.0, 10.0],
                "LATITUDE": [50.0, 40.0, 30.0],
                "START_DATE": ["2025-01-08", "2025-01-15", "2025-01-08"],
            }
        )
    }
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="bands",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    slices = _build_input_time_slices([parquet_uri], coordinate_frames, config)

    assert [(item.source_uri, item.timestamp) for item in slices] == [
        (parquet_uri, np.datetime64("2025-01-08T00:00:00.000000000")),
        (parquet_uri, np.datetime64("2025-01-15T00:00:00.000000000")),
    ]


def test_extract_existing_value_columns_reads_band_labels_attr() -> None:
    dataset = xr.Dataset(
        {
            "bands": (("time", "band", "y", "x"), np.ones((1, 1, 1, 1), dtype=np.float32)),
        }
    )
    dataset["bands"].attrs["band_labels"] = ["NDVI"]

    value_columns, layout = _extract_existing_value_columns(dataset)

    assert value_columns == ("NDVI",)
    assert layout == "bands"


def test_validate_expected_columns_against_existing_rejects_mismatch() -> None:
    with pytest.raises(ValueError, match="do not match existing store"):
        _validate_expected_columns_against_existing(
            expected_columns=("NDVI",),
            existing_columns=("EVI",),
            output_store_uri="oci://Ayoub@test/cubes/maize.zarr",
        )


def test_validate_append_frames_fit_existing_grid_rejects_out_of_grid_points() -> None:
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="bands",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs="EPSG:4326",
        max_grid_cells=1_000,
        x_resolution=0.5,
        y_resolution=0.5,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )
    coordinate_frames = {
        "oci://Ayoub@test/maize.parquet": pd.DataFrame(
            {
                "LONGITUDE": [10.0, 11.5],
                "LATITUDE": [20.0, 19.0],
                "START_DATE": ["2025-01-08", "2025-01-08"],
            }
        )
    }
    existing_context = ExistingStoreContext(
        x_values=np.array([10.0, 10.5, 11.0], dtype=np.float64),
        y_values=np.array([20.0, 19.5], dtype=np.float64),
        time_values=np.array([np.datetime64("2025-01-01")], dtype="datetime64[ns]"),
        value_columns=("NDVI",),
        layout="bands",
    )

    with pytest.raises(ValueError, match="does not fit the existing target grid"):
        _validate_append_frames_fit_existing_grid(
            coordinate_frames=coordinate_frames,
            parquet_uris=["oci://Ayoub@test/maize.parquet"],
            config=config,
            existing_context=existing_context,
            output_store_uri="oci://Ayoub@test/cubes/maize.zarr",
        )


def test_snapped_coordinates_match_shared_regular_axis_exactly() -> None:
    origin = 0.00004999499
    resolution = 0.0001
    raw = pd.Series(
        [
            12.300049994991,
            12.300149994989,
            12.300249994992,
        ],
        dtype=np.float64,
    )

    snapped = _snap_to_resolution(raw, resolution=resolution, origin=origin).to_numpy(dtype=np.float64)
    axis = _build_regular_axis(
        minimum=float(snapped.min()),
        maximum=float(snapped.max()),
        resolution=resolution,
        descending=False,
    )

    positions = pd.Index(axis).get_indexer(snapped)
    assert positions.tolist() == [0, 1, 2]


def test_build_grid_array_places_values_by_coordinate_index() -> None:
    frame = pd.DataFrame(
        {
            "x": [20.0, 10.0, 20.0, 10.0],
            "y": [40.0, 50.0, 50.0, 40.0],
            "value": [4.0, 1.0, 2.0, 3.0],
        }
    )

    result = _build_grid_array(
        df=frame,
        x_col="x",
        y_col="y",
        values=frame["value"].to_numpy(dtype="float32"),
        x_values=np.array([10.0, 20.0], dtype=np.float64),
        y_values=np.array([50.0, 40.0], dtype=np.float64),
        dtype="float32",
    )

    assert result.shape == (1, 2, 2)
    assert result[0].tolist() == [[1.0, 2.0], [3.0, 4.0]]


def test_build_ingest_summary_reports_source_and_aggregated_rows() -> None:
    summary = _build_ingest_summary(
        parquet_uri="oci://Ayoub@test/maize.parquet",
        timestamp=np.datetime64("2025-01-15T00:00:00.000000000"),
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=0.5,
            y_resolution=0.5,
            cell_aggregation="mean",
            string_cell_aggregation="first",
        ),
        value_columns=("NDVI",),
        x_values=np.array([10.0, 10.5], dtype=np.float64),
        y_values=np.array([20.5, 20.0], dtype=np.float64),
        input_rows=4,
        output_rows=2,
    )

    assert summary["source"] == "oci://Ayoub@test/maize.parquet"
    assert summary["variables"] == ["bands"]
    assert summary["value_columns"] == ["NDVI"]
    assert summary["input_rows"] == 4
    assert summary["output_rows"] == 2
    assert summary["aggregation_ratio"] == 0.5
    assert summary["x_resolution"] == 0.5
    assert summary["y_resolution"] == 0.5
    assert summary["preserve_points"] is False
    assert summary["shape"] == {"time": 1, "y": 2, "x": 2}


def test_grid_dataset_builds_time_slice_with_spatial_metadata() -> None:
    frame = pd.DataFrame(
        {
            "x": [10.0, 20.0, 10.0, 20.0],
            "y": [50.0, 50.0, 40.0, 40.0],
            "value": [1.0, 2.0, 3.0, 4.0],
        }
    )
    config = ConversionConfig(
        x_column="x",
        y_column="y",
        value_columns=("value",),
        layout="per-variable",
        timestamp_column=None,
        timestamp_regex=r"ts=(\d{4}-\d{2}-\d{2})",
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs="EPSG:4326",
        max_grid_cells=1_000,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    dataset = _grid_dataset(
        frame,
        parquet_uri="oci://STAY@test/cubes/parquet/ts=2026-01-17",
        config=config,
        expected_x=None,
        expected_y=None,
    )

    assert dataset["value"].shape == (1, 2, 2)
    assert dataset["time"].values[0] == np.datetime64("2026-01-17T00:00:00.000000000")
    assert dataset["value"].values[0].tolist() == [[1.0, 2.0], [3.0, 4.0]]
    assert dataset.attrs["source_input_rows"] == 4
    assert dataset.attrs["source_output_rows"] == 4
    assert dataset["spatial_ref"].attrs["GeoTransform"] == "5.0 10.0 0.0 55.0 0.0 -10.0"


def test_grid_dataset_fails_fast_for_sparse_point_cloud_shape() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.0, 20.0, 30.0],
            "LATITUDE": [50.0, 40.0, 30.0],
            "QUADKEY": ["1", "2", "3"],
            "NDVI": [0.1, 0.2, 0.3],
            "START_DATE": ["2025-01-15", "2025-01-15", "2025-01-15"],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="bands",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=8,
        x_resolution=None,
        y_resolution=None,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    with pytest.raises(ValueError, match="Dense grid too large"):
        _grid_dataset(
            frame,
            parquet_uri="oci://Ayoub@test/maize.parquet",
            config=config,
            expected_x=None,
            expected_y=None,
        )


def test_prepare_spatial_frame_snaps_and_aggregates_duplicate_cells() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.01, 10.04, 10.49],
            "LATITUDE": [20.01, 20.02, 20.51],
            "NDVI": [0.2, 0.4, 0.6],
            "FINAL_PREDICTION": [1.0, 3.0, 5.0],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI", "FINAL_PREDICTION"),
        layout="bands",
        timestamp_column=None,
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=0.5,
        y_resolution=0.5,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    prepared = _prepare_spatial_frame(frame, config, config.value_columns or ())

    assert len(prepared.index) == 2
    assert prepared["LONGITUDE"].tolist() == [10.0, 10.5]
    assert prepared["LATITUDE"].tolist() == [20.0, 20.5]
    assert prepared["NDVI"].tolist() == pytest.approx([0.3, 0.6])
    assert prepared["FINAL_PREDICTION"].tolist() == pytest.approx([2.0, 5.0])


def test_prepare_coordinate_frame_deduplicates_without_value_columns() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.01, 10.04, 10.49],
            "LATITUDE": [20.01, 20.02, 20.51],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="bands",
        timestamp_column=None,
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=0.5,
        y_resolution=0.5,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    prepared = _prepare_spatial_frame(frame, config, ())

    assert len(prepared.index) == 2
    assert prepared["LONGITUDE"].tolist() == [10.0, 10.5]
    assert prepared["LATITUDE"].tolist() == [20.0, 20.5]


def test_grid_dataset_builds_snapped_cube_from_point_rows() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.01, 10.04, 10.49, 10.51],
            "LATITUDE": [20.01, 20.02, 20.49, 20.51],
            "START_DATE": ["2025-01-15"] * 4,
            "NDVI": [0.2, 0.4, 0.6, 0.8],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("NDVI",),
        layout="per-variable",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs="EPSG:4326",
        max_grid_cells=1_000,
        x_resolution=0.5,
        y_resolution=0.5,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    dataset = _grid_dataset(
        frame,
        parquet_uri="oci://Ayoub@test/maize.parquet",
        config=config,
        expected_x=None,
        expected_y=None,
    )

    assert dataset["x"].values.tolist() == [10.0, 10.5]
    assert dataset["y"].values.tolist() == [20.5, 20.0]
    np.testing.assert_allclose(
        dataset["NDVI"].values[0],
        np.array([[np.nan, 0.7], [0.3, np.nan]], dtype=np.float32),
        equal_nan=True,
    )
    assert dataset.attrs["source_input_rows"] == 4
    assert dataset.attrs["source_output_rows"] == 2


def test_grid_dataset_encodes_string_value_columns() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.01, 10.04, 10.49],
            "LATITUDE": [20.01, 20.02, 20.49],
            "START_DATE": ["2025-01-15"] * 3,
            "LABEL": ["corn", "corn", "soy"],
        }
    )
    config = ConversionConfig(
        x_column="LONGITUDE",
        y_column="LATITUDE",
        value_columns=("LABEL",),
        layout="per-variable",
        timestamp_column="START_DATE",
        timestamp_regex=None,
        x_dim="x",
        y_dim="y",
        y_descending=True,
        dtype="float32",
        crs=None,
        max_grid_cells=1_000,
        x_resolution=0.5,
        y_resolution=0.5,
        cell_aggregation="mean",
        string_cell_aggregation="first",
    )

    dataset = _grid_dataset(
        frame,
        parquet_uri="oci://Ayoub@test/maize.parquet",
        config=config,
        expected_x=None,
        expected_y=None,
    )

    assert dataset["LABEL"].attrs["categorical_encoding"] == {"0": "corn", "1": "soy"}
    assert dataset["LABEL"].attrs["_FillValue"] == -1
    np.testing.assert_allclose(
        dataset["LABEL"].values[0],
        np.array([[np.nan, 1.0], [0.0, np.nan]], dtype=np.float32),
        equal_nan=True,
    )


def test_build_to_zarr_kwargs_defaults_to_v3_shape() -> None:
    dataset = _grid_dataset(
        pd.DataFrame(
            {
                "x": [10.0, 20.0, 10.0, 20.0],
                "y": [50.0, 50.0, 40.0, 40.0],
                "value": [1.0, 2.0, 3.0, 4.0],
            }
        ),
        parquet_uri="oci://STAY@test/cubes/parquet/ts=2026-01-17",
        config=ConversionConfig(
            x_column="x",
            y_column="y",
            value_columns=("value",),
            layout="bands",
            timestamp_column=None,
            timestamp_regex=r"ts=(\d{4}-\d{2}-\d{2})",
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=None,
            y_resolution=None,
            cell_aggregation="mean",
            string_cell_aggregation="first",
        ),
        expected_x=None,
        expected_y=None,
    )

    kwargs = _build_to_zarr_kwargs(
        dataset=dataset,
        mode="w",
        append_dim=None,
        chunk_size=256,
        shard_size=4096,
        zarr_version=3,
    )

    assert kwargs["zarr_version"] == 3
    assert kwargs["zarr_format"] == 3
    assert kwargs["consolidated"] is True
    assert kwargs["encoding"]["bands"]["chunks"] == (1, 1, 2, 2)
    assert kwargs["encoding"]["bands"]["shards"] == (1, 1, 4096, 4096)


def test_build_to_zarr_kwargs_preserves_v2_consolidation() -> None:
    dataset = xr.Dataset({"value": (("time", "y", "x"), np.ones((1, 2, 2), dtype=np.float32))})
    kwargs = _build_to_zarr_kwargs(
        dataset=dataset,
        mode="a",
        append_dim="time",
        chunk_size=256,
        shard_size=4096,
        zarr_version=2,
    )

    assert kwargs["zarr_version"] == 2
    assert kwargs["zarr_format"] == 2
    assert kwargs["consolidated"] is True


def test_grid_dataset_builds_viewer_compatible_bands_cube() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.0, 20.0, 10.0, 20.0],
            "LATITUDE": [50.0, 50.0, 40.0, 40.0],
            "START_DATE": ["2025-01-15"] * 4,
            "NDVI": [0.1, 0.2, 0.3, 0.4],
            "FINAL_PREDICTION": [1.0, 2.0, 3.0, 4.0],
        }
    )
    dataset = _grid_dataset(
        frame,
        parquet_uri="oci://Ayoub@test/maize.parquet",
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI", "FINAL_PREDICTION"),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=None,
            y_resolution=None,
            cell_aggregation="mean",
            string_cell_aggregation="first",
        ),
        expected_x=None,
        expected_y=None,
    )

    assert dataset["bands"].shape == (1, 2, 2, 2)
    assert dataset["band"].values.tolist() == ["NDVI", "FINAL_PREDICTION"]
    np.testing.assert_allclose(
        dataset["bands"].values[0, 0],
        np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32),
    )
    np.testing.assert_allclose(
        dataset["bands"].values[0, 1],
        np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
    )
    assert dataset["bands"].attrs["grid_mapping"] == "spatial_ref"


def test_build_to_zarr_kwargs_chunks_4d_bands_layout() -> None:
    dataset = xr.Dataset(
        {
            "bands": (("time", "band", "y", "x"), np.ones((1, 2, 600, 700), dtype=np.float32)),
        },
        coords={
            "time": np.array([np.datetime64("2025-01-15")]),
            "band": np.array(["NDVI", "FINAL_PREDICTION"], dtype=str),
            "y": np.arange(600, dtype=np.float64),
            "x": np.arange(700, dtype=np.float64),
        },
    )

    kwargs = _build_to_zarr_kwargs(
        dataset=dataset,
        mode="w",
        append_dim=None,
        chunk_size=256,
        shard_size=4096,
        zarr_version=3,
    )

    assert kwargs["encoding"]["bands"]["chunks"] == (1, 1, 256, 256)
    assert kwargs["encoding"]["bands"]["shards"] == (1, 1, 4096, 4096)


def test_grid_dataset_reindexes_to_shared_target_grid() -> None:
    frame = pd.DataFrame(
        {
            "LONGITUDE": [10.0, 11.0],
            "LATITUDE": [50.0, 49.0],
            "START_DATE": ["2025-01-22"] * 2,
            "NDVI": [0.2, 0.8],
        }
    )
    dataset = _grid_dataset(
        frame,
        parquet_uri="oci://Ayoub@test/maize_2.parquet",
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=1.0,
            y_resolution=1.0,
            cell_aggregation="mean",
            string_cell_aggregation="first",
        ),
        expected_x=np.array([10.0, 11.0, 12.0], dtype=np.float64),
        expected_y=np.array([50.0, 49.0, 48.0], dtype=np.float64),
    )

    assert dataset["x"].values.tolist() == [10.0, 11.0, 12.0]
    assert dataset["y"].values.tolist() == [50.0, 49.0, 48.0]
    assert dataset["bands"].shape == (1, 1, 3, 3)
    np.testing.assert_allclose(
        dataset["bands"].values[0, 0],
        np.array(
            [
                [0.2, np.nan, np.nan],
                [np.nan, 0.8, np.nan],
                [np.nan, np.nan, np.nan],
            ],
            dtype=np.float32,
        ),
        equal_nan=True,
    )


def test_resolve_target_grid_uses_global_resolution_extent(monkeypatch) -> None:
    first_frame = pd.DataFrame(
        {
            "LONGITUDE": [10.01, 10.49],
            "LATITUDE": [20.01, 20.49],
        }
    )
    second_frame = pd.DataFrame(
        {
            "LONGITUDE": [10.99, 11.51],
            "LATITUDE": [19.01, 19.49],
        }
    )

    coordinate_frames = {
        "oci://Ayoub@test/first.parquet": first_frame,
        "oci://Ayoub@test/second.parquet": second_frame,
    }
    target_x, target_y = _resolve_target_grid(
        parquet_uris=[
            "oci://Ayoub@test/first.parquet",
            "oci://Ayoub@test/second.parquet",
        ],
        coordinate_frames=coordinate_frames,
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=0.5,
            y_resolution=0.5,
            cell_aggregation="mean",
            string_cell_aggregation="first",
        ),
    )

    assert target_x.tolist() == [10.0, 10.5, 11.0, 11.5]
    assert target_y.tolist() == [20.5, 20.0, 19.5, 19.0]


def test_resolve_point_preserving_resolutions_clamps_coarse_grid() -> None:
    coordinate_frames = {
        "oci://Ayoub@test/first.parquet": pd.DataFrame(
            {
                "LONGITUDE": [10.00, 10.05, 10.10],
                "LATITUDE": [20.00, 20.02, 20.04],
            }
        ),
        "oci://Ayoub@test/second.parquet": pd.DataFrame(
            {
                "LONGITUDE": [10.15, 10.20],
                "LATITUDE": [20.06, 20.08],
            }
        ),
    }

    x_resolution, y_resolution = _resolve_point_preserving_resolutions(
        coordinate_frames=coordinate_frames,
        parquet_uris=[
            "oci://Ayoub@test/first.parquet",
            "oci://Ayoub@test/second.parquet",
        ],
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=0.1,
            y_resolution=0.1,
            cell_aggregation="mean",
            string_cell_aggregation="first",
            preserve_points=True,
        ),
    )

    assert x_resolution == pytest.approx(0.05)
    assert y_resolution == pytest.approx(0.02)


def test_resolve_point_preserving_resolutions_infers_missing_grid() -> None:
    coordinate_frames = {
        "oci://Ayoub@test/first.parquet": pd.DataFrame(
            {
                "LONGITUDE": [10.00, 10.05, 10.10],
                "LATITUDE": [20.00, 20.02, 20.04],
            }
        ),
    }

    x_resolution, y_resolution = _resolve_point_preserving_resolutions(
        coordinate_frames=coordinate_frames,
        parquet_uris=["oci://Ayoub@test/first.parquet"],
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:4326",
            max_grid_cells=1_000,
            x_resolution=None,
            y_resolution=None,
            cell_aggregation="mean",
            string_cell_aggregation="first",
            preserve_points=True,
        ),
    )

    assert x_resolution == pytest.approx(0.05)
    assert y_resolution == pytest.approx(0.02)


def test_resolve_snap_origins_projects_and_infers_shared_10m_grid() -> None:
    coordinate_frames = {
        "oci://Ayoub@test/first.parquet": pd.DataFrame(
            {
                "LONGITUDE": [30.0000449, 30.0001348],
                "LATITUDE": [-2.0000452, -2.0001356],
            }
        ),
        "oci://Ayoub@test/second.parquet": pd.DataFrame(
            {
                "LONGITUDE": [30.0002247, 30.0003145],
                "LATITUDE": [-2.0002259, -2.0003163],
            }
        ),
    }

    x_origin, y_origin = _resolve_snap_origins(
        coordinate_frames=coordinate_frames,
        parquet_uris=[
            "oci://Ayoub@test/first.parquet",
            "oci://Ayoub@test/second.parquet",
        ],
        config=ConversionConfig(
            x_column="LONGITUDE",
            y_column="LATITUDE",
            value_columns=("NDVI",),
            layout="bands",
            timestamp_column="START_DATE",
            timestamp_regex=None,
            x_dim="x",
            y_dim="y",
            y_descending=True,
            dtype="float32",
            crs="EPSG:32736",
            max_grid_cells=1_000_000,
            x_resolution=10.0,
            y_resolution=10.0,
            cell_aggregation="mean",
            string_cell_aggregation="first",
            source_crs="EPSG:4326",
        ),
    )

    assert x_origin is not None
    assert y_origin is not None
    assert 0.0 <= x_origin < 10.0
    assert 0.0 <= y_origin < 10.0
