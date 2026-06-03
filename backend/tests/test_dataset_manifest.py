from types import SimpleNamespace

from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import build_catalog_index
from app.core.dataset_catalog import build_dataset_manifest
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import has_direct_store_target
from app.core.dataset_catalog import warm_catalog_index
from app.core.oci_object_storage import ZarrStoreSummary
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta


def test_build_dataset_manifest_returns_detached_meta_copies() -> None:
    catalog = {
        "dataset-1": CatalogEntry(
            id="dataset-1",
            path="cubes/example.zarr",
            meta=DatasetMeta(
                id="dataset-1",
                name="example.zarr",
                description="Example dataset",
                variables=[],
            ),
            zarr_format=3,
            consolidated=False,
            data_array_name="bands",
            band_array_name="band",
            band_names=[],
            band_indices={},
        )
    }

    manifest = build_dataset_manifest(catalog)

    assert len(manifest) == 1
    assert manifest[0].id == "dataset-1"
    assert manifest[0] is not catalog["dataset-1"].meta


def test_warm_catalog_index_populates_catalog_and_manifest(monkeypatch) -> None:
    catalog = {
        "dataset-1": CatalogEntry(
            id="dataset-1",
            path="cubes/example.zarr",
            meta=DatasetMeta(
                id="dataset-1",
                name="example.zarr",
                description="Example dataset",
                variables=[],
            ),
            zarr_format=3,
            consolidated=False,
            data_array_name="bands",
            band_array_name="band",
            band_names=[],
            band_indices={},
        )
    }
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=object(),
            storage_connector=object(),
            dataset_catalog=None,
            dataset_manifest=None,
        )
    )

    monkeypatch.setattr("app.core.dataset_catalog.build_catalog_index", lambda **_kwargs: catalog)

    result = warm_catalog_index(app)

    assert result is catalog
    assert app.state.dataset_catalog is catalog
    assert app.state.dataset_manifest is not None
    assert [item.id for item in app.state.dataset_manifest] == ["dataset-1"]


def test_warm_catalog_index_can_eagerly_ready_entries(monkeypatch) -> None:
    catalog = {
        "dataset-1": CatalogEntry(
            id="dataset-1",
            path="cubes/example.zarr",
            meta=DatasetMeta(
                id="dataset-1",
                name="example.zarr",
                description="Example dataset",
                variables=[],
            ),
            zarr_format=3,
            consolidated=False,
            data_array_name="bands",
            band_array_name="band",
            band_names=[],
            band_indices={},
        )
    }
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=object(),
            storage_connector=object(),
            dataset_catalog=None,
            dataset_manifest=None,
        )
    )

    monkeypatch.setattr("app.core.dataset_catalog.build_catalog_index", lambda **_kwargs: catalog)
    monkeypatch.setattr(
        "app.core.dataset_catalog.ensure_catalog_entry_ready",
        lambda entry, _connector: entry.meta.variables.append(
            VariableMeta(
                id="band-1",
                name="Band 1",
                unit="DN",
                time_steps=1,
                stats={
                    "min": 0.0,
                    "max": 1.0,
                    "p02": 0.0,
                    "p98": 1.0,
                },
            )
        ),
    )

    result = warm_catalog_index(app, eager_entry_state=True)

    assert result is catalog
    assert len(app.state.dataset_manifest[0].variables) == 1


def test_ensure_catalog_entry_metadata_ready_does_not_load_coordinates(monkeypatch) -> None:
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
        consolidated=False,
        data_array_name="bands",
        band_array_name="band",
        band_names=[],
        band_indices={},
    )

    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda **_kwargs: (
            {},
            {
                "bands": {"shape": [1, 2, 4, 4], "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}}, "data_type": "uint16", "codecs": [], "attributes": {}, "dimension_names": ["time", "band", "y", "x"]},
                "band": {"shape": [2], "chunk_grid": {"configuration": {"chunk_shape": [2]}}, "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}}, "codecs": [], "attributes": {}, "dimension_names": ["band"]},
                "x": {"shape": [4], "chunk_grid": {"configuration": {"chunk_shape": [4]}}, "data_type": "float32", "codecs": [], "attributes": {}, "dimension_names": ["x"]},
                "y": {"shape": [4], "chunk_grid": {"configuration": {"chunk_shape": [4]}}, "data_type": "float32", "codecs": [], "attributes": {}, "dimension_names": ["y"]},
            },
        ),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_fixed_length_utf32_labels",
        lambda **_kwargs: ["B1", "B2"],
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog._sample_band_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("band stats should not be sampled interactively")),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_1d_numeric_array",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("coordinate arrays should not be loaded")),
    )

    ensure_catalog_entry_metadata_ready(entry, connector=object())  # type: ignore[arg-type]

    assert [item.id for item in entry.meta.variables] == ["B1", "B2"]
    assert entry.meta.variables[0].stats.p02 == 0.019999999552965164
    assert entry.meta.variables[0].stats.p98 == 0.9800000190734863


