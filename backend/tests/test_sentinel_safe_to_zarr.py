from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from app.tools.sentinel_safe_to_zarr import SentinelBandObject
from app.tools.sentinel_safe_to_zarr import _build_oci_uri
from app.tools.sentinel_safe_to_zarr import _coords_from_transform
from app.tools.sentinel_safe_to_zarr import _derive_output_store
from app.tools.sentinel_safe_to_zarr import _discover_safe_band_objects
from app.tools.sentinel_safe_to_zarr import _geotransform_from_transform
from app.tools.sentinel_safe_to_zarr import _normalize_output_store_uri
from app.tools.sentinel_safe_to_zarr import _parse_bands
from app.tools.sentinel_safe_to_zarr import _parse_safe_timestamp
from app.tools.sentinel_safe_to_zarr import _validate_storage_layout


@dataclass(frozen=True)
class DummyTransform:
    a: float
    b: float
    c: float
    d: float
    e: float
    f: float


def test_parse_safe_timestamp_reads_sensing_time() -> None:
    timestamp = _parse_safe_timestamp(
        "S2B_MSIL1C_20250617T112109_N0511_R037_T29SNS_20250617T131418.SAFE/"
    )

    assert timestamp == np.datetime64("2025-06-17T11:21:09", "ns")


def test_derive_output_store_uses_safe_product_name() -> None:
    output = _derive_output_store(
        "S2B_MSIL1C_20250617T112109_N0511_R037_T29SNS_20250617T131418.SAFE/"
    )

    assert output == "cubes/S2B_MSIL1C_20250617T112109_N0511_R037_T29SNS_20250617T131418.zarr"


def test_parse_bands_normalizes_and_rejects_duplicates() -> None:
    assert _parse_bands("b02, B03,B8A") == ("B02", "B03", "B8A")

    with pytest.raises(ValueError, match="Duplicate"):
        _parse_bands("B02,B02")


def test_discover_safe_band_objects_selects_requested_l1c_bands_in_order() -> None:
    objects = [
        "S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B04.jp2",
        "S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B02.jp2",
        "S2.SAFE/GRANULE/L1C_T29SNS/QI_DATA/MSK.jp2",
        "S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B08.jp2",
    ]

    selected = _discover_safe_band_objects(objects, bands=("B02", "B04", "B08"), resolution_m=10)

    assert selected == (
        SentinelBandObject(
            band="B02",
            object_name="S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B02.jp2",
            resolution_m=None,
        ),
        SentinelBandObject(
            band="B04",
            object_name="S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B04.jp2",
            resolution_m=None,
        ),
        SentinelBandObject(
            band="B08",
            object_name="S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B08.jp2",
            resolution_m=None,
        ),
    )


def test_discover_safe_band_objects_prefers_matching_l2a_resolution() -> None:
    objects = [
        "S2.SAFE/GRANULE/L2A_T29SNS/IMG_DATA/R20m/T29SNS_20250617T112109_B02_20m.jp2",
        "S2.SAFE/GRANULE/L2A_T29SNS/IMG_DATA/R10m/T29SNS_20250617T112109_B02_10m.jp2",
    ]

    selected = _discover_safe_band_objects(objects, bands=("B02",), resolution_m=10)

    assert selected[0].object_name.endswith("_B02_10m.jp2")
    assert selected[0].resolution_m == 10


def test_discover_safe_band_objects_raises_for_missing_band() -> None:
    with pytest.raises(ValueError, match="B08"):
        _discover_safe_band_objects(
            ["S2.SAFE/GRANULE/L1C_T29SNS/IMG_DATA/T29SNS_20250617T112109_B04.jp2"],
            bands=("B04", "B08"),
            resolution_m=10,
        )


def test_output_uri_helpers_support_explicit_and_relative_destinations() -> None:
    assert _build_oci_uri("Ayoub", "lrdwfp6kyp5x", "cubes/example.zarr") == (
        "oci://Ayoub@lrdwfp6kyp5x/cubes/example.zarr"
    )
    assert _normalize_output_store_uri(
        "cubes/example.zarr",
        output_bucket="Ayoub",
        output_namespace="lrdwfp6kyp5x",
    ) == "oci://Ayoub@lrdwfp6kyp5x/cubes/example.zarr"
    assert _normalize_output_store_uri(
        "oci://Other@ns/cubes/example.zarr",
        output_bucket="Ayoub",
        output_namespace="lrdwfp6kyp5x",
    ) == "oci://Other@ns/cubes/example.zarr"


def test_coords_and_geotransform_from_north_up_transform() -> None:
    transform = DummyTransform(a=10.0, b=0.0, c=499980.0, d=0.0, e=-10.0, f=3900000.0)

    x, y = _coords_from_transform(transform, width=3, height=2)

    np.testing.assert_allclose(x, [499985.0, 499995.0, 500005.0])
    np.testing.assert_allclose(y, [3899995.0, 3899985.0])
    assert _geotransform_from_transform(transform) == "499980.0 10.0 0.0 3900000.0 0.0 -10.0"


def test_coords_reject_rotated_transform() -> None:
    with pytest.raises(ValueError, match="Rotated"):
        _coords_from_transform(DummyTransform(a=10.0, b=1.0, c=0.0, d=0.0, e=-10.0, f=0.0), width=1, height=1)


def test_validate_storage_layout_rejects_invalid_shard_size() -> None:
    with pytest.raises(ValueError, match="integer multiple"):
        _validate_storage_layout(chunk_size=512, shard_size=3000, zarr_version=3)
