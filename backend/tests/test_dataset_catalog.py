import numpy as np
import pytest

from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import _refine_bounds_from_nonempty_data
from app.core.dataset_catalog import _time_labels_from_values
from app.core.dataset_catalog import _select_projected_array_names
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.core.zarr_v3 import read_store_metadata
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats


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


def test_ensure_catalog_entry_ready_uses_geotransform_without_loading_coordinates(monkeypatch) -> None:
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
            attributes={"band_labels": ["NDVI"]},
            dimension_names=("time", "band", "y", "x"),
        ),
        x_meta=object(),  # type: ignore[arg-type]
        y_meta=object(),  # type: ignore[arg-type]
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(29.95, 0.1, 0.0, 10.05, 0.0, -0.1),
    )

    monkeypatch.setattr(
        "app.core.dataset_catalog.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: current_entry,
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_1d_numeric_array",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("coordinate arrays should not be loaded")),
    )

    ready = ensure_catalog_entry_ready(entry, connector=object())  # type: ignore[arg-type]

    assert ready.meta.bounds is not None
    assert ready.meta.bounds.west == pytest.approx(29.95)
    assert ready.meta.bounds.east == pytest.approx(30.15)
    assert ready.meta.native_resolution_m is not None


def test_refine_bounds_from_nonempty_data_prefers_source_window_over_browse(monkeypatch) -> None:
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
            shape=(2, 1, 200, 300),
            chunk_shape=(1, 1, 64, 64),
            data_type="float32",
            fill_value=np.nan,
            codecs=[],
            separator="/",
            attributes={"band_labels": ["NDVI"]},
            dimension_names=("time", "band", "y", "x"),
        ),
        geo_transform=(10.0, 0.1, 0.0, 20.0, 0.0, -0.1),
    )
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=2,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]

    monkeypatch.setattr(
        "app.core.dataset_catalog.estimate_4d_nonempty_pixel_bounds",
        lambda **_kwargs: (10, 30, 20, 50),
    )

    bounds = _refine_bounds_from_nonempty_data(entry=entry, connector=object())  # type: ignore[arg-type]

    assert bounds is not None
    assert bounds.west == pytest.approx(11.0)
    assert bounds.east == pytest.approx(13.0)
    assert bounds.north == pytest.approx(18.0)
    assert bounds.south == pytest.approx(15.0)