def test_ensure_catalog_entry_metadata_ready_rebuilds_variables_when_band_names_exist(monkeypatch) -> None:
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
        consolidated=False,
        data_array_name="bands",
        band_array_name="band",
        band_names=["B1", "B2"],
        band_indices={"B1": 0, "B2": 1},
    )

    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda **_kwargs: (
            {},
            {
                "bands": {"shape": [1, 2, 4, 4], "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}}, "data_type": "uint16", "codecs": [], "attributes": {}, "dimension_names": ["time", "band", "y", "x"]},
                "band": {"shape": [2], "chunk_grid": {"configuration": {"chunk_shape": [2]}}, "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}}, "codecs": [], "attributes": {}, "dimension_names": ["band"]},
                "x": {"shape": [4], "chunk_grid": {"configuration": {"chunk_shape": [4]}}, "data_type": "float32", "codecs": [], "attributes": {}, "dimension_names": ["x"]},
                "y": {"shape": [4], "chunk_grid": {"configuration": {"chunk_shape": [4]}}, "data_type": "float32", "codecs": [], "attributes": {}, "dimension_names": ["y"]},
            },
        ),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_fixed_length_utf32_labels",
        lambda **_kwargs: ["B1", "B2"],
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog._sample_band_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("band stats should not be sampled interactively")),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_1d_numeric_array",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("coordinate arrays should not be loaded")),
    )

    ensure_catalog_entry_metadata_ready(entry, connector=object())  # type: ignore[arg-type]

    assert [item.id for item in entry.meta.variables] == ["B1", "B2"]


