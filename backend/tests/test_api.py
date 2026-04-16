from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_healthcheck() -> None:
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_datasets() -> None:
    response = client.get("/api/datasets")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == "demo-global"


def test_list_variables() -> None:
    response = client.get("/api/datasets/demo-global/variables")
    assert response.status_code == 200
    payload = response.json()
    ids = {item["id"] for item in payload}
    assert ids == {"temperature", "precipitation"}


def test_get_tile() -> None:
    response = client.get("/api/tiles/demo-global/temperature/1/1/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-cache-status"] in {"MISS", "HIT"}
    assert response.headers["x-representation"] == "browse"
    assert len(response.content) > 100


def test_get_tile_hits_memory_cache_on_second_request() -> None:
    first = client.get("/api/tiles/demo-global/temperature/1/1/1")
    second = client.get("/api/tiles/demo-global/temperature/1/1/1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["x-cache-status"] == "HIT"
    assert second.content == first.content


def test_high_zoom_tile_prefers_serving_representation() -> None:
    response = client.get("/api/tiles/demo-global/temperature/10/512/512")

    assert response.status_code == 200
    assert response.headers["x-representation"] == "serving"
