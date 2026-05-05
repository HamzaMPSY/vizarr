from fastapi.testclient import TestClient

from app.core.dataset_catalog import CatalogEntry
from app.core.oci_auth import OCIAuthExpiredError
from app.core.oci_object_storage import OCIObjectInfo
from app.main import app
from app.models.dataset import DatasetMeta
from app.models.dataset import CompositeStyle
from app.models.dataset import TileJSON
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats
from app.models.plans import QueryPlan


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
    assert response.headers["x-representation"] == "serving"
    assert len(response.content) > 100


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


def test_oci_tile_serves_composite_style(monkeypatch) -> None:
    entry = _configure_oci_app_state(monkeypatch)
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
