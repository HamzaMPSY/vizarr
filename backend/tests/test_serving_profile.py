from types import SimpleNamespace

from app.core.dataset_catalog import CatalogEntry
from app.core.serving_profile import build_dataset_serving_profile
from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.models.dataset import DatasetMeta


class _Connector:
    def __init__(self, payloads: dict[str, str]) -> None:
        self.payloads = payloads

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket/{object_path.lstrip('/')}"

    def read_text(self, object_path: str, *, use_cache: bool = True) -> str:
        try:
            return self.payloads[object_path]
        except KeyError as exc:
            raise FileNotFoundError(object_path) from exc


def _entry() -> CatalogEntry:
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
        ),
        zarr_format=3,
        consolidated=True,
        data_array_name="bands",
        band_array_name="band",
        band_names=["NDVI"],
        band_indices={"NDVI": 0},
        data_array_meta=ZarrV3ArrayMetadata(
            shape=(4, 1, 100, 200),
            chunk_shape=(1, 1, 4096, 4096),
            data_type="float32",
            fill_value=None,
            codecs=[
                {
                    "name": "sharding_indexed",
                    "configuration": {
                        "chunk_shape": [1, 1, 256, 256],
                        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
                        "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
                        "index_location": "end",
                    },
                }
            ],
            separator="/",
            attributes={"band_labels": ["NDVI"]},
            dimension_names=("time", "band", "y", "x"),
        ),
    )


