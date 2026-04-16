from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import numcodecs

from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.zarr_reader import read_store_json


_DTYPE_MAP = {
    "bool": np.dtype(np.bool_),
    "int8": np.dtype(np.int8),
    "int16": np.dtype(np.int16),
    "int32": np.dtype(np.int32),
    "int64": np.dtype(np.int64),
    "uint8": np.dtype(np.uint8),
    "uint16": np.dtype(np.uint16),
    "uint32": np.dtype(np.uint32),
    "uint64": np.dtype(np.uint64),
    "float32": np.dtype(np.float32),
    "float64": np.dtype(np.float64),
}

_UINT64_MAX = (1 << 64) - 1
_MAX_PARALLEL_CHUNK_READS = 8


@dataclass(frozen=True)
class ZarrV3ArrayMetadata:
    shape: tuple[int, ...]
    chunk_shape: tuple[int, ...]
    data_type: Any
    fill_value: Any
    codecs: list[dict[str, Any]]
    separator: str
    attributes: dict[str, Any]
    dimension_names: tuple[str, ...]

    @property
    def dtype(self) -> np.dtype:
        return _dtype_from_data_type(self.data_type)

    @property
    def sharding(self) -> "ShardingCodecMetadata | None":
        return _extract_sharding_codec(self.codecs)

    @property
    def effective_chunk_shape(self) -> tuple[int, ...]:
        if self.sharding is not None:
            return self.sharding.chunk_shape
        return self.chunk_shape


@dataclass(frozen=True)
class ShardingCodecMetadata:
    chunk_shape: tuple[int, ...]
    codecs: list[dict[str, Any]]
    index_codecs: list[dict[str, Any]]
    index_location: str


def read_consolidated_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_json = read_store_json(
        connector=connector,
        object_path=connector.build_oci_uri(store_path.rstrip("/") + "/zarr.json"),
    )
    store_metadata = json.loads(raw_json)
    metadata = store_metadata.get("consolidated_metadata", {}).get("metadata", {})
    return store_metadata, metadata


