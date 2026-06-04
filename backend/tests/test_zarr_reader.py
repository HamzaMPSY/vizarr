import json

import pytest

from app.core.zarr_reader import normalize_zarr_v2_metadata_entries
from app.core.zarr_reader import read_zarr_v2_store_metadata
from app.core.zarr_reader import _build_open_zarr_kwargs


class _StubConnector:
    def __init__(self, payloads: dict[str, str]) -> None:
        self._payloads = payloads
        self.prefixes: list[str] = []

    def get_filesystem(self):
        raise AssertionError("filesystem access is not expected in this helper test")

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket/{object_path.lstrip('/')}"

    def read_text(self, object_path: str, *, use_cache: bool = True) -> str:
        try:
            return self._payloads[object_path]
        except KeyError as exc:
            raise FileNotFoundError(object_path) from exc

    def list_prefixes(self, prefix: str | None = None) -> list[str]:
        return self.prefixes


def test_build_open_zarr_kwargs_uses_requested_consolidation_for_v2(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: None,
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": True,
    }


def test_build_open_zarr_kwargs_for_v3_without_consolidated_metadata_forces_false(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: {"zarr_format": 3},
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": False,
        "zarr_version": 3,
        "zarr_format": 3,
    }


def test_build_open_zarr_kwargs_for_v3_with_consolidated_metadata_preserves_true(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: {"zarr_format": 3, "consolidated_metadata": {"metadata": {}}},
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": True,
        "zarr_version": 3,
        "zarr_format": 3,
    }


def test_normalize_zarr_v2_metadata_entries_merges_array_dimensions() -> None:
    normalized = normalize_zarr_v2_metadata_entries(
        {
            ".zgroup": {"zarr_format": 2},
            "bands/.zarray": {"zarr_format": 2, "shape": [1, 2, 3, 4], "chunks": [1, 1, 3, 4]},
            "bands/.zattrs": {"_ARRAY_DIMENSIONS": ["time", "band", "y", "x"], "long_name": "Bands"},
            "x/.zarray": {"zarr_format": 2, "shape": [4], "chunks": [4]},
            "x/.zattrs": {"_ARRAY_DIMENSIONS": ["x"]},
        }
    )

    assert sorted(normalized) == ["bands", "x"]
    assert normalized["bands"]["dimension_names"] == ["time", "band", "y", "x"]
    assert normalized["bands"]["attributes"]["long_name"] == "Bands"
    assert normalized["x"]["dimension_names"] == ["x"]


def test_read_zarr_v2_store_metadata_reads_consolidated_xarray_dimensions() -> None:
    connector = _StubConnector(
        {
            "oci://bucket/cubes/v2.zarr/.zmetadata": json.dumps(
                {
                    "metadata": {
                        ".zgroup": {"zarr_format": 2},
                        ".zattrs": {"title": "Example"},
                        "bands/.zarray": {
                            "zarr_format": 2,
                            "shape": [1, 2, 3, 4],
                            "chunks": [1, 1, 3, 4],
                            "dtype": "<f4",
                            "compressor": None,
                            "fill_value": None,
                            "order": "C",
                            "filters": None,
                        },
                        "bands/.zattrs": {"_ARRAY_DIMENSIONS": ["time", "band", "y", "x"]},
                        "x/.zarray": {"zarr_format": 2, "shape": [4], "chunks": [4], "dtype": "<f8"},
                        "x/.zattrs": {"_ARRAY_DIMENSIONS": ["x"]},
                        "y/.zarray": {"zarr_format": 2, "shape": [3], "chunks": [3], "dtype": "<f8"},
                        "y/.zattrs": {"_ARRAY_DIMENSIONS": ["y"]},
                    }
                }
            )
        }
    )

    store_metadata, metadata = read_zarr_v2_store_metadata(connector=connector, store_path="cubes/v2.zarr")

    assert store_metadata["zarr_format"] == 2
    assert store_metadata["attributes"] == {"title": "Example"}
    assert sorted(metadata) == ["bands", "x", "y"]
    assert metadata["bands"]["dimension_names"] == ["time", "band", "y", "x"]


def test_read_zarr_v2_store_metadata_reads_unconsolidated_array_dimensions() -> None:
    connector = _StubConnector(
        {
            "oci://bucket/cubes/v2.zarr/.zgroup": json.dumps({"zarr_format": 2}),
            "oci://bucket/cubes/v2.zarr/.zattrs": json.dumps({"title": "Unconsolidated"}),
            "oci://bucket/cubes/v2.zarr/bands/.zarray": json.dumps(
                {
                    "zarr_format": 2,
                    "shape": [1, 2, 3, 4],
                    "chunks": [1, 1, 3, 4],
                    "dtype": "<f4",
                    "compressor": None,
                    "fill_value": None,
                    "order": "C",
                    "filters": None,
                }
            ),
            "oci://bucket/cubes/v2.zarr/bands/.zattrs": json.dumps(
                {"_ARRAY_DIMENSIONS": ["time", "band", "y", "x"]}
            ),
        }
    )
    connector.prefixes = ["cubes/v2.zarr/bands/"]

    store_metadata, metadata = read_zarr_v2_store_metadata(connector=connector, store_path="cubes/v2.zarr")

    assert store_metadata["zarr_format"] == 2
    assert store_metadata["attributes"] == {"title": "Unconsolidated"}
    assert sorted(metadata) == ["bands"]
    assert metadata["bands"]["dimension_names"] == ["time", "band", "y", "x"]


def test_read_zarr_v2_store_metadata_requires_metadata_mapping() -> None:
    connector = _StubConnector({"oci://bucket/cubes/bad.zarr/.zmetadata": json.dumps({"metadata": []})})

    with pytest.raises(ValueError, match="metadata object"):
        read_zarr_v2_store_metadata(connector=connector, store_path="cubes/bad.zarr")
