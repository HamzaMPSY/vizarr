import numpy as np
from fastapi.testclient import TestClient

from app.core.dataset_catalog import CatalogEntry
from app.main import app
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats
from app.core.zarr_v3 import ZarrV3ArrayMetadata


client = TestClient(app)


def test_query_point_returns_synthetic_source_value() -> None:
    dataset = app.state.registry.dataset
    lon = float(dataset.coords["lon"].values[10])
    lat = float(dataset.coords["lat"].values[20])
    expected = float(dataset["temperature"].isel(time=0, lat=20, lon=10).values)

    response = client.get(
        "/api/query/point",
        params={
            "dataset_id": "demo-global",
            "variable": "temperature",
            "lon": lon,
            "lat": lat,
            "time_index": 0,
            "diagnostics": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_type"] == "source_point"
    assert payload["value"] == expected
    assert payload["unit"] == "degC"
    assert payload["is_nodata"] is False
    assert payload["pixel_x"] == 10
    assert payload["pixel_y"] == 20
    assert payload["diagnostics"]["storage_backend"] == "synthetic"


def test_query_bbox_returns_synthetic_source_window() -> None:
    dataset = app.state.registry.dataset
    lon_values = dataset.coords["lon"].values
    lat_values = dataset.coords["lat"].values
    expected = dataset["temperature"].isel(time=0, lat=slice(20, 22), lon=slice(10, 12)).values
    west = float(lon_values[10])
    east = float(lon_values[11])
    south = float(min(lat_values[20], lat_values[21]))
    north = float(max(lat_values[20], lat_values[21]))

    response = client.get(
        "/api/query/bbox",
        params={
            "dataset_id": "demo-global",
            "variable": "temperature",
            "bbox": f"{west},{south},{east},{north}",
            "time_index": 0,
            "max_width": 4,
            "max_height": 4,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_type"] == "source_bbox"
    assert payload["shape"] == [2, 2]
    assert payload["values"] == expected.astype(float).tolist()
    assert payload["valid_count"] == 4


def test_query_range_returns_metadata_stats_without_bbox() -> None:
    response = client.get(
        "/api/query/range",
        params={
            "dataset_id": "demo-global",
            "variable": "temperature",
            "time_index": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    variable = next(item for item in app.state.registry.meta.variables if item.id == "temperature")
    assert payload["result_type"] == "range_stats"
    assert payload["stats_source"] == "metadata"
    assert payload["min"] == variable.stats.min
    assert payload["max"] == variable.stats.max
    assert payload["p02"] == variable.stats.p02
    assert payload["p98"] == variable.stats.p98
    assert payload["histogram_counts"] == []


def test_query_range_samples_synthetic_bbox_histogram() -> None:
    dataset = app.state.registry.dataset
    lon_values = dataset.coords["lon"].values
    lat_values = dataset.coords["lat"].values
    expected = dataset["temperature"].isel(time=0, lat=slice(20, 22), lon=slice(10, 12)).values.astype(float)
    west = float(lon_values[10])
    east = float(lon_values[11])
    south = float(min(lat_values[20], lat_values[21]))
    north = float(max(lat_values[20], lat_values[21]))
    p02, p98 = np.percentile(expected.ravel(), [2, 98])

    response = client.get(
        "/api/query/range",
        params={
            "dataset_id": "demo-global",
            "variable": "temperature",
            "bbox": f"{west},{south},{east},{north}",
            "time_index": 0,
            "bins": 4,
            "max_width": 4,
            "max_height": 4,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stats_source"] == "sampled_bbox"
    assert payload["valid_count"] == 4
    assert payload["min"] == float(expected.min())
    assert payload["max"] == float(expected.max())
    assert np.isclose(payload["p02"], p02)
    assert np.isclose(payload["p98"], p98)
    assert len(payload["histogram_bins"]) == 5
    assert len(payload["histogram_counts"]) == 4
    assert sum(payload["histogram_counts"]) == 4


def test_query_readback_does_not_use_visual_tile_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.colormap.encode_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("colormap should not be used")),
    )
    monkeypatch.setattr(
        "app.core.tile_generator.generate_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("WebP tile generator should not be used")),
    )

    response = client.get(
        "/api/query/point",
        params={
            "dataset_id": "demo-global",
            "variable": "temperature",
            "lon": 0,
            "lat": 0,
            "time_index": 0,
        },
    )

    assert response.status_code == 200
    assert response.json()["result_type"] == "source_point"


class _ReadbackConnector:
    def __init__(self, values: np.ndarray) -> None:
        self.values = values.reshape(1, 1, *values.shape)
        self.payloads = {
            "cubes/readback.zarr/bands/c/0/0/0/0": self.values.tobytes(),
            "bucket@ns/cubes/readback.zarr/bands/c/0/0/0/0": self.values.tobytes(),
        }

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket@ns/{object_path.lstrip('/')}"

    def read_bytes(self, object_path: str, *, use_cache: bool = True) -> bytes:
        payload = self.payloads.get(object_path)
        if payload is None:
            raise FileNotFoundError(object_path)
        return payload


def _configure_oci_readback_app_state(monkeypatch, values: np.ndarray) -> CatalogEntry:
    entry = CatalogEntry(
        id="dataset-1",
        path="cubes/readback.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="readback.zarr",
            description="Readback fixture",
            variables=[
                VariableMeta(
                    id="NDVI",
                    name="NDVI",
                    unit="1",
                    time_steps=1,
                    stats=VariableStats(min=0.0, max=65535.0, p02=0.0, p98=65535.0),
                )
            ],
            crs_authority="EPSG:4326",
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
            data_type="uint16",
            fill_value=None,
            codecs=[],
            separator="/",
            attributes={},
            dimension_names=("time", "band", "y", "x"),
        ),
        crs_wkt='GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],CS[ellipsoidal,2],AXIS["longitude",east],AXIS["latitude",north],ANGLEUNIT["degree",0.0174532925199433]]',
        geo_transform=(-0.5, 1.0, 0.0, 1.5, 0.0, -1.0),
    )
    monkeypatch.setattr(app.state.settings, "storage_backend", "oci_zarr")
    monkeypatch.setattr(app.state, "storage_connector", _ReadbackConnector(values), raising=False)
    monkeypatch.setattr(app.state, "dataset_catalog", {entry.id: entry}, raising=False)
    monkeypatch.setattr("app.core.readback.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)
    return entry


def test_query_point_returns_oci_source_value_before_visual_encoding(monkeypatch) -> None:
    values = np.array([[101, 102], [201, 202]], dtype=np.uint16)
    _configure_oci_readback_app_state(monkeypatch, values)

    response = client.get(
        "/api/query/point",
        params={
            "dataset_id": "dataset-1",
            "variable": "NDVI",
            "lon": 0.0,
            "lat": 1.0,
            "time_index": 0,
            "diagnostics": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_type"] == "source_point"
    assert payload["value"] == 101
    assert payload["unit"] == "1"
    assert payload["pixel_x"] == 0
    assert payload["pixel_y"] == 0
    assert payload["diagnostics"]["array_name"] == "bands"
    assert payload["diagnostics"]["dtype"] == "uint16"
    assert payload["diagnostics"]["zarr_chunk_count"] == 1


def test_query_bbox_returns_oci_source_window_and_enforces_size(monkeypatch) -> None:
    values = np.array([[101, 102], [201, 202]], dtype=np.uint16)
    _configure_oci_readback_app_state(monkeypatch, values)

    response = client.get(
        "/api/query/bbox",
        params={
            "dataset_id": "dataset-1",
            "variable": "NDVI",
            "bbox": "-0.5,-0.5,1.5,1.5",
            "time_index": 0,
            "max_width": 2,
            "max_height": 2,
            "diagnostics": True,
        },
    )
    oversized = client.get(
        "/api/query/bbox",
        params={
            "dataset_id": "dataset-1",
            "variable": "NDVI",
            "bbox": "-0.5,-0.5,1.5,1.5",
            "time_index": 0,
            "max_width": 1,
            "max_height": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["shape"] == [2, 2]
    assert payload["values"] == [[101, 102], [201, 202]]
    assert payload["valid_count"] == 4
    assert payload["diagnostics"]["source_window"] == {"x_start": 0, "x_stop": 2, "y_start": 0, "y_stop": 2}
    assert oversized.status_code == 413


def test_preview_route_returns_planned_artifact() -> None:
    response = client.post(
        "/api/query/preview",
        json={
            "collection_id": "demo-global",
            "aoi_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            "start": "2026-01-01",
            "end": "2026-01-31",
            "bands": ["temperature"],
            "style": "rgb-default",
            "max_size": 1024,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_type"] == "preview"
    assert payload["execution_path"] == "interactive"
    assert payload["artifact_id"].startswith("art_")
    assert payload["plan"]["chosen_representation"] == "browse"
    assert payload["plan"]["collection_id"] == "demo-global"
    assert payload["plan"]["selected_cube"] == "demo-global:browse:rgb-default"


def test_clip_route_hands_off_oversized_request_to_batch() -> None:
    response = client.post(
        "/api/query/clip",
        json={
            "collection_id": "demo-global",
            "aoi_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            "start": "2026-01-01",
            "end": "2026-03-31",
            "bands": ["temperature", "precipitation", "temperature", "precipitation", "temperature"],
            "output_format": "zarr",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["result_type"] == "export"
    assert payload["status"] == "queued"
    assert payload["plan"]["execution_path"] == "batch"


def test_export_routes_create_and_fetch_job() -> None:
    create_response = client.post(
        "/api/exports",
        json={
            "collection_id": "demo-global",
            "aoi_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            "start": "2026-01-01",
            "end": "2026-03-31",
            "bands": ["temperature", "precipitation"],
            "output_format": "zarr",
        },
    )

    assert create_response.status_code == 200
    job_id = create_response.json()["job_id"]

    status_response = client.get(f"/api/exports/{job_id}")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["job_id"] == job_id
    assert payload["status"] == "queued"
    assert payload["job_type"] == "export"
