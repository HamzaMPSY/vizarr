from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.browse_tiles import browse_overview_exists
from app.core.browse_tiles import prewarm_browse_overviews
from app.core.dataset_catalog import CatalogEntry
from app.models.dataset import DatasetMeta
from app.models.dataset import VariableMeta


def _entry() -> CatalogEntry:
    return CatalogEntry(
        id="dataset-1",
        path="cubes/example.zarr",
        meta=DatasetMeta(
            id="dataset-1",
            name="example.zarr",
            description="Example dataset",
            variables=[
                VariableMeta(
                    id="B1",
                    name="B1",
                    unit="DN",
                    time_steps=1,
                    stats={"min": 0.0, "max": 1.0, "p02": 0.0, "p98": 1.0},
                ),
                VariableMeta(
                    id="B2",
                    name="B2",
                    unit="DN",
                    time_steps=1,
                    stats={"min": 0.0, "max": 1.0, "p02": 0.0, "p98": 1.0},
                ),
            ],
        ),
        zarr_format=3,
        consolidated=False,
        data_array_name="bands",
        band_array_name="band",
        band_names=["B1", "B2"],
        band_indices={"B1": 0, "B2": 1},
    )


def test_prewarm_browse_overviews_warms_first_variable_only_by_default(monkeypatch) -> None:
    entry = _entry()
    warmed: list[str] = []

    monkeypatch.setattr("app.core.browse_tiles.browse_overview_exists", lambda **_kwargs: False)
    monkeypatch.setattr(
        "app.core.browse_tiles.get_or_create_browse_overview",
        lambda **kwargs: warmed.append(kwargs["variable"]) or (np.zeros((1, 1), dtype=np.float32), (0.0, 0.0, 1.0, 1.0)),
    )

    count = prewarm_browse_overviews(
        SimpleNamespace(),
        object(),  # type: ignore[arg-type]
        {"dataset-1": entry},
    )

    assert count == 1
    assert warmed == ["B1"]


def test_prewarm_browse_overviews_can_warm_all_variables(monkeypatch) -> None:
    entry = _entry()
    warmed: list[str] = []

    monkeypatch.setattr("app.core.browse_tiles.browse_overview_exists", lambda **_kwargs: False)
    monkeypatch.setattr(
        "app.core.browse_tiles.get_or_create_browse_overview",
        lambda **kwargs: warmed.append(kwargs["variable"]) or (np.zeros((1, 1), dtype=np.float32), (0.0, 0.0, 1.0, 1.0)),
    )

    count = prewarm_browse_overviews(
        SimpleNamespace(),
        object(),  # type: ignore[arg-type]
        {"dataset-1": entry},
        all_variables=True,
    )

    assert count == 2
    assert warmed == ["B1", "B2"]


def test_browse_overview_exists_checks_disk_cache(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        browse_local_cache_dir=str(tmp_path),
        planner_version="v1",
        browse_overview_max_size=1536,
    )
    entry = _entry()
    cache_dir = tmp_path / entry.id
    cache_dir.mkdir(parents=True)
    cache_file = next(iter(cache_dir.glob("*.npz")), None)
    assert cache_file is None

    digest = "ignored"
    path = cache_dir / f"B1-0-{digest}.npz"
    np.savez_compressed(path, data=np.zeros((1, 1), dtype=np.float32), bbox=np.asarray([0.0, 0.0, 1.0, 1.0]))

    monkeypatch_path = path
    # Align the helper's computed path with the test file.
    from app.core import browse_tiles as browse_tiles_module

    original = browse_tiles_module._overview_cache_path
    browse_tiles_module._overview_cache_path = lambda *_args, **_kwargs: monkeypatch_path
    try:
        assert browse_overview_exists(settings=settings, entry=entry, variable="B1", time_index=0) is True
    finally:
        browse_tiles_module._overview_cache_path = original
