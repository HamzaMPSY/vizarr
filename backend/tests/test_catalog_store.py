from types import SimpleNamespace

from app.index.catalog_store import build_index_records
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats


def test_build_index_records_uses_loaded_manifest_without_catalog_scan(monkeypatch) -> None:
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
            zarr_proxy_root="/api/zarr/dataset-1",
        )
    ]
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                storage_backend="oci_zarr",
                browse_enabled_styles="",
                planner_version="v1",
            ),
            storage_connector=object(),
            dataset_catalog=None,
            dataset_manifest=manifest,
            registry=None,
        )
    )
    monkeypatch.setattr(
        "app.index.catalog_store.get_or_build_catalog",
        lambda _app: (_ for _ in ()).throw(AssertionError("catalog scan should not run")),
    )

    records = build_index_records(app, allow_catalog_build=False)

    assert {item.representation for item in records} == {"browse", "source", "serving"}
    assert {item.collection_id for item in records} == {"dataset-1"}
    assert all(item.bands == ("B1",) for item in records)