def read_store_metadata(
    connector: OCIObjectStorageConnector,
    store_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_metadata, metadata = read_consolidated_metadata(
        connector=connector,
        store_path=store_path,
    )
    if metadata:
        return store_metadata, metadata

    store_prefix = store_path.rstrip("/") + "/"
    discovered: dict[str, Any] = {}
    for child_prefix in connector.list_prefixes(prefix=store_prefix):
        relative_prefix = child_prefix.removeprefix(store_prefix).rstrip("/")
        if not relative_prefix:
            continue
        child_object_path = connector.build_oci_uri(child_prefix.rstrip("/") + "/zarr.json")
        try:
            discovered[relative_prefix] = json.loads(
                read_store_json(
                    connector=connector,
                    object_path=child_object_path,
                )
            )
        except FileNotFoundError:
            continue

    return store_metadata, discovered


def parse_array_metadata(node: dict[str, Any]) -> ZarrV3ArrayMetadata:
    separator = node.get("chunk_key_encoding", {}).get("configuration", {}).get("separator", "/")
    return ZarrV3ArrayMetadata(
        shape=tuple(int(item) for item in node["shape"]),
        chunk_shape=tuple(int(item) for item in node["chunk_grid"]["configuration"]["chunk_shape"]),
        data_type=node["data_type"],
        fill_value=node.get("fill_value"),
        codecs=list(node.get("codecs", [])),
        separator=separator,
        attributes=dict(node.get("attributes", {})),
        dimension_names=tuple(node.get("dimension_names", [])),
    )


def build_chunk_object_path(
    store_path: str,
    array_name: str,
    separator: str,
    chunk_indices: tuple[int, ...],
) -> str:
    encoded_indices = separator.join(str(item) for item in chunk_indices)
    return f"{store_path.rstrip('/')}/{array_name}/c/{encoded_indices}"


def load_1d_numeric_array(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
) -> np.ndarray:
    if len(metadata.shape) != 1:
        raise ValueError(f"{array_name} is not a 1D array")

    chunk_size = metadata.effective_chunk_shape[0]
    chunk_count = int(np.ceil(metadata.shape[0] / chunk_size))
    values: list[np.ndarray] = []
    for chunk_index in range(chunk_count):
        decoded = _read_array_chunk_bytes(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(chunk_index,),
        )
        expected_count = _resolved_chunk_length(
            size=metadata.shape[0],
            chunk_size=chunk_size,
            chunk_index=chunk_index,
        )
        values.append(np.frombuffer(decoded, dtype=metadata.dtype, count=expected_count).copy())

    if not values:
        return np.asarray([], dtype=metadata.dtype)
    return np.concatenate(values, axis=0)


def load_fixed_length_utf32_labels(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
) -> list[str]:
    if len(metadata.shape) != 1:
        raise ValueError(f"{array_name} is not a 1D array")

    data_type = metadata.data_type
    if not isinstance(data_type, dict) or data_type.get("name") != "fixed_length_utf32":
        raise ValueError(f"{array_name} is not fixed_length_utf32")

    chars_per_value = int(data_type["configuration"]["length_bytes"]) // 4
    chunk_size = metadata.effective_chunk_shape[0]
    chunk_count = int(np.ceil(metadata.shape[0] / chunk_size))
    values: list[str] = []

    for chunk_index in range(chunk_count):
        decoded = _read_array_chunk_bytes(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(chunk_index,),
        )
        expected_count = _resolved_chunk_length(
            size=metadata.shape[0],
            chunk_size=chunk_size,
            chunk_index=chunk_index,
        )
        text = decoded.decode("utf-32-le")
        chunk_values = [
            text[index : index + chars_per_value].rstrip("\x00")
            for index in range(0, len(text), chars_per_value)
        ]
        values.extend(chunk_values[:expected_count])

    return values[: metadata.shape[0]]


def load_4d_window(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    time_index: int,
    band_index: int,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
) -> np.ndarray:
    if len(metadata.shape) != 4:
        raise ValueError(f"{array_name} is not a 4D array")

    chunk_t, chunk_b, chunk_y, chunk_x = metadata.effective_chunk_shape
    if chunk_t != 1 or chunk_b != 1:
        raise ValueError(f"{array_name} chunk layout is not supported: {metadata.effective_chunk_shape}")

    fill_value = metadata.fill_value if metadata.fill_value is not None else 0
    window = np.full((y_stop - y_start, x_stop - x_start), fill_value, dtype=metadata.dtype)

    y_chunk_start = y_start // chunk_y
    y_chunk_stop = (y_stop - 1) // chunk_y + 1
    x_chunk_start = x_start // chunk_x
    x_chunk_stop = (x_stop - 1) // chunk_x + 1

    chunk_positions = [
        (y_chunk_index, x_chunk_index)
        for y_chunk_index in range(y_chunk_start, y_chunk_stop)
        for x_chunk_index in range(x_chunk_start, x_chunk_stop)
    ]

    def _load_chunk(position: tuple[int, int]) -> tuple[int, int, np.ndarray]:
        y_chunk_index, x_chunk_index = position
        chunk = load_4d_chunk(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(time_index, band_index, y_chunk_index, x_chunk_index),
        )
        return y_chunk_index, x_chunk_index, chunk

    if len(chunk_positions) <= 1:
        loaded_chunks = [_load_chunk(position) for position in chunk_positions]
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CHUNK_READS, len(chunk_positions))) as executor:
            loaded_chunks = list(executor.map(_load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:

        chunk_y_start = y_chunk_index * chunk_y
        chunk_x_start = x_chunk_index * chunk_x
        chunk_y_stop = chunk_y_start + chunk.shape[2]
        chunk_x_stop = chunk_x_start + chunk.shape[3]

        src_y_start = max(y_start - chunk_y_start, 0)
        src_y_stop = min(y_stop - chunk_y_start, chunk.shape[2])
        src_x_start = max(x_start - chunk_x_start, 0)
        src_x_stop = min(x_stop - chunk_x_start, chunk.shape[3])

        dst_y_start = max(chunk_y_start - y_start, 0)
        dst_y_stop = dst_y_start + (src_y_stop - src_y_start)
        dst_x_start = max(chunk_x_start - x_start, 0)
        dst_x_stop = dst_x_start + (src_x_stop - src_x_start)

        window[dst_y_start:dst_y_stop, dst_x_start:dst_x_stop] = chunk[
            0,
            0,
            src_y_start:src_y_stop,
            src_x_start:src_x_stop,
        ]

    return window


def load_4d_chunk(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    chunk_indices: tuple[int, int, int, int],
) -> np.ndarray:
    chunk_shape = tuple(
        _resolved_chunk_length(size=size, chunk_size=chunk_size, chunk_index=chunk_index)
        for size, chunk_size, chunk_index in zip(metadata.shape, metadata.effective_chunk_shape, chunk_indices, strict=True)
    )

    decoded = _read_array_chunk_bytes(
        connector=connector,
        store_path=store_path,
        array_name=array_name,
        metadata=metadata,
        chunk_indices=chunk_indices,
        missing_ok=True,
    )
    if decoded is None:
        fill_value = metadata.fill_value if metadata.fill_value is not None else 0
        return np.full(chunk_shape, fill_value, dtype=metadata.dtype)

    flat = np.frombuffer(decoded, dtype=metadata.dtype)
    expected_size = int(np.prod(chunk_shape))
    if flat.size == expected_size:
        return flat.reshape(chunk_shape)

    full_chunk_size = int(np.prod(metadata.effective_chunk_shape))
    if flat.size == full_chunk_size:
        return flat.reshape(metadata.effective_chunk_shape)

    raise ValueError(
        f"Unexpected decoded chunk size for {array_name}: "
        f"{flat.size} values, expected {expected_size} or {full_chunk_size}"
    )


def _resolved_chunk_length(size: int, chunk_size: int, chunk_index: int) -> int:
    offset = chunk_index * chunk_size
    return max(min(chunk_size, size - offset), 0)


def _read_chunk_bytes(
    connector: OCIObjectStorageConnector,
    object_path: str,
    codecs: list[dict[str, Any]],
    missing_ok: bool = False,
) -> bytes | None:
    resolved = connector.build_oci_uri(object_path).removeprefix("oci://")
    try:
        payload = connector.read_bytes(resolved, use_cache=True)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise

    return _decode_bytes(payload, codecs)


def _read_array_chunk_bytes(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    chunk_indices: tuple[int, ...],
    missing_ok: bool = False,
) -> bytes | None:
    sharding = metadata.sharding
    if sharding is None:
        return _read_chunk_bytes(
            connector=connector,
            object_path=build_chunk_object_path(
                store_path=store_path,
                array_name=array_name,
                separator=metadata.separator,
                chunk_indices=chunk_indices,
            ),
            codecs=metadata.codecs,
            missing_ok=missing_ok,
        )

    return _read_sharded_chunk_bytes(
        connector=connector,
        store_path=store_path,
        array_name=array_name,
        metadata=metadata,
        sharding=sharding,
        chunk_indices=chunk_indices,
        missing_ok=missing_ok,
    )


def _read_sharded_chunk_bytes(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    sharding: ShardingCodecMetadata,
    chunk_indices: tuple[int, ...],
    missing_ok: bool,
) -> bytes | None:
    attempts = [
        (metadata.chunk_shape, sharding.chunk_shape),
    ]
    if metadata.chunk_shape != sharding.chunk_shape:
        attempts.append((sharding.chunk_shape, metadata.chunk_shape))

    last_error: Exception | None = None
    for shard_shape, inner_chunk_shape in attempts:
        shard_indices, local_chunk_indices = _resolve_shard_chunk_position(
            chunk_indices=chunk_indices,
            shard_shape=shard_shape,
            inner_chunk_shape=inner_chunk_shape,
        )
        shard_object_path = build_chunk_object_path(
            store_path=store_path,
            array_name=array_name,
            separator=metadata.separator,
            chunk_indices=shard_indices,
        )
        try:
            shard_index = _read_shard_index(
                connector=connector,
                object_path=shard_object_path,
                shard_shape=shard_shape,
                inner_chunk_shape=inner_chunk_shape,
                index_codecs=sharding.index_codecs,
                index_location=sharding.index_location,
            )
            offset, length = (
                int(value) for value in shard_index[tuple(local_chunk_indices) + (slice(None),)]
            )
            if offset == _UINT64_MAX and length == _UINT64_MAX:
                if missing_ok:
                    return None
                raise FileNotFoundError(shard_object_path)

            payload = connector.read_byte_range(
                connector.build_oci_uri(shard_object_path).removeprefix("oci://"),
                start=offset,
                end=offset + length,
                use_cache=True,
            )
            return _decode_bytes(payload, sharding.codecs)
        except (FileNotFoundError, ValueError) as exc:
            last_error = exc
            continue

    if missing_ok:
        return None
    if last_error is not None:
        raise last_error
    raise FileNotFoundError(build_chunk_object_path(store_path, array_name, metadata.separator, chunk_indices))


def _read_shard_index(
    connector: OCIObjectStorageConnector,
    object_path: str,
    shard_shape: tuple[int, ...],
    inner_chunk_shape: tuple[int, ...],
    index_codecs: list[dict[str, Any]],
    index_location: str,
) -> np.ndarray:
    chunks_per_shard = _chunks_per_shard(shard_shape=shard_shape, inner_chunk_shape=inner_chunk_shape)
    encoded_index_size = _shard_index_encoded_size(chunks_per_shard, index_codecs)
    resolved = connector.build_oci_uri(object_path).removeprefix("oci://")
    if index_location == "start":
        payload = connector.read_byte_range(resolved, start=0, end=encoded_index_size, use_cache=True)
    elif index_location == "end":
        payload = connector.read_byte_tail(resolved, length=encoded_index_size, use_cache=True)
    else:
        raise ValueError(f"Unsupported Zarr v3 sharding index_location: {index_location}")

    decoded = _decode_bytes(payload, index_codecs)
    expected_entries = int(np.prod(chunks_per_shard))
    index = np.frombuffer(decoded, dtype="<u8")
    if index.size != expected_entries * 2:
        raise ValueError(
            f"Unexpected shard index size: {index.size} uint64 values, expected {expected_entries * 2}"
        )
    return index.reshape(chunks_per_shard + (2,))


def _resolve_shard_chunk_position(
    chunk_indices: tuple[int, ...],
    shard_shape: tuple[int, ...],
    inner_chunk_shape: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    shard_indices: list[int] = []
    local_chunk_indices: list[int] = []
    for chunk_index, outer_size, inner_size in zip(
        chunk_indices,
        shard_shape,
        inner_chunk_shape,
        strict=True,
    ):
        offset = chunk_index * inner_size
        shard_index = offset // outer_size
        local_offset = offset - (shard_index * outer_size)
        shard_indices.append(shard_index)
        local_chunk_indices.append(local_offset // inner_size)
    return tuple(shard_indices), tuple(local_chunk_indices)


def _chunks_per_shard(
    shard_shape: tuple[int, ...],
    inner_chunk_shape: tuple[int, ...],
) -> tuple[int, ...]:
    return tuple(
        int(np.ceil(shard_size / inner_size))
        for shard_size, inner_size in zip(shard_shape, inner_chunk_shape, strict=True)
    )


def _shard_index_encoded_size(
    chunks_per_shard: tuple[int, ...],
    index_codecs: list[dict[str, Any]],
) -> int:
    size = int(np.prod(chunks_per_shard)) * 2 * np.dtype("<u8").itemsize
    for codec in index_codecs:
        codec_name = codec["name"]
        if codec_name == "bytes":
            continue
        if codec_name == "crc32c":
            size += 4
            continue
        raise ValueError(f"Unsupported Zarr v3 index codec: {codec_name}")
    return size


def _decode_bytes(payload: bytes, codecs: list[dict[str, Any]]) -> bytes:
    result = payload
    for codec in reversed(codecs):
        codec_name = codec["name"]
        configuration = codec.get("configuration", {})
        if codec_name == "zstd":
            result = numcodecs.Zstd(**configuration).decode(result)
            continue
        if codec_name == "crc32c":
            result = _decode_crc32c(result)
            continue
        if codec_name == "bytes":
            continue
        raise ValueError(f"Unsupported Zarr v3 codec: {codec_name}")
    return result


def _decode_crc32c(payload: bytes) -> bytes:
    if len(payload) < 4:
        raise ValueError("crc32c payload is too short")

    body = payload[:-4]
    expected = int.from_bytes(payload[-4:], byteorder="little", signed=False)
    actual = _crc32c(body)
    if actual != expected:
        raise ValueError(f"crc32c checksum mismatch: expected {expected}, got {actual}")
    return body


def _extract_sharding_codec(codecs: list[dict[str, Any]]) -> ShardingCodecMetadata | None:
    sharding_codec = next((codec for codec in codecs if codec["name"] == "sharding_indexed"), None)
    if sharding_codec is None:
        return None

    configuration = sharding_codec.get("configuration", {})
    return ShardingCodecMetadata(
        chunk_shape=tuple(int(value) for value in configuration["chunk_shape"]),
        codecs=list(configuration.get("codecs", [])),
        index_codecs=list(configuration.get("index_codecs", [])),
        index_location=str(configuration.get("index_location", "end")),
    )


def _crc32c(payload: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x82F63B78
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


def _dtype_from_data_type(data_type: Any) -> np.dtype:
    if isinstance(data_type, str):
        try:
            return _DTYPE_MAP[data_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported Zarr v3 data type: {data_type}") from exc

    raise ValueError(f"Unsupported Zarr v3 data type spec: {data_type!r}")
