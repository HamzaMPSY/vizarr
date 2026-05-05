import numpy as np
import numcodecs

from app.core.zarr_v3 import ZarrV3ArrayMetadata
from app.core.zarr_v3 import _crc32c
from app.core.zarr_v3 import _decode_bytes
from app.core.zarr_v3 import _resolved_chunk_length
from app.core.zarr_v3 import build_chunk_object_path
from app.core.zarr_v3 import estimate_4d_nonempty_pixel_bounds
from app.core.zarr_v3 import estimate_4d_present_shard_pixel_bounds
from app.core.zarr_v3 import load_1d_numeric_array
from app.core.zarr_v3 import load_2d_chunk
from app.core.zarr_v3 import load_2d_window
from app.core.zarr_v3 import load_4d_chunk
from app.core.zarr_v3 import load_4d_window
from app.core.zarr_v3 import load_fixed_length_utf32_labels


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


def test_decode_bytes_handles_crc32c_pipeline() -> None:
    payload = b"vizarr-shard-index"
    checksum = _crc32c(payload).to_bytes(4, byteorder="little", signed=False)
    decoded = _decode_bytes(
        payload + checksum,
        [
            {"name": "bytes", "configuration": {"endian": "little"}},
            {"name": "crc32c"},
        ],
    )
    assert decoded == payload


def test_resolved_chunk_length_handles_edge_chunk() -> None:
    assert _resolved_chunk_length(size=7741, chunk_size=512, chunk_index=15) == 61


def test_load_1d_numeric_array_reassembles_multiple_chunks(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(5,),
        chunk_shape=(2,),
        data_type="float32",
        fill_value=None,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("x",),
    )

    chunks = {
        (0,): np.asarray([1.0, 2.0], dtype=np.float32).tobytes(),
        (1,): np.asarray([3.0, 4.0], dtype=np.float32).tobytes(),
        (2,): np.asarray([5.0], dtype=np.float32).tobytes(),
    }

    monkeypatch.setattr(
        "app.core.zarr_v3._read_array_chunk_bytes",
        lambda *, chunk_indices, **_kwargs: chunks[chunk_indices],
    )

    values = load_1d_numeric_array(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="x",
        metadata=metadata,
    )

    assert values.tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_load_fixed_length_utf32_labels_reassembles_multiple_chunks(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(3,),
        chunk_shape=(2,),
        data_type={"name": "fixed_length_utf32", "configuration": {"length_bytes": 8}},
        fill_value=None,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("band",),
    )

    def encode(values: list[str]) -> bytes:
        return "".join(value.ljust(2, "\x00") for value in values).encode("utf-32-le")

    chunks = {
        (0,): encode(["B1", "B2"]),
        (1,): encode(["B3"]),
    }

    monkeypatch.setattr(
        "app.core.zarr_v3._read_array_chunk_bytes",
        lambda *, chunk_indices, **_kwargs: chunks[chunk_indices],
    )

    labels = load_fixed_length_utf32_labels(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="band",
        metadata=metadata,
    )

    assert labels == ["B1", "B2", "B3"]


def test_load_2d_window_reassembles_requested_area(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(4, 4),
        chunk_shape=(2, 2),
        data_type="uint16",
        fill_value=0,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("y", "x"),
    )

    chunks = {
        (0, 0): np.array([[1, 2], [5, 6]], dtype=np.uint16),
        (0, 1): np.array([[3, 4], [7, 8]], dtype=np.uint16),
        (1, 0): np.array([[9, 10], [13, 14]], dtype=np.uint16),
        (1, 1): np.array([[11, 12], [15, 16]], dtype=np.uint16),
    }

    monkeypatch.setattr(
        "app.core.zarr_v3.load_2d_chunk",
        lambda *, chunk_indices, **_kwargs: chunks[chunk_indices],
    )

    window = load_2d_window(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="NDVI",
        metadata=metadata,
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


def test_load_2d_chunk_accepts_full_edge_chunk_payload(monkeypatch) -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(5, 5),
        chunk_shape=(3, 3),
        data_type="uint16",
        fill_value=0,
        codecs=[],
        separator="/",
        attributes={},
        dimension_names=("y", "x"),
    )

    full_chunk = np.arange(9, dtype=np.uint16).reshape((3, 3))

    monkeypatch.setattr(
        "app.core.zarr_v3._read_chunk_bytes",
        lambda **_kwargs: full_chunk.tobytes(),
    )

    chunk = load_2d_chunk(
        connector=None,  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="NDVI",
        metadata=metadata,
        chunk_indices=(1, 1),
    )

    assert chunk.shape == (3, 3)


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