def test_build_dataset_serving_profile_flags_missing_multiscale_and_partial_browse(monkeypatch) -> None:
    entry = _entry()
    connector = _Connector({})
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.serving_profile.read_browse_manifest", lambda *_args, **_kwargs: {
        "variables": {"NDVI": {"overviews": {"0": {"levels": {"0": {"path": "browse/cubes/example.zarr/overviews/NDVI-0-z0.npz"}}}}}}
    })
    monkeypatch.setattr("app.core.serving_profile.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    profile = build_dataset_serving_profile(settings, connector, entry)

    assert profile.dataset_id == "dataset-1"
    assert profile.has_multiscale is False
    assert profile.browse_overview_zoom_levels == [0]
    assert profile.chunk_layout is not None
    assert profile.chunk_layout.sharded is True
    assert profile.chunk_layout.shard_shape == [1, 1, 4096, 4096]
    assert profile.chunk_layout.inner_chunk_shape == [1, 1, 256, 256]
    assert profile.browser_multiscale_ready is False
    assert profile.seamless_rendering_ready is False
    assert "missing_multiscale_pyramid" in profile.seamless_rendering_gaps
    assert "incomplete_browse_overview_coverage" in profile.seamless_rendering_gaps


def test_build_dataset_serving_profile_accepts_multiscale_proxy_layout(monkeypatch) -> None:
    entry = _entry()
    entry.meta.time_values = ["2025-01-08"]
    entry.meta.multiscale_store_path = "multiscale/cubes/example.zarr"
    entry.meta.multiscale_zarr_format = 2
    entry.meta.multiscale_zarr_consolidated = True
    entry.meta.multiscale_proxy_root = "/api/zarr/multiscale/dataset-1"
    entry.meta.multiscale_population_strategy = "prepopulated_then_lazy"
    entry.meta.multiscale_prepopulated_zoom_max = 12
    entry.meta.multiscale_max_zoom = 15
    connector = _Connector(
        {
            "oci://bucket/multiscale/cubes/example.zarr/.zmetadata": (
                '{"metadata":{".zattrs":{"multiscales":[{"datasets":[{"path":"0"},{"path":"1"}]}],"population_strategy":"prepopulated_then_lazy","prepopulated_zoom_max":12,"max_zoom":15},'
                '"0/bands/.zarray":{"shape":[1,1,512,768],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"},'
                '"1/bands/.zarray":{"shape":[1,1,256,512],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"}}}'
            ),
        }
    )
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.serving_profile.read_browse_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.serving_profile.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    profile = build_dataset_serving_profile(settings, connector, entry)

    assert profile.has_multiscale is True
    assert profile.multiscale_paths == ["0", "1"]
    assert profile.multiscale_proxy_root == "/api/zarr/multiscale/dataset-1"
    assert profile.multiscale_population_strategy == "prepopulated_then_lazy"
    assert profile.multiscale_prepopulated_zoom_max == 12
    assert profile.multiscale_max_zoom == 15
    assert profile.browser_multiscale_ready is True
    assert profile.seamless_rendering_ready is True


def test_build_dataset_serving_profile_accepts_unconsolidated_multiscale_proxy_layout(monkeypatch) -> None:
    entry = _entry()
    entry.meta.time_values = ["2025-01-08"]
    entry.meta.multiscale_store_path = "multiscale/cubes/example.zarr"
    entry.meta.multiscale_zarr_format = 2
    entry.meta.multiscale_zarr_consolidated = False
    entry.meta.multiscale_proxy_root = "/api/zarr/multiscale/dataset-1"
    connector = _Connector(
        {
            "oci://bucket/multiscale/cubes/example.zarr/.zgroup": '{"zarr_format":2}',
            "oci://bucket/multiscale/cubes/example.zarr/.zattrs": (
                '{"multiscales":[{"datasets":[{"path":"9"},{"path":"10"}]}],"population_strategy":"prepopulated_then_lazy","prepopulated_zoom_max":12,"max_zoom":17}'
            ),
            "oci://bucket/multiscale/cubes/example.zarr/9/bands/.zarray": (
                '{"shape":[1,1,512,768],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"}'
            ),
            "oci://bucket/multiscale/cubes/example.zarr/10/bands/.zarray": (
                '{"shape":[1,1,1024,1536],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"}'
            ),
        }
    )
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.serving_profile.read_browse_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.serving_profile.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    profile = build_dataset_serving_profile(settings, connector, entry)

    assert profile.has_multiscale is True
    assert profile.multiscale_paths == ["9", "10"]
    assert profile.browser_multiscale_ready is True


def test_build_dataset_serving_profile_rejects_multiscale_store_that_browser_cannot_read(monkeypatch) -> None:
    entry = _entry()
    entry.meta.multiscale_store_path = "multiscale/cubes/example.zarr"
    entry.meta.multiscale_zarr_format = 3
    entry.meta.multiscale_zarr_consolidated = True
    entry.meta.multiscale_proxy_root = "/api/zarr/multiscale/dataset-1"
    connector = _Connector(
        {
            "oci://bucket/multiscale/cubes/example.zarr/zarr.json": (
                '{"zarr_format":3,"attributes":{"multiscales":[{"datasets":[{"path":"0"}]}]},'
                '"consolidated_metadata":{"kind":"inline","must_understand":false,"metadata":{}}}'
            ),
        }
    )
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.serving_profile.read_browse_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.serving_profile.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    profile = build_dataset_serving_profile(settings, connector, entry)

    assert profile.has_multiscale is True
    assert profile.browser_multiscale_ready is False
    assert profile.seamless_rendering_ready is False
    assert "multiscale_store_not_browser_readable" in profile.seamless_rendering_gaps


def test_build_dataset_serving_profile_rejects_multiscale_store_with_incomplete_time_coverage(monkeypatch) -> None:
    entry = _entry()
    entry.meta.time_values = ["2025-01-08", "2025-01-15", "2025-01-22", "2025-01-29"]
    entry.meta.multiscale_store_path = "multiscale/cubes/example.zarr"
    entry.meta.multiscale_zarr_format = 2
    entry.meta.multiscale_zarr_consolidated = True
    entry.meta.multiscale_proxy_root = "/api/zarr/multiscale/dataset-1"
    connector = _Connector(
        {
            "oci://bucket/multiscale/cubes/example.zarr/.zmetadata": (
                '{"metadata":{".zattrs":{"multiscales":[{"datasets":[{"path":"0"},{"path":"1"}]}],"population_strategy":"prepopulated_then_lazy","prepopulated_zoom_max":12,"max_zoom":15},'
                '"0/bands/.zarray":{"shape":[1,1,512,768],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"},'
                '"1/bands/.zarray":{"shape":[1,1,256,512],"chunks":[1,1,256,256],"dtype":"<f4","compressor":null,"filters":null,"order":"C"}}}'
            ),
        }
    )
    settings = SimpleNamespace(browse_tile_max_zoom=8)

    monkeypatch.setattr("app.core.serving_profile.read_browse_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.serving_profile.ensure_catalog_entry_metadata_ready", lambda current_entry, _connector: current_entry)

    profile = build_dataset_serving_profile(settings, connector, entry)

    assert profile.has_multiscale is True
    assert profile.browser_multiscale_ready is False
    assert profile.seamless_rendering_ready is False
    assert "multiscale_store_not_browser_readable" in profile.seamless_rendering_gaps
