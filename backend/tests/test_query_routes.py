from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


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
