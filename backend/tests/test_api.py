import asyncio
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.rate_limit import ApiKeyRateLimiter
from app.core.dataset_catalog import CatalogEntry
from app.core.oci_auth import OCIAuthExpiredError
from app.core.oci_object_storage import OCIObjectInfo
from app.core.tile_observability import record_object_read
from app.core.tile_observability import record_zarr_chunk_read
from app.main import app
from app.main import _cors_allowed_origins
from app.models.dataset import DatasetBounds
from app.models.dataset import DatasetMeta
from app.models.dataset import CompositeStyle
from app.models.dataset import TileJSON
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats
from app.models.plans import QueryPlan
from app.services.browse_jobs import BrowseGenerationJobStore


client = TestClient(app)


def test_healthcheck() -> None:
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_enabled_requires_api_key(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "secret-key")

    missing = client.get("/api/datasets")
    invalid = client.get("/api/datasets", headers={"X-API-Key": "wrong"})
    valid = client.get("/api/datasets", headers={"X-API-Key": "secret-key"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200


def test_auth_is_required_by_production_environment(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", False)
    monkeypatch.setattr(app.state.settings, "app_environment", "production")
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "")

    health = client.get("/api/healthz")
    protected = client.get("/api/datasets")

    assert health.status_code == 200
    assert protected.status_code == 503
    assert "AUTH_API_KEYS" in protected.json()["detail"]


def test_production_cors_does_not_default_to_wildcard() -> None:
    settings = SimpleNamespace(app_environment="production", cors_allowed_origins="")

    assert _cors_allowed_origins(settings) == []


def test_configured_production_cors_filters_wildcard() -> None:
    settings = SimpleNamespace(
        app_environment="production",
        cors_allowed_origins="*,https://viewer.example.com",
    )

    assert _cors_allowed_origins(settings) == ["https://viewer.example.com"]


def test_api_key_rate_limit_returns_429(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "limited-key")
    monkeypatch.setattr(
        app.state,
        "api_key_rate_limiter",
        ApiKeyRateLimiter(limit=1, window_seconds=60),
        raising=False,
    )

    first = client.get("/api/datasets", headers={"X-API-Key": "limited-key"})
    second = client.get("/api/datasets", headers={"X-API-Key": "limited-key"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "API key rate limit exceeded"
    assert second.headers["retry-after"]


def test_dataset_scoped_api_key_filters_dataset_list(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "scoped=demo-global,other=missing-dataset")

    visible = client.get("/api/datasets", headers={"X-API-Key": "scoped"})
    hidden = client.get("/api/datasets", headers={"X-API-Key": "other"})
    denied = client.get("/api/datasets/not-demo/variables", headers={"X-API-Key": "scoped"})

    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == ["demo-global"]
    assert hidden.status_code == 200
    assert hidden.json() == []
    assert denied.status_code == 403


def test_dataset_scoped_api_key_cannot_use_storage_debug_routes(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "scoped=demo-global,global-key")

    scoped = client.get("/api/storage/objects", headers={"X-API-Key": "scoped"})
    global_key = client.get("/api/storage/objects", headers={"X-API-Key": "global-key"})

    assert scoped.status_code == 403
    assert global_key.status_code == 400


def test_dataset_websocket_requires_auth_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "ws-key=demo-global")

    try:
        with client.websocket_connect("/ws/datasets"):
            raise AssertionError("unauthenticated websocket should not connect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    with client.websocket_connect("/ws/datasets?api_key=ws-key") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "datasets.invalidate"
    assert payload["datasets"] == [{"id": "demo-global", "name": "Synthetic Global Weather"}]


def test_list_datasets() -> None:
    response = client.get("/api/datasets")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == "demo-global"


def test_list_datasets_filters_by_bbox() -> None:
    visible = client.get("/api/datasets?bbox=10,0,20,5")
    hidden = client.get("/api/datasets?bbox=10,86,20,89")

    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == ["demo-global"]
    assert hidden.status_code == 200
    assert hidden.json() == []


def test_list_datasets_rejects_invalid_bbox() -> None:
    response = client.get("/api/datasets?bbox=1,2,3")

    assert response.status_code == 422
    assert "bbox" in response.json()["detail"]


def test_list_variables() -> None:
    response = client.get("/api/datasets/demo-global/variables")
    assert response.status_code == 200
    payload = response.json()
    ids = {item["id"] for item in payload}
    assert ids == {"temperature", "precipitation"}


def test_get_tile(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", False)

    response = client.get("/api/tiles/demo-global/temperature/1/1/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-cache-status"] in {"MISS", "HIT"}
    assert response.headers["x-representation"] == "serving"
    assert "x-tile-time-ms" not in response.headers
    assert len(response.content) > 100


def test_tile_debug_headers_are_gated_by_settings(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", False)
    hidden = client.get("/api/tiles/demo-global/temperature/2/1/1?colormap=magma")

    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", True)
    visible = client.get("/api/tiles/demo-global/temperature/2/1/2?colormap=magma")

    assert hidden.status_code == 200
    assert visible.status_code == 200
    assert "x-tile-time-ms" not in hidden.headers
    assert float(visible.headers["x-tile-time-ms"]) >= 0
    assert float(visible.headers["x-tile-planner-ms"]) >= 0
    assert float(visible.headers["x-tile-cache-lookup-ms"]) >= 0
    assert float(visible.headers["x-tile-render-ms"]) >= 0
    assert float(visible.headers["x-tile-encode-ms"]) >= 0
    assert visible.headers["x-object-get-count"] == "0"
    assert visible.headers["x-zarr-chunk-count"] == "0"


def test_get_colormap_palette() -> None:
    response = client.get("/api/colormaps/viridis/palette?samples=8")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 8
    assert all(len(item) == 4 for item in payload)


def test_get_tile_hits_memory_cache_on_second_request() -> None:
    first = client.get("/api/tiles/demo-global/temperature/1/1/1")
    second = client.get("/api/tiles/demo-global/temperature/1/1/1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["x-cache-status"] == "HIT"
    assert second.content == first.content


def test_dataset_tile_cache_invalidation_bypasses_stale_memory_cache() -> None:
    asyncio.run(app.state.cache.invalidate_dataset_tiles("demo-global"))
    first = client.get("/api/tiles/demo-global/temperature/4/7/6?colormap=magma")
    second = client.get("/api/tiles/demo-global/temperature/4/7/6?colormap=magma")
    asyncio.run(app.state.cache.invalidate_dataset_tiles("demo-global"))
    third = client.get("/api/tiles/demo-global/temperature/4/7/6?colormap=magma")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert first.headers["x-cache-status"] == "MISS"
    assert second.headers["x-cache-status"] == "HIT"
    assert third.headers["x-cache-status"] == "MISS"


def test_tilejson_template_changes_after_dataset_tile_cache_invalidation() -> None:
    asyncio.run(app.state.cache.invalidate_dataset_tiles("demo-global"))
    first = client.get("/api/tilejson/demo-global/temperature?colormap=viridis")
    asyncio.run(app.state.cache.invalidate_dataset_tiles("demo-global"))
    second = client.get("/api/tilejson/demo-global/temperature?colormap=viridis")

    assert first.status_code == 200
    assert second.status_code == 200
    first_tile = first.json()["tiles"][0]
    second_tile = second.json()["tiles"][0]
    assert "cache_version=" in first_tile
    assert "cache_version=" in second_tile
    assert first_tile != second_tile


def test_high_zoom_tile_prefers_serving_representation() -> None:
    response = client.get("/api/tiles/demo-global/temperature/10/512/512")

    assert response.status_code == 200
    assert response.headers["x-representation"] == "serving"


def test_dataset_websocket_sends_invalidation_snapshot() -> None:
    with client.websocket_connect("/ws/datasets") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "datasets.invalidate"
    assert payload["datasets"][0]["id"] == "demo-global"
    assert "version" in payload


def test_dataset_websocket_supports_ping() -> None:
    with client.websocket_connect("/ws/datasets") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "ping"})
        payload = websocket.receive_json()

    assert payload == {"type": "pong"}


class _FakeZarrConnector:
    def __init__(self) -> None:
        self.namespace = "ns"
        self.payloads = {
            "cubes/example.zarr/zarr.json": b'{"zarr_format":3}',
            "bucket@ns/cubes/example.zarr/zarr.json": b'{"zarr_format":3}',
            "cubes/example.zarr/bands/c/0/0/0/0": b"0123456789",
            "multiscale/cubes/example.zarr/zarr.json": (
                b'{"zarr_format":3,"attributes":{"multiscales":[{"datasets":[{"path":"0"}]}]}}'
            ),
            "multiscale/cubes/example.zarr/0/bands/c/0/0/0/0": b"abcdefghij",
        }

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket@ns/{object_path.lstrip('/')}"

    def read_text(self, object_path: str, *, use_cache: bool = True) -> str:
        return self.read_bytes(object_path).decode("utf-8")

    def head_object(self, object_path: str) -> OCIObjectInfo:
        payload = self.payloads.get(object_path)
        if payload is None:
            raise FileNotFoundError(object_path)
        content_type = "application/json" if object_path.endswith("zarr.json") else "application/octet-stream"
        return OCIObjectInfo(
            name=object_path,
            size=len(payload),
            etag=f"etag-{len(payload)}",
            content_type=content_type,
        )

    def read_bytes(self, object_path: str) -> bytes:
        resolved = object_path.removeprefix("oci://")
        payload = self.payloads.get(resolved) or self.payloads.get(object_path)
        if payload is None:
            raise FileNotFoundError(object_path)
        return payload

    def read_byte_range(
        self,
        object_path: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> bytes:
        payload = self.read_bytes(object_path)
        return payload[start:end]

    def read_byte_tail(self, object_path: str, *, length: int) -> bytes:
        payload = self.read_bytes(object_path)
        return payload[-length:]


def _catalog_entry() -> CatalogEntry:
    return CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[],
            zarr_format=3,
            zarr_consolidated=True,
            zarr_proxy_root="/api/zarr/dataset-1",
            multiscale_store_path="multiscale/cubes/example.zarr",
            multiscale_zarr_format=3,
            multiscale_zarr_consolidated=True,
            multiscale_proxy_root="/api/zarr/multiscale/dataset-1",
            multiscale_population_strategy="prepopulated_then_lazy",
            multiscale_prepopulated_zoom_max=12,
            multiscale_max_zoom=15,
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=[],
        band_indices={},
    )


def _configure_oci_app_state(monkeypatch) -> CatalogEntry:
    entry = _catalog_entry()
    monkeypatch.setattr(app.state.settings, "storage_backend", "oci_zarr")
    monkeypatch.setattr(app.state, "storage_connector", _FakeZarrConnector(), raising=False)
    monkeypatch.setattr(app.state, "dataset_catalog", {entry.id: entry}, raising=False)
    monkeypatch.setattr(app.state, "dataset_manifest", [entry.meta.model_copy(deep=True)], raising=False)
    return entry


def test_list_datasets_exposes_zarr_proxy_metadata(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/datasets")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["zarr_format"] == 3
    assert payload[0]["zarr_consolidated"] is True
    assert payload[0]["zarr_proxy_root"] == "/api/zarr/dataset-1"
    assert payload[0]["multiscale_store_path"] == "multiscale/cubes/example.zarr"
    assert payload[0]["multiscale_proxy_root"] == "/api/zarr/multiscale/dataset-1"
    assert payload[0]["multiscale_population_strategy"] == "prepopulated_then_lazy"
    assert payload[0]["multiscale_prepopulated_zoom_max"] == 12
    assert payload[0]["multiscale_max_zoom"] == 15


def test_oci_list_datasets_filters_manifest_by_bbox(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    variable = VariableMeta(
        id="NDVI",
        name="NDVI",
        unit="1",
        time_steps=1,
        stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
    )
    entry.meta.variables = [variable]
    entry.meta.bounds = DatasetBounds(west=30.0, south=-2.0, east=31.0, north=-1.0)
    remote = entry.meta.model_copy(deep=True)
    remote.id = "dataset-2"
    remote.name = "remote.zarr"
    remote.bounds = DatasetBounds(west=-120.0, south=30.0, east=-110.0, north=40.0)
    app.state.dataset_manifest = [entry.meta.model_copy(deep=True), remote]

    response = client.get("/api/datasets?bbox=29,-3,32,0")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["dataset-1"]


def test_oci_list_datasets_bbox_filter_supports_antimeridian(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.meta.bounds = DatasetBounds(west=170.0, south=-10.0, east=-170.0, north=10.0)
    app.state.dataset_manifest = [entry.meta.model_copy(deep=True)]

    response = client.get("/api/datasets?bbox=175,-5,-175,5")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["dataset-1"]


def test_list_datasets_can_return_loaded_object_manifest_without_catalog_scan(monkeypatch) -> None:
    manifest = [
        DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[
                VariableMeta(
                    id="B1",
                    name="Band 1",
                    unit="DN",
                    time_steps=1,
                    stats=VariableStats(min=0.0, max=1.0, p02=0.02, p98=0.98),
                )
            ],
            zarr_format=3,
            zarr_consolidated=True,
            zarr_proxy_root="/api/zarr/dataset-1",
        )
    ]
    monkeypatch.setattr(app.state.settings, "storage_backend", "oci_zarr")
    monkeypatch.setattr(app.state, "storage_connector", object(), raising=False)
    monkeypatch.setattr(app.state, "dataset_catalog", None, raising=False)
    monkeypatch.setattr(app.state, "dataset_manifest", manifest, raising=False)
    monkeypatch.setattr(
        app.state,
        "dataset_manifest_diagnostics",
        {
            "source": "object_manifest",
            "status": "loaded",
            "generated_at": "2026-06-03T00:00:00+00:00",
            "dataset_count": 1,
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.api.datasets.get_or_build_catalog",
        lambda _app: (_ for _ in ()).throw(AssertionError("catalog scan should not run")),
    )

    response = client.get("/api/datasets")

    assert response.status_code == 200
    assert response.headers["x-dataset-manifest-source"] == "object_manifest"
    assert response.headers["x-dataset-manifest-status"] == "loaded"
    assert response.headers["x-dataset-manifest-generated-at"] == "2026-06-03T00:00:00+00:00"
    payload = response.json()
    assert [item["id"] for item in payload] == ["dataset-1"]
    assert payload[0]["variables"][0]["id"] == "B1"


def test_list_datasets_hydrates_variables_when_manifest_is_shallow(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = []
    app.state.dataset_manifest = [entry.meta.model_copy(deep=True)]

    def _hydrate(current_entry, _connector):
        current_entry.meta.variables = [
            VariableMeta(
                id="NDVI",
                name="NDVI",
                unit="1",
                time_steps=4,
                stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
            )
        ]
        return current_entry

    monkeypatch.setattr("app.api.datasets.ensure_catalog_entry_metadata_ready", _hydrate)

    response = client.get("/api/datasets")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["variables"][0]["id"] == "NDVI"
    assert app.state.dataset_manifest[0].variables[0].id == "NDVI"


def test_zarr_proxy_returns_full_object(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/zarr/dataset-1/zarr.json")

    assert response.status_code == 200
    assert response.content == b'{"zarr_format":3}'
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == "etag-17"
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_dataset_scoped_api_key_allows_zarr_proxy_for_allowed_dataset(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "scoped=dataset-1")

    responses = [
        client.get("/api/zarr/dataset-1", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
        client.head("/api/zarr/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/multiscale/dataset-1", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/multiscale/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]


def test_dataset_scoped_api_key_denies_zarr_proxy_for_other_dataset(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)
    monkeypatch.setattr(app.state.settings, "auth_enabled", True)
    monkeypatch.setattr(app.state.settings, "auth_api_keys", "scoped=other-dataset")

    missing_key = client.get("/api/zarr/dataset-1/zarr.json")
    denied = [
        client.get("/api/zarr/dataset-1", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
        client.head("/api/zarr/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/multiscale/dataset-1", headers={"X-API-Key": "scoped"}),
        client.get("/api/zarr/multiscale/dataset-1/zarr.json", headers={"X-API-Key": "scoped"}),
    ]

    assert missing_key.status_code == 401
    assert [response.status_code for response in denied] == [403, 403, 403, 403, 403]


def test_zarr_proxy_supports_byte_ranges(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get(
        "/api/zarr/dataset-1/bands/c/0/0/0/0",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"


def test_zarr_proxy_supports_head_without_body(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.head("/api/zarr/dataset-1/zarr.json")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "17"
    assert response.headers["etag"] == "etag-17"


def test_zarr_proxy_uses_if_none_match_cache_validation(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)
    connector = app.state.storage_connector

    def fail_read(*_args, **_kwargs):
        raise AssertionError("conditional cache validation should not read object bytes")

    monkeypatch.setattr(connector, "read_bytes", fail_read)

    response = client.get("/api/zarr/dataset-1/zarr.json", headers={"If-None-Match": '"etag-17"'})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == "etag-17"
    assert response.headers["cache-control"] == "public, max-age=3600"


def test_multiscale_zarr_proxy_returns_full_object(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/zarr/multiscale/dataset-1/zarr.json")

    assert response.status_code == 200
    assert response.content == b'{"zarr_format":3,"attributes":{"multiscales":[{"datasets":[{"path":"0"}]}]}}'
    assert response.headers["x-zarr-variant"] == "multiscale"


def test_multiscale_zarr_proxy_supports_byte_ranges(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get(
        "/api/zarr/multiscale/dataset-1/0/bands/c/0/0/0/0",
        headers={"Range": "bytes=1-3"},
    )

    assert response.status_code == 206
    assert response.content == b"bcd"
    assert response.headers["content-range"] == "bytes 1-3/10"
    assert response.headers["x-zarr-variant"] == "multiscale"


def test_zarr_proxy_rejects_invalid_range(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get(
        "/api/zarr/dataset-1/bands/c/0/0/0/0",
        headers={"Range": "bytes=5-2"},
    )

    assert response.status_code == 416


def test_zarr_proxy_rejects_path_traversal(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/zarr/dataset-1/../zarr.json")

    assert response.status_code in {400, 404}


def test_zarr_proxy_returns_404_for_missing_object(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/zarr/dataset-1/missing")

    assert response.status_code == 404


def test_zarr_proxy_requires_oci_backend(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "storage_backend", "synthetic")
    monkeypatch.setattr(app.state, "storage_connector", None, raising=False)

    response = client.get("/api/zarr/dataset-1/zarr.json")

    assert response.status_code == 400


def test_storage_zarr_json_accepts_relative_path(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/storage/zarr-json?zarr_path=cubes/example.zarr")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object_path"] == "cubes/example.zarr/zarr.json"
    assert payload["resolved_path"] == "oci://bucket@ns/cubes/example.zarr/zarr.json"
    assert payload["metadata"]["zarr_format"] == 3


def test_storage_zarr_json_accepts_full_oci_uri(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/storage/zarr-json?zarr_path=oci://bucket@ns/cubes/example.zarr")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object_path"] == "oci://bucket@ns/cubes/example.zarr/zarr.json"
    assert payload["resolved_path"] == "oci://bucket@ns/cubes/example.zarr/zarr.json"
    assert payload["metadata"]["zarr_format"] == 3


def test_storage_zarr_json_returns_404_for_missing_root(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    response = client.get("/api/storage/zarr-json?zarr_path=cubes/missing.zarr")

    assert response.status_code == 404


def test_oci_tile_returns_pyramid_representation_when_planned(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.meta.multiscale_zarr_format = 2

    metadata_calls = {"metadata": 0, "full": 0}

    monkeypatch.setattr(
        app.state.planner,
        "plan_tile_request",
        lambda **_kwargs: QueryPlan(
            planner_version="v1",
            collection_id=entry.id,
            request_class="tile",
            chosen_representation="pyramid",
            execution_path="interactive",
            request_fingerprint="fingerprint",
            response_cache_key="artifact:tile:pyramid:fingerprint",
            plan_cache_key="plan:fingerprint",
            selected_cube=f"{entry.id}:pyramid",
            selected_path=entry.meta.multiscale_store_path,
        ),
    )
    monkeypatch.setattr(
        "app.api.tiles.ensure_catalog_entry_metadata_ready",
        lambda current_entry, _connector: metadata_calls.__setitem__("metadata", metadata_calls["metadata"] + 1)
        or current_entry,
    )
    monkeypatch.setattr(
        "app.api.tiles.ensure_catalog_entry_ready",
        lambda current_entry, _connector: metadata_calls.__setitem__("full", metadata_calls["full"] + 1) or current_entry,
    )
    monkeypatch.setattr("app.api.tiles.generate_pyramid_tile", lambda *_args, **_kwargs: (b"pyramid-bytes", (0.1, 0.9)))
    monkeypatch.setattr(
        "app.api.tiles.generate_projected_band_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("serving renderer should not be used")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_browse_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browse renderer should not be used")),
    )

    response = client.get("/api/tiles/dataset-1/NDVI/6/32/30")

    assert response.status_code == 200
    assert response.content == b"pyramid-bytes"
    assert response.headers["x-representation"] == "pyramid"
    assert response.headers["x-cache-status"] == "MISS"
    assert response.headers["x-request-class"] == "tile"
    assert response.headers["x-execution-path"] == "interactive"
    assert response.headers["x-data-vmin"] == "0.1"
    assert response.headers["x-data-vmax"] == "0.9"
    assert metadata_calls == {"metadata": 1, "full": 0}


def test_oci_tile_falls_back_from_pyramid_to_serving(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.meta.multiscale_zarr_format = 2

    monkeypatch.setattr(
        app.state.planner,
        "plan_tile_request",
        lambda **_kwargs: QueryPlan(
            planner_version="v1",
            collection_id=entry.id,
            request_class="tile",
            chosen_representation="pyramid",
            execution_path="interactive",
            request_fingerprint="fallback",
            response_cache_key="artifact:tile:pyramid:fallback",
            plan_cache_key="plan:fallback",
            selected_cube=f"{entry.id}:pyramid",
            selected_path=entry.meta.multiscale_store_path,
        ),
    )
    monkeypatch.setattr("app.api.tiles.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)
    monkeypatch.setattr(
        "app.api.tiles.generate_pyramid_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing chunk")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_and_cache_pyramid_tile",
        lambda *_args, **_kwargs: (b"serving-bytes", (0.2, 0.8)),
    )

    response = client.get("/api/tiles/dataset-1/NDVI/8/120/110")

    assert response.status_code == 200
    assert response.content == b"serving-bytes"
    assert response.headers["x-representation"] == "serving"
    assert response.headers["x-cache-status"] == "MISS"
    assert response.headers["x-data-vmin"] == "0.2"


def test_oci_tile_outside_dataset_bounds_returns_empty_without_source_reads(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", True)
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    entry.meta.bounds = DatasetBounds(west=30.39, south=-2.09, east=30.81, north=-1.05)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]

    monkeypatch.setattr(
        app.state.planner,
        "plan_tile_request",
        lambda **_kwargs: QueryPlan(
            planner_version="v1",
            collection_id=entry.id,
            request_class="tile",
            chosen_representation="serving",
            execution_path="interactive",
            request_fingerprint="outside-bounds",
            response_cache_key="artifact:tile:outside-bounds",
            plan_cache_key="plan:outside-bounds",
            selected_cube=f"{entry.id}:serving",
            selected_path=entry.path,
        ),
    )
    monkeypatch.setattr("app.api.tiles.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)
    monkeypatch.setattr(
        "app.api.tiles.ensure_catalog_entry_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full catalog readiness should not run")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_projected_band_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct renderer should not run")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_projected_composite_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("composite renderer should not run")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_browse_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browse renderer should not run")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_pyramid_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pyramid renderer should not run")),
    )

    response = client.get("/api/tiles/dataset-1/NDVI/6/0/0?colormap=oob-test")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-representation"] == "empty"
    assert response.headers["x-planned-representation"] == "serving"
    assert response.headers["x-tile-empty"] == "bounds"
    assert response.headers["x-cache-status"] == "BYPASS"
    assert response.headers["x-object-get-count"] == "0"
    assert response.headers["x-zarr-chunk-count"] == "0"
    assert len(response.content) > 0


def test_oci_direct_tile_returns_503_when_compute_budget_exceeded(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", True)
    monkeypatch.setattr(app.state.settings, "direct_tile_max_parallel_chunk_reads", 2)
    monkeypatch.setattr(app.state.settings, "direct_tile_max_zarr_chunks", 1)
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]

    monkeypatch.setattr(
        app.state.planner,
        "plan_tile_request",
        lambda **_kwargs: QueryPlan(
            planner_version="v1",
            collection_id=entry.id,
            request_class="tile",
            chosen_representation="serving",
            execution_path="interactive",
            request_fingerprint="budget",
            response_cache_key="artifact:tile:budget",
            plan_cache_key="plan:budget",
            selected_cube=entry.id,
            selected_path=entry.path,
        ),
    )
    monkeypatch.setattr("app.api.tiles.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    def _generate_direct_tile(*args):
        assert args[-1] == 2
        record_object_read(bytes_read=128, byte_range=True)
        record_zarr_chunk_read()
        record_zarr_chunk_read()
        return b"direct-bytes", (0.2, 0.8)

    monkeypatch.setattr("app.api.tiles.generate_projected_band_tile", _generate_direct_tile)

    response = client.get("/api/tiles/dataset-1/NDVI/9/277/244?colormap=budget-test")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "direct_tile_compute_budget_exceeded",
            "reason": "chunk_reads 2 exceeded limit 1",
            "metric": "chunk_reads",
            "actual": 2,
            "limit": 1,
        }
    }
    assert response.headers["x-cache-status"] == "BYPASS"
    assert response.headers["x-representation"] == "serving"
    assert response.headers["x-tile-budget-status"] == "exceeded"
    assert response.headers["x-tile-budget-metric"] == "chunk_reads"
    assert response.headers["x-tile-budget-limit"] == "1"
    assert response.headers["x-tile-budget-actual"] == "2"
    assert response.headers["x-zarr-chunk-count"] == "2"


def test_oci_tile_serves_composite_style(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    monkeypatch.setattr(app.state.settings, "tile_debug_headers_enabled", True)
    entry.band_names = ["B4", "B3", "B2"]
    entry.band_indices = {"B4": 0, "B3": 1, "B2": 2}
    entry.meta.variables = [
        VariableMeta(
            id="B4",
            name="B4 Red",
            unit="DN",
            time_steps=1,
            stats=VariableStats(min=0.0, max=255.0, p02=0.0, p98=255.0),
        ),
        VariableMeta(
            id="B3",
            name="B3 Green",
            unit="DN",
            time_steps=1,
            stats=VariableStats(min=0.0, max=255.0, p02=0.0, p98=255.0),
        ),
        VariableMeta(
            id="B2",
            name="B2 Blue",
            unit="DN",
            time_steps=1,
            stats=VariableStats(min=0.0, max=255.0, p02=0.0, p98=255.0),
        ),
    ]
    entry.meta.composite_styles = [
        CompositeStyle(
            id="true-color",
            name="True Color",
            description="Natural color",
            bands=["B4", "B3", "B2"],
        )
    ]

    monkeypatch.setattr(
        app.state.planner,
        "plan_tile_request",
        lambda **_kwargs: QueryPlan(
            planner_version="v1",
            collection_id=entry.id,
            request_class="tile",
            chosen_representation="browse",
            execution_path="interactive",
            request_fingerprint="composite",
            response_cache_key="artifact:tile:composite",
            plan_cache_key="plan:composite",
            selected_cube=entry.id,
            selected_path=entry.path,
        ),
    )
    monkeypatch.setattr("app.api.tiles.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)
    composite_calls = {"count": 0}

    def _generate_composite_tile(*_args, **_kwargs):
        composite_calls["count"] += 1
        return b"rgb-bytes", (0.0, 255.0)

    monkeypatch.setattr(
        "app.api.tiles.generate_projected_composite_tile",
        _generate_composite_tile,
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_projected_band_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("single-band renderer should not be used")),
    )
    monkeypatch.setattr(
        "app.api.tiles.generate_browse_tile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browse renderer should not be used")),
    )

    response = client.get("/api/tiles/dataset-1/true-color/6/32/30")
    cached_response = client.get("/api/tiles/dataset-1/true-color/6/32/30")

    assert response.status_code == 200
    assert response.content == b"rgb-bytes"
    assert response.headers["x-representation"] == "serving"
    assert response.headers["x-cache-status"] == "MISS"
    assert response.headers["x-data-vmin"] == "0.0"
    assert response.headers["x-data-vmax"] == "255.0"
    assert cached_response.status_code == 200
    assert cached_response.content == b"rgb-bytes"
    assert cached_response.headers["x-representation"] == "serving"
    assert cached_response.headers["x-cache-status"] == "HIT"
    assert float(response.headers["x-tile-time-ms"]) >= 0
    assert float(response.headers["x-tile-encode-ms"]) >= 0
    assert cached_response.headers["x-object-get-count"] == "0"
    assert cached_response.headers["x-zarr-chunk-count"] == "0"
    assert composite_calls["count"] == 1


def test_oci_tilejson_returns_dynamic_tile_contract(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=4,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]

    monkeypatch.setattr(
        "app.api.tilejson.build_dataset_tilejson",
        lambda *_args, **_kwargs: TileJSON(
            name="example.zarr:NDVI",
            tiles=["http://testserver/api/tiles/dataset-1/NDVI/{z}/{x}/{y}?time_index=0&colormap=red_green"],
            bounds=[30.39, -2.08, 30.81, -1.04],
            minzoom=9,
            maxzoom=18,
            detail_minzoom=9,
            has_coarse_fallback=False,
            coarse_representation=None,
        ),
    )

    response = client.get("/api/tilejson/dataset-1/NDVI?time_index=0&colormap=red_green")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "example.zarr:NDVI"
    assert payload["minzoom"] == 9
    assert payload["maxzoom"] == 18
    assert payload["detail_minzoom"] == 9
    assert payload["has_coarse_fallback"] is False
    assert payload["tiles"][0].endswith("/api/tiles/dataset-1/NDVI/{z}/{x}/{y}?time_index=0&colormap=red_green")


def test_dataset_serving_profile_reports_browser_readiness(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)

    monkeypatch.setattr(
        "app.api.datasets.build_dataset_serving_profile",
        lambda *_args, **_kwargs: {
            "dataset_id": entry.id,
            "zarr_format": 3,
            "zarr_consolidated": True,
            "zarr_proxy_root": "/api/zarr/dataset-1",
            "multiscale_store_path": "multiscale/cubes/example.zarr",
            "multiscale_zarr_format": 3,
            "multiscale_zarr_consolidated": True,
            "multiscale_proxy_root": "/api/zarr/multiscale/dataset-1",
            "multiscale_population_strategy": "prepopulated_then_lazy",
            "multiscale_prepopulated_zoom_max": 12,
            "multiscale_max_zoom": 15,
            "data_array_name": "bands",
            "variable_ids": [],
            "has_multiscale": True,
            "multiscale_paths": ["0"],
            "browse_overview_zoom_levels": [0],
            "browse_overview_max_zoom": 0,
            "browse_coverage": {
                "expected_zoom_levels": list(range(0, 9)),
                "available_zoom_levels": [0],
                "missing_variables": [],
                "missing_time_steps": {},
                "last_generated_at": None,
                "generation_status": "partial",
                "expected_artifact_count": 9,
                "available_artifact_count": 1,
            },
            "chunk_layout": {
                "sharded": True,
                "shard_shape": [1, 1, 4096, 4096],
                "inner_chunk_shape": [1, 1, 256, 256],
            },
            "supported_rendering_modes": ["dynamic_tiles", "proxy_zarr", "multiscale_proxy", "browse_overviews", "multiscale"],
            "browser_multiscale_ready": True,
            "seamless_rendering_ready": False,
            "seamless_rendering_gaps": ["incomplete_browse_overview_coverage"],
        },
    )

    response = client.get("/api/datasets/dataset-1/serving-profile")

    assert response.status_code == 200
    payload = response.json()
    assert payload["browser_multiscale_ready"] is True
    assert payload["browse_overview_zoom_levels"] == [0]
    assert payload["multiscale_proxy_root"] == "/api/zarr/multiscale/dataset-1"
    assert payload["multiscale_population_strategy"] == "prepopulated_then_lazy"
    assert payload["multiscale_prepopulated_zoom_max"] == 12
    assert payload["multiscale_max_zoom"] == 15


def test_dataset_serving_profile_returns_503_when_oci_token_has_expired(monkeypatch) -> None:
    _configure_oci_app_state(monkeypatch)

    monkeypatch.setattr(
        "app.api.datasets.build_dataset_serving_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OCIAuthExpiredError("OCI CLI token has expired. Re-authenticate before starting the backend.")
        ),
    )

    response = client.get("/api/datasets/dataset-1/serving-profile")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "OCI CLI token has expired. Re-authenticate before starting the backend."
    }


def test_healthcheck_reports_oci_auth_status(monkeypatch) -> None:
    auth = SimpleNamespace(
        auth_mode="security_token",
        token_expires_at_epoch=int(time.time()) + 1_200,
    )
    connector = SimpleNamespace(auth=auth)
    monkeypatch.setattr(app.state.settings, "storage_backend", "oci_zarr")
    monkeypatch.setattr(app.state, "storage_connector", connector, raising=False)

    response = client.get("/api/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["oci_auth"]["status"] == "ok"
    assert payload["oci_auth"]["mode"] == "security_token"
    assert payload["oci_auth"]["token_seconds_remaining"] > 0


def test_healthcheck_reports_expired_oci_auth(monkeypatch) -> None:
    class ExpiredConnector:
        @property
        def auth(self):
            raise OCIAuthExpiredError("OCI CLI token has expired. Re-authenticate before starting the backend.")

    monkeypatch.setattr(app.state.settings, "storage_backend", "oci_zarr")
    monkeypatch.setattr(app.state, "storage_connector", ExpiredConnector(), raising=False)

    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json()["oci_auth"] == {
        "status": "expired",
        "mode": app.state.settings.oci_auth_mode,
        "detail": "OCI CLI token has expired. Re-authenticate before starting the backend.",
    }


def test_create_browse_generation_job_runs_and_reports_status(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    monkeypatch.setattr(app.state, "browse_generation_job_store", BrowseGenerationJobStore(), raising=False)
    monkeypatch.setattr("app.api.datasets.ensure_catalog_entry_metadata_ready", lambda entry, _connector: entry)

    def _build(**kwargs):
        kwargs["progress_callback"](True)
        kwargs["progress_callback"](False)
        return {"manifest_path": "browse/cubes/example.zarr/manifest.json", "generated": 1, "reused": 1}

    monkeypatch.setattr("app.api.datasets.build_and_store_browse_overviews", _build)

    accepted = client.post(
        "/api/datasets/dataset-1/browse-generation",
        json={"variables": ["NDVI"], "time_indices": [0], "zoom_levels": [0, 1]},
    )

    assert accepted.status_code == 202
    accepted_payload = accepted.json()
    assert accepted_payload["dataset_id"] == "dataset-1"
    assert accepted_payload["total_artifacts"] == 2

    status = client.get(f"/api/datasets/dataset-1/browse-generation/{accepted_payload['job_id']}")

    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["status"] == "succeeded"
    assert status_payload["progress"] == 1.0
    assert status_payload["generated_artifacts"] == 1
    assert status_payload["reused_artifacts"] == 1
    assert status_payload["manifest_path"] == "browse/cubes/example.zarr/manifest.json"
    assert status_payload["can_retry"] is False


def test_create_browse_generation_job_reuses_active_duplicate(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    monkeypatch.setattr(app.state, "browse_generation_job_store", BrowseGenerationJobStore(), raising=False)
    monkeypatch.setattr("app.api.datasets.ensure_catalog_entry_metadata_ready", lambda entry, _connector: entry)
    scheduled_tasks = []

    def _capture_task(self, func, *args, **kwargs):
        scheduled_tasks.append((func, args, kwargs))

    monkeypatch.setattr("app.api.datasets.BackgroundTasks.add_task", _capture_task)

    first = client.post(
        "/api/datasets/dataset-1/browse-generation",
        json={"variables": ["NDVI", "NDVI"], "time_indices": [0, 0], "zoom_levels": [1, 0, 1]},
    )
    duplicate = client.post(
        "/api/datasets/dataset-1/browse-generation",
        json={"variables": ["NDVI"], "time_indices": [0], "zoom_levels": [0, 1]},
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert len(scheduled_tasks) == 1

    status = client.get(f"/api/datasets/dataset-1/browse-generation/{first.json()['job_id']}")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert status.json()["variables"] == ["NDVI"]
    assert status.json()["time_indices"] == [0]
    assert status.json()["zoom_levels"] == [0, 1]
    assert status.json()["total_artifacts"] == 2


def test_browse_generation_job_reports_failure_and_retry_state(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
    entry.meta.variables = [
        VariableMeta(
            id="NDVI",
            name="NDVI",
            unit="1",
            time_steps=1,
            stats=VariableStats(min=0.0, max=1.0, p02=0.1, p98=0.9),
        )
    ]
    entry.band_names = ["NDVI"]
    entry.band_indices = {"NDVI": 0}
    monkeypatch.setattr(app.state, "browse_generation_job_store", BrowseGenerationJobStore(), raising=False)
    monkeypatch.setattr("app.api.datasets.ensure_catalog_entry_metadata_ready", lambda entry, _connector: entry)

    def _fail(**_kwargs):
        raise RuntimeError("browse build failed")

    monkeypatch.setattr("app.api.datasets.build_and_store_browse_overviews", _fail)

    accepted = client.post(
        "/api/datasets/dataset-1/browse-generation",
        json={"variables": ["NDVI"], "time_indices": [0], "zoom_levels": [0]},
    )
    failed = client.get(f"/api/datasets/dataset-1/browse-generation/{accepted.json()['job_id']}")

    retry = client.post(
        "/api/datasets/dataset-1/browse-generation",
        json={
            "variables": ["NDVI"],
            "time_indices": [0],
            "zoom_levels": [0],
            "retry_job_id": accepted.json()["job_id"],
        },
    )
    retry_status = client.get(f"/api/datasets/dataset-1/browse-generation/{retry.json()['job_id']}")

    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["can_retry"] is True
    assert failed.json()["error_message"] == "browse build failed"
    assert retry.status_code == 202
    assert retry_status.json()["attempt"] == 2
    assert retry_status.json()["retry_of_job_id"] == accepted.json()["job_id"]