def test_load_4d_chunk_reads_inner_sharded_chunk() -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(1, 1, 4, 4),
        chunk_shape=(1, 1, 4, 4),
        data_type="uint16",
        fill_value=0,
        codecs=[
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 1, 2, 2],
                    "codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_location": "end",
                },
            }
        ],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    chunks = [
        np.array([[[[1, 2], [5, 6]]]], dtype=np.uint16).tobytes(),
        np.array([[[[3, 4], [7, 8]]]], dtype=np.uint16).tobytes(),
        np.array([[[[9, 10], [13, 14]]]], dtype=np.uint16).tobytes(),
        np.array([[[[11, 12], [15, 16]]]], dtype=np.uint16).tobytes(),
    ]
    payload = b"".join(chunks)
    index = np.asarray(
        [
            [0, len(chunks[0])],
            [len(chunks[0]), len(chunks[1])],
            [len(chunks[0]) + len(chunks[1]), len(chunks[2])],
            [len(chunks[0]) + len(chunks[1]) + len(chunks[2]), len(chunks[3])],
        ],
        dtype="<u8",
    ).reshape((1, 1, 2, 2, 2))
    index_bytes = index.tobytes()

    class FakeConnector:
        def build_oci_uri(self, object_path: str) -> str:
            return f"oci://bucket@ns/{object_path}"

        def read_byte_tail(self, object_path: str, *, length: int, use_cache: bool = False) -> bytes:
            assert object_path == "bucket@ns/cubes/example.zarr/bands/c/0/0/0/0"
            assert length == len(index_bytes)
            return index_bytes

        def read_byte_range(
            self,
            object_path: str,
            *,
            start: int | None = None,
            end: int | None = None,
            use_cache: bool = False,
        ) -> bytes:
            assert object_path == "bucket@ns/cubes/example.zarr/bands/c/0/0/0/0"
            assert start is not None
            assert end is not None
            return payload[start:end]

    chunk = load_4d_chunk(
        connector=FakeConnector(),  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        chunk_indices=(0, 0, 1, 1),
    )

    assert chunk.tolist() == [[[[11, 12], [15, 16]]]]


def test_estimate_4d_nonempty_pixel_bounds_reads_shard_indices() -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(2, 1, 8, 8),
        chunk_shape=(1, 1, 4, 4),
        data_type="float32",
        fill_value=np.nan,
        codecs=[
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 1, 2, 2],
                    "codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_location": "end",
                },
            }
        ],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    missing = np.asarray([np.iinfo(np.uint64).max, np.iinfo(np.uint64).max], dtype="<u8")

    def shard_index(nonempty_locals: set[tuple[int, int]]) -> bytes:
        index = np.zeros((1, 1, 2, 2, 2), dtype="<u8")
        for local_y in range(2):
            for local_x in range(2):
                if (local_y, local_x) in nonempty_locals:
                    index[0, 0, local_y, local_x] = (0, 8)
                else:
                    index[0, 0, local_y, local_x] = missing
        return index.tobytes()

    shard_payloads = {
        "bucket@ns/cubes/example.zarr/bands/c/0/0/0/0": shard_index({(1, 1)}),
        "bucket@ns/cubes/example.zarr/bands/c/1/0/1/1": shard_index({(0, 0)}),
    }

    class FakeConnector:
        def build_oci_uri(self, object_path: str) -> str:
            return f"oci://bucket@ns/{object_path}"

        def read_byte_tail(self, object_path: str, *, length: int, use_cache: bool = False) -> bytes:
            payload = shard_payloads.get(object_path)
            if payload is None:
                raise FileNotFoundError(object_path)
            assert length == len(payload)
            return payload

    bounds = estimate_4d_nonempty_pixel_bounds(
        connector=FakeConnector(),  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        time_indices=[0, 1],
        band_index=0,
    )

    assert bounds == (2, 6, 2, 6)


