import numpy as np
import numcodecs

from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.core.zarr_v3 import _decode_bytes
from app.core.zarr_v3 import _resolved_chunk_length
from app.core.zarr_v3 import build_chunk_object_path
from app.core.zarr_v3 import load_4d_chunk
from app.core.zarr_v3 import load_4d_window


def test_build_chunk_object_path_uses_v3_separator() -> None:
    object_path = build_chunk_object_path(
        store_path="cubes/example.zarr",
        array_name="bands",
        separator="/",
        chunk_indices=(0, 1, 2, 3),
    )
    assert object_path == "cubes/example.zarr/bands/c/0/1/2/3"


def test_decode_bytes_handles_zstd_pipeline() -> None:
    payload = np.arange(8, dtype=np.uint16).tobytes()
    encoded = numcodecs.Zstd(level=0, checksum=False).encode(payload)
    decoded = _decode_bytes(
        encoded,
        [
            {"name": "bytes", "configuration": {"endian": "little"}},
            {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
        ],
    )
    assert decoded == payload


def test_resolved_chunk_length_handles_edge_chunk() -> None:
    assert _resolved_chunk_length(size=7741, chunk_size=512, chunk_index=15) == 61


def test_load_4d_window_reassembles_requested_area(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(1, 1, 4, 4),
        chunk_shape=(1, 1, 2, 2),
        data_type="uint16",
        fill_value=0,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    chunks = {
        (0, 0, 0, 0): np.array([[[[1, 2], [5, 6]]]], dtype=np.uint16),
        (0, 0, 0, 1): np.array([[[[3, 4], [7, 8]]]], dtype=np.uint16),
        (0, 0, 1, 0): np.array([[[[9, 10], [13, 14]]]], dtype=np.uint16),
        (0, 0, 1, 1): np.array([[[[11, 12], [15, 16]]]], dtype=np.uint16),
    }

    def fake_load_4d_chunk(*, chunk_indices, **_kwargs):
        return chunks[chunk_indices]

    monkeypatch.setattr("app.core.zarr_v3.load_4d_chunk", fake_load_4d_chunk)

    window = load_4d_window(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        time_index=0,
        band_index=0,
        y_start=1,
        y_stop=4,
        x_start=1,
        x_stop=4,
    )

    assert window.tolist() == [
        [6, 7, 8],
        [10, 11, 12],
        [14, 15, 16],
    ]


def test_load_4d_chunk_accepts_full_edge_chunk_payload(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(1, 1, 7741, 7611),
        chunk_shape=(1, 1, 512, 512),
        data_type="uint16",
        fill_value=0,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    full_chunk = np.arange(1 * 1 * 512 * 512, dtype=np.uint16).reshape((1, 1, 512, 512))

    monkeypatch.setattr(
        "app.core.zarr_v3._read_chunk_bytes",
        lambda **_kwargs: full_chunk.tobytes(),
    )

    chunk = load_4d_chunk(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        chunk_indices=(0, 0, 0, 14),
    )

    assert chunk.shape == (1, 1, 512, 512)
