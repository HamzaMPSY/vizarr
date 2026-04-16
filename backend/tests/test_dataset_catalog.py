import numpy as np
import pytest

from app.core.dataset_catalog import _time_labels_from_values
from app.core.dataset_catalog import _select_projected_array_names
from app.core.zarr_v3 import read_store_metadata


def test_select_projected_array_names_accepts_non_landsat_band_dim_name() -> None:
    metadata = {
        "spectral_cube": {
            "shape": [1, 3, 128, 256],
            "dimension_names": ["time", "spectral", "y", "x"],
        },
        "spectral": {
            "shape": [3],
            "dimension_names": ["spectral"],
        },
        "x": {
            "shape": [256],
            "dimension_names": ["x"],
        },
        "y": {
            "shape": [128],
            "dimension_names": ["y"],
        },
    }

    assert _select_projected_array_names(metadata) == ("spectral_cube", "spectral")


def test_select_projected_array_names_rejects_missing_supported_4d_layout() -> None:
    metadata = {
        "value": {
            "shape": [128, 256],
            "dimension_names": ["y", "x"],
        }
    }

    with pytest.raises(ValueError, match="supported projected 4D array"):
        _select_projected_array_names(metadata)


class _StubConnector:
    def __init__(self) -> None:
        self.prefixes = ["cubes/example.zarr/bands/", "cubes/example.zarr/band/", "cubes/example.zarr/x/"]
        self.payloads = {
            "oci://bucket/cubes/example.zarr/zarr.json": '{"zarr_format": 3}',
            "oci://bucket/cubes/example.zarr/bands/zarr.json": '{"shape": [1, 2, 3, 4], "dimension_names": ["time", "band", "y", "x"]}',
            "oci://bucket/cubes/example.zarr/band/zarr.json": '{"shape": [2], "dimension_names": ["band"]}',
            "oci://bucket/cubes/example.zarr/x/zarr.json": '{"shape": [4], "dimension_names": ["x"]}',
        }

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket/{object_path.lstrip('/')}"

    def list_prefixes(self, prefix: str | None = None) -> list[str]:
        assert prefix == "cubes/example.zarr/"
        return self.prefixes


def test_read_store_metadata_falls_back_to_child_zarr_json_nodes(monkeypatch) -> None:
    connector = _StubConnector()

    monkeypatch.setattr(
        "app.core.zarr_reader.read_store_json",
        lambda connector, object_path: connector.payloads[object_path],
    )
    monkeypatch.setattr(
        "app.core.zarr_v3.read_store_json",
        lambda connector, object_path: connector.payloads[object_path],
    )

    store_metadata, metadata = read_store_metadata(connector=connector, store_path="cubes/example.zarr")

    assert store_metadata["zarr_format"] == 3
    assert sorted(metadata) == ["band", "bands", "x"]
    assert metadata["bands"]["dimension_names"] == ["time", "band", "y", "x"]


def test_time_labels_from_nanosecond_epoch_values_returns_iso_dates() -> None:
    labels = _time_labels_from_values(
        values=np.array([1736294400000000000, 1736899200000000000], dtype="int64"),
        attributes={"units": "nanoseconds since 1970-01-01T00:00:00", "calendar": "proleptic_gregorian"},
    )

    assert labels == ["2025-01-08", "2025-01-15"]
