from app.core.zarr_reader import _build_open_zarr_kwargs


class _StubConnector:
    def __init__(self, payloads: dict[str, str]) -> None:
        self._payloads = payloads

    def get_filesystem(self):
        raise AssertionError("filesystem access is not expected in this helper test")


def test_build_open_zarr_kwargs_uses_requested_consolidation_for_v2(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: None,
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": True,
    }


def test_build_open_zarr_kwargs_for_v3_without_consolidated_metadata_forces_false(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: {"zarr_format": 3},
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": False,
        "zarr_version": 3,
        "zarr_format": 3,
    }


def test_build_open_zarr_kwargs_for_v3_with_consolidated_metadata_preserves_true(monkeypatch) -> None:
    connector = _StubConnector({})

    monkeypatch.setattr(
        "app.core.zarr_reader._read_root_zarr_json",
        lambda **_kwargs: {"zarr_format": 3, "consolidated_metadata": {"metadata": {}}},
    )

    assert _build_open_zarr_kwargs(connector=connector, zarr_path="oci://bucket/test.zarr", consolidated=True) == {
        "consolidated": True,
        "zarr_version": 3,
        "zarr_format": 3,
    }