def test_build_catalog_index_skips_unsupported_store(monkeypatch) -> None:
    connector = SimpleNamespace()
    settings = SimpleNamespace(oci_prefix="cubes")

    monkeypatch.setattr(
        connector,
        "list_zarr_stores",
        lambda **_kwargs: [
            ZarrStoreSummary(path="cubes/good.zarr", consolidated=False, zarr_format=3),
            ZarrStoreSummary(path="cubes/bad.zarr", consolidated=False, zarr_format=3),
        ],
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda *, store_path, **_kwargs: (
            {},
            {
                "cube": {
                    "shape": [1, 2, 4, 4],
                    "dimension_names": ["time", "bandish", "y", "x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}},
                    "data_type": "uint16",
                    "codecs": [],
                    "attributes": {},
                },
                "bandish": {
                    "shape": [2],
                    "dimension_names": ["bandish"],
                    "chunk_grid": {"configuration": {"chunk_shape": [2]}},
                    "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}},
                    "codecs": [],
                    "attributes": {},
                },
                "x": {
                    "shape": [4],
                    "dimension_names": ["x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
                "y": {
                    "shape": [4],
                    "dimension_names": ["y"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
            } if store_path.endswith("good.zarr") else {
                "value": {
                    "shape": [4, 4],
                    "dimension_names": ["row", "column"],
                    "chunk_grid": {"configuration": {"chunk_shape": [2, 2]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                }
            },
        ),
    )

    catalog = build_catalog_index(settings=settings, connector=connector)  # type: ignore[arg-type]

    assert len(catalog) == 1
    assert next(iter(catalog.values())).path == "cubes/good.zarr"


def test_build_catalog_index_accepts_direct_store_prefix_without_listing(monkeypatch) -> None:
    connector = SimpleNamespace()
    settings = SimpleNamespace(
        oci_prefix="cubes/example.zarr",
        oci_zarr_path="",
    )

    monkeypatch.setattr(
        connector,
        "list_zarr_stores",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct store path should skip prefix listing")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda *, store_path, **_kwargs: (
            {"zarr_format": 3, "consolidated_metadata": {"metadata": {}}},
            {
                "cube": {
                    "shape": [1, 2, 4, 4],
                    "dimension_names": ["time", "bandish", "y", "x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}},
                    "data_type": "uint16",
                    "codecs": [],
                    "attributes": {},
                },
                "bandish": {
                    "shape": [2],
                    "dimension_names": ["bandish"],
                    "chunk_grid": {"configuration": {"chunk_shape": [2]}},
                    "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}},
                    "codecs": [],
                    "attributes": {},
                },
                "x": {
                    "shape": [4],
                    "dimension_names": ["x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
                "y": {
                    "shape": [4],
                    "dimension_names": ["y"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
            },
        ),
    )

    catalog = build_catalog_index(settings=settings, connector=connector)  # type: ignore[arg-type]

    assert len(catalog) == 1
    assert next(iter(catalog.values())).path == "cubes/example.zarr"


def test_build_catalog_index_tolerates_missing_time_coordinate_chunk(monkeypatch) -> None:
    connector = SimpleNamespace()
    settings = SimpleNamespace(
        oci_prefix="cubes/example.zarr",
        oci_zarr_path="",
    )

    monkeypatch.setattr(
        connector,
        "list_zarr_stores",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct store path should skip prefix listing")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda *, store_path, **_kwargs: (
            {"zarr_format": 3, "consolidated_metadata": {"metadata": {}}},
            {
                "bands": {
                    "shape": [1, 2, 4, 4],
                    "dimension_names": ["time", "band", "y", "x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}},
                    "data_type": "uint16",
                    "codecs": [],
                    "attributes": {},
                },
                "band": {
                    "shape": [2],
                    "dimension_names": ["band"],
                    "chunk_grid": {"configuration": {"chunk_shape": [2]}},
                    "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}},
                    "codecs": [],
                    "attributes": {},
                },
                "time": {
                    "shape": [1],
                    "dimension_names": ["time"],
                    "chunk_grid": {"configuration": {"chunk_shape": [1]}},
                    "data_type": "int64",
                    "codecs": [],
                    "attributes": {},
                },
                "x": {
                    "shape": [4],
                    "dimension_names": ["x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
                "y": {
                    "shape": [4],
                    "dimension_names": ["y"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
            },
        ),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.load_1d_numeric_array",
        lambda **_kwargs: (_ for _ in ()).throw(FileNotFoundError("time/c/0")),
    )

    catalog = build_catalog_index(settings=settings, connector=connector)  # type: ignore[arg-type]

    entry = next(iter(catalog.values()))
    assert len(catalog) == 1
    assert entry.path == "cubes/example.zarr"
    assert entry.meta.time_values is None


def test_has_direct_store_target_detects_direct_prefix() -> None:
    settings = SimpleNamespace(oci_prefix="cubes/example.zarr", oci_zarr_path="")

    assert has_direct_store_target(settings) is True


def test_has_direct_store_target_detects_explicit_zarr_path() -> None:
    settings = SimpleNamespace(oci_prefix="cubes", oci_zarr_path="oci://bucket@namespace/cubes/example.zarr")

    assert has_direct_store_target(settings) is True


def test_build_catalog_index_attaches_multiscale_store_metadata(monkeypatch) -> None:
    connector = SimpleNamespace()
    settings = SimpleNamespace(
        oci_prefix="cubes/example.zarr",
        oci_zarr_path="",
        oci_multiscale_prefix_root="multiscale",
    )

    monkeypatch.setattr(
        connector,
        "list_zarr_stores",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct store path should skip prefix listing")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.read_consolidated_metadata",
        lambda *, store_path, **_kwargs: (
            {"zarr_format": 3, "consolidated_metadata": {"metadata": {}}},
            {
                "cube": {
                    "shape": [1, 2, 4, 4],
                    "dimension_names": ["time", "bandish", "y", "x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [1, 1, 2, 2]}},
                    "data_type": "uint16",
                    "codecs": [],
                    "attributes": {},
                },
                "bandish": {
                    "shape": [2],
                    "dimension_names": ["bandish"],
                    "chunk_grid": {"configuration": {"chunk_shape": [2]}},
                    "data_type": {"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}},
                    "codecs": [],
                    "attributes": {},
                },
                "x": {
                    "shape": [4],
                    "dimension_names": ["x"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
                "y": {
                    "shape": [4],
                    "dimension_names": ["y"],
                    "chunk_grid": {"configuration": {"chunk_shape": [4]}},
                    "data_type": "float32",
                    "codecs": [],
                    "attributes": {},
                },
            },
        ),
    )
    monkeypatch.setattr(
        "app.core.dataset_catalog.probe_multiscale_store",
        lambda **_kwargs: SimpleNamespace(
            path="multiscale/cubes/example.zarr",
            zarr_format=2,
            consolidated=True,
            population_strategy="prepopulated_then_lazy",
            prepopulated_zoom_max=12,
            max_zoom=15,
        ),
    )

    catalog = build_catalog_index(settings=settings, connector=connector)  # type: ignore[arg-type]

    entry = next(iter(catalog.values()))
    assert entry.meta.multiscale_store_path == "multiscale/cubes/example.zarr"
    assert entry.meta.multiscale_zarr_format == 2
    assert entry.meta.multiscale_zarr_consolidated is True
    assert entry.meta.multiscale_proxy_root == f"/api/zarr/multiscale/{entry.id}"
    assert entry.meta.multiscale_population_strategy == "prepopulated_then_lazy"
    assert entry.meta.multiscale_prepopulated_zoom_max == 12
    assert entry.meta.multiscale_max_zoom == 15