def test_estimate_4d_nonempty_pixel_bounds_returns_none_when_all_shards_are_empty() -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(1, 1, 4, 4),
        chunk_shape=(1, 1, 4, 4),
        data_type="float32",
        fill_value=np.nan,
        codecs=[
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 1, 2, 2],
                    "codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_location": "end",
                },
            }
        ],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    class FakeConnector:
        def build_oci_uri(self, object_path: str) -> str:
            return f"oci://bucket@ns/{object_path}"

        def read_byte_tail(self, object_path: str, *, length: int, use_cache: bool = False) -> bytes:
            raise FileNotFoundError(object_path)

    bounds = estimate_4d_nonempty_pixel_bounds(
        connector=FakeConnector(),  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        band_index=0,
    )

    assert bounds is None


def test_estimate_4d_nonempty_pixel_bounds_prefilters_with_object_layout() -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(1, 1, 8, 8),
        chunk_shape=(1, 1, 4, 4),
        data_type="float32",
        fill_value=np.nan,
        codecs=[
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 1, 2, 2],
                    "codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_location": "end",
                },
            }
        ],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    class FakeConnector:
        def build_oci_uri(self, object_path: str) -> str:
            return f"oci://bucket@ns/{object_path}"

        def list_prefixes(self, prefix: str) -> list[str]:
            assert prefix == "cubes/example.zarr/bands/c/0/0/"
            return ["cubes/example.zarr/bands/c/0/0/1/"]

        def list_objects(self, prefix: str, limit: int = 200) -> list[object]:
            assert prefix == "cubes/example.zarr/bands/c/0/0/1/"
            assert limit == 10000
            return [type("Obj", (), {"name": "cubes/example.zarr/bands/c/0/0/1/1"})()]

        def read_byte_tail(self, object_path: str, *, length: int, use_cache: bool = False) -> bytes:
            assert object_path == "bucket@ns/cubes/example.zarr/bands/c/0/0/1/1"
            index = np.asarray(
                [[[[[0, 8], [0, 8]], [[0, 8], [0, 8]]]]],
                dtype="<u8",
            )
            payload = index.tobytes()
            assert length == len(payload)
            return payload

    bounds = estimate_4d_nonempty_pixel_bounds(
        connector=FakeConnector(),  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        time_indices=[0],
        band_index=0,
    )

    assert bounds == (4, 8, 4, 8)


def test_estimate_4d_present_shard_pixel_bounds_uses_object_layout_only() -> None:
    metadata = ZarrV3ArrayMetadata(
        shape=(2, 1, 16, 16),
        chunk_shape=(1, 1, 8, 8),
        data_type="float32",
        fill_value=np.nan,
        codecs=[
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 1, 2, 2],
                    "codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                    ],
                    "index_location": "end",
                },
            }
        ],
        separator="/",
        attributes={},
        dimension_names=("time", "band", "y", "x"),
    )

    class FakeConnector:
        def list_prefixes(self, prefix: str) -> list[str]:
            assert prefix == "cubes/example.zarr/bands/c/0/0/"
            return ["cubes/example.zarr/bands/c/0/0/1/"]

        def list_objects(self, prefix: str, limit: int = 200) -> list[object]:
            assert prefix == "cubes/example.zarr/bands/c/0/0/1/"
            assert limit == 10000
            return [type("Obj", (), {"name": "cubes/example.zarr/bands/c/0/0/1/0"})()]

    bounds = estimate_4d_present_shard_pixel_bounds(
        connector=FakeConnector(),  # type: ignore[arg-type]
        store_path="cubes/example.zarr",
        array_name="bands",
        metadata=metadata,
        time_indices=[0],
        band_index=0,
    )

    assert bounds == (0, 8, 8, 16)
