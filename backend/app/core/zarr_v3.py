from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import json
from contextvars import copy_context
from dataclasses import dataclass
from threading import RLock
from typing import Any

import numpy as np
import numcodecs

from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.tile_observability import record_zarr_chunk_read
from app.core.tile_observability import record_zarr_shard_index_read
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
_DEFAULT_SHARD_INDEX_CACHE_ENTRIES = 4096
_DEFAULT_SHARD_INDEX_CACHE_BYTES = 64 * 1024 * 1024
_SHARD_INDEX_CACHE: OrderedDict[tuple[object, ...], tuple[np.ndarray, int]] = OrderedDict()
_SHARD_INDEX_CACHE_SIZE = 0
_SHARD_INDEX_CACHE_LOCK = RLock()


def _executor_map_with_context(executor: ThreadPoolExecutor, function, items):
    contexts_and_items = [(copy_context(), item) for item in items]
    return executor.map(lambda item: item[0].run(function, item[1]), contexts_and_items)


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
    if chunk_count <= 0:
        return np.asarray([], dtype=metadata.dtype)

    def read_chunk(chunk_index: int) -> np.ndarray:
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
        return np.frombuffer(decoded, dtype=metadata.dtype, count=expected_count).copy()

    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CHUNK_READS, chunk_count)) as executor:
        values = list(_executor_map_with_context(executor, read_chunk, range(chunk_count)))

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


def load_2d_window(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
    *,
    max_parallel_chunk_reads: int | None = None,
) -> np.ndarray:
    if len(metadata.shape) != 2:
        raise ValueError(f"{array_name} is not a 2D array")

    chunk_y, chunk_x = metadata.effective_chunk_shape
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
        chunk = load_2d_chunk(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(y_chunk_index, x_chunk_index),
        )
        return y_chunk_index, x_chunk_index, chunk

    if len(chunk_positions) <= 1:
        loaded_chunks = [_load_chunk(position) for position in chunk_positions]
    else:
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:
        chunk_y_start = y_chunk_index * chunk_y
        chunk_x_start = x_chunk_index * chunk_x
        chunk_y_stop = chunk_y_start + chunk.shape[0]
        chunk_x_stop = chunk_x_start + chunk.shape[1]

        src_y_start = max(y_start - chunk_y_start, 0)
        src_y_stop = min(y_stop - chunk_y_start, chunk.shape[0])
        src_x_start = max(x_start - chunk_x_start, 0)
        src_x_stop = min(x_stop - chunk_x_start, chunk.shape[1])

        dst_y_start = max(chunk_y_start - y_start, 0)
        dst_y_stop = dst_y_start + (src_y_stop - src_y_start)
        dst_x_start = max(chunk_x_start - x_start, 0)
        dst_x_stop = dst_x_start + (src_x_stop - src_x_start)

        window[dst_y_start:dst_y_stop, dst_x_start:dst_x_stop] = chunk[
            src_y_start:src_y_stop,
            src_x_start:src_x_stop,
        ]

    return window


def load_2d_window_decimated(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
    *,
    y_step: int,
    x_step: int,
    max_parallel_chunk_reads: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(metadata.shape) != 2:
        raise ValueError(f"{array_name} is not a 2D array")
    if y_step <= 0 or x_step <= 0:
        raise ValueError("Decimation steps must be positive integers")

    chunk_y, chunk_x = metadata.effective_chunk_shape
    sampled_y = np.arange(y_start, y_stop, y_step, dtype=np.int64)
    sampled_x = np.arange(x_start, x_stop, x_step, dtype=np.int64)
    if sampled_y.size == 0:
        sampled_y = np.asarray([y_start], dtype=np.int64)
    if sampled_x.size == 0:
        sampled_x = np.asarray([x_start], dtype=np.int64)
    if sampled_y[-1] != (y_stop - 1):
        sampled_y = np.append(sampled_y, y_stop - 1)
    if sampled_x[-1] != (x_stop - 1):
        sampled_x = np.append(sampled_x, x_stop - 1)

    fill_value = metadata.fill_value if metadata.fill_value is not None else 0
    window = np.full((len(sampled_y), len(sampled_x)), fill_value, dtype=metadata.dtype)

    y_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for row_index, y_value in enumerate(sampled_y.tolist()):
        y_chunk_index = y_value // chunk_y
        y_samples_by_chunk.setdefault(y_chunk_index, []).append((row_index, y_value - (y_chunk_index * chunk_y)))

    x_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for col_index, x_value in enumerate(sampled_x.tolist()):
        x_chunk_index = x_value // chunk_x
        x_samples_by_chunk.setdefault(x_chunk_index, []).append((col_index, x_value - (x_chunk_index * chunk_x)))

    chunk_positions = [
        (y_chunk_index, x_chunk_index)
        for y_chunk_index in y_samples_by_chunk
        for x_chunk_index in x_samples_by_chunk
    ]

    def _load_chunk(position: tuple[int, int]) -> tuple[int, int, np.ndarray]:
        y_chunk_index, x_chunk_index = position
        chunk = load_2d_chunk(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(y_chunk_index, x_chunk_index),
        )
        return y_chunk_index, x_chunk_index, chunk

    if len(chunk_positions) <= 1:
        loaded_chunks = [_load_chunk(position) for position in chunk_positions]
    else:
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:
        row_samples = y_samples_by_chunk[y_chunk_index]
        col_samples = x_samples_by_chunk[x_chunk_index]
        row_indices = np.asarray([sample[0] for sample in row_samples], dtype=np.int64)
        col_indices = np.asarray([sample[0] for sample in col_samples], dtype=np.int64)
        local_y = np.asarray([sample[1] for sample in row_samples], dtype=np.int64)
        local_x = np.asarray([sample[1] for sample in col_samples], dtype=np.int64)
        window[np.ix_(row_indices, col_indices)] = chunk[np.ix_(local_y, local_x)]

    return window, sampled_y.astype(np.float64), sampled_x.astype(np.float64)


def load_3d_window(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    time_index: int,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
    *,
    max_parallel_chunk_reads: int | None = None,
) -> np.ndarray:
    if len(metadata.shape) != 3:
        raise ValueError(f"{array_name} is not a 3D array")

    chunk_t, chunk_y, chunk_x = metadata.effective_chunk_shape
    if chunk_t != 1:
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
        chunk = load_3d_chunk(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(time_index, y_chunk_index, x_chunk_index),
        )
        return y_chunk_index, x_chunk_index, chunk

    if len(chunk_positions) <= 1:
        loaded_chunks = [_load_chunk(position) for position in chunk_positions]
    else:
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:
        chunk_y_start = y_chunk_index * chunk_y
        chunk_x_start = x_chunk_index * chunk_x
        chunk_y_stop = chunk_y_start + chunk.shape[1]
        chunk_x_stop = chunk_x_start + chunk.shape[2]

        src_y_start = max(y_start - chunk_y_start, 0)
        src_y_stop = min(y_stop - chunk_y_start, chunk.shape[1])
        src_x_start = max(x_start - chunk_x_start, 0)
        src_x_stop = min(x_stop - chunk_x_start, chunk.shape[2])

        dst_y_start = max(chunk_y_start - y_start, 0)
        dst_y_stop = dst_y_start + (src_y_stop - src_y_start)
        dst_x_start = max(chunk_x_start - x_start, 0)
        dst_x_stop = dst_x_start + (src_x_stop - src_x_start)

        window[dst_y_start:dst_y_stop, dst_x_start:dst_x_stop] = chunk[
            0,
            src_y_start:src_y_stop,
            src_x_start:src_x_stop,
        ]

    return window


def load_3d_window_decimated(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    time_index: int,
    y_start: int,
    y_stop: int,
    x_start: int,
    x_stop: int,
    *,
    y_step: int,
    x_step: int,
    max_parallel_chunk_reads: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(metadata.shape) != 3:
        raise ValueError(f"{array_name} is not a 3D array")
    if y_step <= 0 or x_step <= 0:
        raise ValueError("Decimation steps must be positive integers")

    chunk_t, chunk_y, chunk_x = metadata.effective_chunk_shape
    if chunk_t != 1:
        raise ValueError(f"{array_name} chunk layout is not supported: {metadata.effective_chunk_shape}")

    sampled_y = np.arange(y_start, y_stop, y_step, dtype=np.int64)
    sampled_x = np.arange(x_start, x_stop, x_step, dtype=np.int64)
    if sampled_y.size == 0:
        sampled_y = np.asarray([y_start], dtype=np.int64)
    if sampled_x.size == 0:
        sampled_x = np.asarray([x_start], dtype=np.int64)
    if sampled_y[-1] != (y_stop - 1):
        sampled_y = np.append(sampled_y, y_stop - 1)
    if sampled_x[-1] != (x_stop - 1):
        sampled_x = np.append(sampled_x, x_stop - 1)

    fill_value = metadata.fill_value if metadata.fill_value is not None else 0
    window = np.full((len(sampled_y), len(sampled_x)), fill_value, dtype=metadata.dtype)

    y_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for row_index, y_value in enumerate(sampled_y.tolist()):
        y_chunk_index = y_value // chunk_y
        y_samples_by_chunk.setdefault(y_chunk_index, []).append((row_index, y_value - (y_chunk_index * chunk_y)))

    x_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for col_index, x_value in enumerate(sampled_x.tolist()):
        x_chunk_index = x_value // chunk_x
        x_samples_by_chunk.setdefault(x_chunk_index, []).append((col_index, x_value - (x_chunk_index * chunk_x)))

    chunk_positions = [
        (y_chunk_index, x_chunk_index)
        for y_chunk_index in y_samples_by_chunk
        for x_chunk_index in x_samples_by_chunk
    ]

    def _load_chunk(position: tuple[int, int]) -> tuple[int, int, np.ndarray]:
        y_chunk_index, x_chunk_index = position
        chunk = load_3d_chunk(
            connector=connector,
            store_path=store_path,
            array_name=array_name,
            metadata=metadata,
            chunk_indices=(time_index, y_chunk_index, x_chunk_index),
        )
        return y_chunk_index, x_chunk_index, chunk

    if len(chunk_positions) <= 1:
        loaded_chunks = [_load_chunk(position) for position in chunk_positions]
    else:
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:
        row_samples = y_samples_by_chunk[y_chunk_index]
        col_samples = x_samples_by_chunk[x_chunk_index]
        row_indices = np.asarray([sample[0] for sample in row_samples], dtype=np.int64)
        col_indices = np.asarray([sample[0] for sample in col_samples], dtype=np.int64)
        local_y = np.asarray([sample[1] for sample in row_samples], dtype=np.int64)
        local_x = np.asarray([sample[1] for sample in col_samples], dtype=np.int64)
        window[np.ix_(row_indices, col_indices)] = chunk[0][np.ix_(local_y, local_x)]

    return window, sampled_y.astype(np.float64), sampled_x.astype(np.float64)


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
    *,
    max_parallel_chunk_reads: int | None = None,
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
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

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


def load_4d_window_decimated(
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
    *,
    y_step: int,
    x_step: int,
    max_parallel_chunk_reads: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(metadata.shape) != 4:
        raise ValueError(f"{array_name} is not a 4D array")
    if y_step <= 0 or x_step <= 0:
        raise ValueError("Decimation steps must be positive integers")

    chunk_t, chunk_b, chunk_y, chunk_x = metadata.effective_chunk_shape
    if chunk_t != 1 or chunk_b != 1:
        raise ValueError(f"{array_name} chunk layout is not supported: {metadata.effective_chunk_shape}")

    sampled_y = np.arange(y_start, y_stop, y_step, dtype=np.int64)
    sampled_x = np.arange(x_start, x_stop, x_step, dtype=np.int64)
    if sampled_y.size == 0:
        sampled_y = np.asarray([y_start], dtype=np.int64)
    if sampled_x.size == 0:
        sampled_x = np.asarray([x_start], dtype=np.int64)
    if sampled_y[-1] != (y_stop - 1):
        sampled_y = np.append(sampled_y, y_stop - 1)
    if sampled_x[-1] != (x_stop - 1):
        sampled_x = np.append(sampled_x, x_stop - 1)

    fill_value = metadata.fill_value if metadata.fill_value is not None else 0
    window = np.full((len(sampled_y), len(sampled_x)), fill_value, dtype=metadata.dtype)

    y_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for row_index, y_value in enumerate(sampled_y.tolist()):
        y_chunk_index = y_value // chunk_y
        y_samples_by_chunk.setdefault(y_chunk_index, []).append((row_index, y_value - (y_chunk_index * chunk_y)))

    x_samples_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for col_index, x_value in enumerate(sampled_x.tolist()):
        x_chunk_index = x_value // chunk_x
        x_samples_by_chunk.setdefault(x_chunk_index, []).append((col_index, x_value - (x_chunk_index * chunk_x)))

    chunk_positions = [
        (y_chunk_index, x_chunk_index)
        for y_chunk_index in y_samples_by_chunk
        for x_chunk_index in x_samples_by_chunk
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
        max_workers = min(
            _MAX_PARALLEL_CHUNK_READS if max_parallel_chunk_reads is None else max(max_parallel_chunk_reads, 1),
            len(chunk_positions),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            loaded_chunks = list(_executor_map_with_context(executor, _load_chunk, chunk_positions))

    for y_chunk_index, x_chunk_index, chunk in loaded_chunks:
        row_samples = y_samples_by_chunk[y_chunk_index]
        col_samples = x_samples_by_chunk[x_chunk_index]
        row_indices = np.asarray([sample[0] for sample in row_samples], dtype=np.int64)
        col_indices = np.asarray([sample[0] for sample in col_samples], dtype=np.int64)
        local_y = np.asarray([sample[1] for sample in row_samples], dtype=np.int64)
        local_x = np.asarray([sample[1] for sample in col_samples], dtype=np.int64)
        window[np.ix_(row_indices, col_indices)] = chunk[0, 0][np.ix_(local_y, local_x)]

    return window, sampled_y.astype(np.float64), sampled_x.astype(np.float64)


def load_2d_chunk(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    chunk_indices: tuple[int, int],
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


def load_3d_chunk(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    chunk_indices: tuple[int, int, int],
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


def estimate_4d_nonempty_pixel_bounds(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    *,
    time_indices: list[int] | None = None,
    band_index: int = 0,
) -> tuple[int, int, int, int] | None:
    if len(metadata.shape) != 4:
        raise ValueError(f"{array_name} is not a 4D array")

    sharding = metadata.sharding
    if sharding is None:
        return None

    chunk_t, chunk_b, chunk_y, chunk_x = metadata.effective_chunk_shape
    if chunk_t != 1 or chunk_b != 1:
        raise ValueError(f"{array_name} chunk layout is not supported: {metadata.effective_chunk_shape}")

    shape_t, shape_b, shape_y, shape_x = metadata.shape
    if band_index < 0 or band_index >= shape_b:
        return None

    if time_indices is None:
        time_indices = list(range(shape_t))
    else:
        time_indices = [index for index in time_indices if 0 <= index < shape_t]
    if not time_indices:
        return None

    chunk_grid_y = int(np.ceil(shape_y / chunk_y))
    chunk_grid_x = int(np.ceil(shape_x / chunk_x))
    chunks_per_shard = _chunks_per_shard(
        shard_shape=metadata.chunk_shape,
        inner_chunk_shape=sharding.chunk_shape,
    )
    shard_positions = _list_present_shard_positions(
        connector=connector,
        store_path=store_path,
        array_name=array_name,
        metadata=metadata,
        time_indices=time_indices,
        band_index=band_index,
    )
    if shard_positions is None:
        y_shard_count = int(np.ceil(chunk_grid_y / chunks_per_shard[2]))
        x_shard_count = int(np.ceil(chunk_grid_x / chunks_per_shard[3]))
        shard_positions = [
            (time_index, band_index, y_shard_index, x_shard_index)
            for time_index in time_indices
            for y_shard_index in range(y_shard_count)
            for x_shard_index in range(x_shard_count)
        ]

    def _load_nonempty_chunks(position: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        time_index, band_index_value, y_shard_index, x_shard_index = position
        object_path = build_chunk_object_path(
            store_path=store_path,
            array_name=array_name,
            separator=metadata.separator,
            chunk_indices=(time_index, band_index_value, y_shard_index, x_shard_index),
        )
        try:
            shard_index = _read_shard_index(
                connector=connector,
                object_path=object_path,
                shard_shape=metadata.chunk_shape,
                inner_chunk_shape=sharding.chunk_shape,
                index_codecs=sharding.index_codecs,
                index_location=sharding.index_location,
            )
        except FileNotFoundError:
            return []

        local_y_limit = min(chunks_per_shard[2], chunk_grid_y - (y_shard_index * chunks_per_shard[2]))
        local_x_limit = min(chunks_per_shard[3], chunk_grid_x - (x_shard_index * chunks_per_shard[3]))
        nonempty: list[tuple[int, int]] = []
        for local_y in range(max(local_y_limit, 0)):
            for local_x in range(max(local_x_limit, 0)):
                offset = int(shard_index[0, 0, local_y, local_x, 0])
                length = int(shard_index[0, 0, local_y, local_x, 1])
                if offset == _UINT64_MAX and length == _UINT64_MAX:
                    continue
                nonempty.append(
                    (
                        y_shard_index * chunks_per_shard[2] + local_y,
                        x_shard_index * chunks_per_shard[3] + local_x,
                    )
                )
        return nonempty

    if len(shard_positions) <= 1:
        shard_results = [_load_nonempty_chunks(position) for position in shard_positions]
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CHUNK_READS, len(shard_positions))) as executor:
            shard_results = list(_executor_map_with_context(executor, _load_nonempty_chunks, shard_positions))

    nonempty_chunks = [item for group in shard_results for item in group]
    if not nonempty_chunks:
        return None

    y_chunks = [item[0] for item in nonempty_chunks]
    x_chunks = [item[1] for item in nonempty_chunks]
    min_y_chunk = min(y_chunks)
    max_y_chunk = max(y_chunks)
    min_x_chunk = min(x_chunks)
    max_x_chunk = max(x_chunks)
    return (
        min_x_chunk * chunk_x,
        min((max_x_chunk + 1) * chunk_x, shape_x),
        min_y_chunk * chunk_y,
        min((max_y_chunk + 1) * chunk_y, shape_y),
    )


def estimate_4d_present_shard_pixel_bounds(
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    *,
    time_indices: list[int] | None = None,
    band_index: int = 0,
) -> tuple[int, int, int, int] | None:
    if len(metadata.shape) != 4:
        raise ValueError(f"{array_name} is not a 4D array")

    sharding = metadata.sharding
    if sharding is None:
        return None

    shape_t, shape_b, shape_y, shape_x = metadata.shape
    if band_index < 0 or band_index >= shape_b:
        return None

    if time_indices is None:
        time_indices = list(range(shape_t))
    else:
        time_indices = [index for index in time_indices if 0 <= index < shape_t]
    if not time_indices:
        return None

    shard_positions = _list_present_shard_positions(
        connector=connector,
        store_path=store_path,
        array_name=array_name,
        metadata=metadata,
        time_indices=time_indices,
        band_index=band_index,
    )
    if not shard_positions:
        return None

    shard_y = int(metadata.chunk_shape[2])
    shard_x = int(metadata.chunk_shape[3])
    y_shards = [position[2] for position in shard_positions]
    x_shards = [position[3] for position in shard_positions]
    return (
        min(x_shards) * shard_x,
        min((max(x_shards) + 1) * shard_x, shape_x),
        min(y_shards) * shard_y,
        min((max(y_shards) + 1) * shard_y, shape_y),
    )


def _list_present_shard_positions(
    *,
    connector: OCIObjectStorageConnector,
    store_path: str,
    array_name: str,
    metadata: ZarrV3ArrayMetadata,
    time_indices: list[int],
    band_index: int,
) -> list[tuple[int, int, int, int]] | None:
    if metadata.separator != "/":
        return None
    if not hasattr(connector, "list_prefixes") or not hasattr(connector, "list_objects"):
        return None

    present: list[tuple[int, int, int, int]] = []
    array_root = f"{store_path.rstrip('/')}/{array_name}/c"

    for time_index in time_indices:
        band_root = f"{array_root}/{time_index}/{band_index}/"
        y_prefixes = connector.list_prefixes(prefix=band_root)
        for y_prefix in y_prefixes:
            y_token = y_prefix.removeprefix(band_root).rstrip("/")
            if not y_token.isdigit():
                continue
            y_shard_index = int(y_token)
            for item in connector.list_objects(prefix=y_prefix, limit=10000):
                x_token = item.name.rstrip("/").split("/")[-1]
                if not x_token.isdigit():
                    continue
                present.append((time_index, band_index, y_shard_index, int(x_token)))

    return present or None


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

    record_zarr_chunk_read()
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
            record_zarr_chunk_read()
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
    cache_key = _shard_index_cache_key(
        connector=connector,
        resolved=resolved,
        shard_shape=shard_shape,
        inner_chunk_shape=inner_chunk_shape,
        index_codecs=index_codecs,
        index_location=index_location,
        encoded_index_size=encoded_index_size,
    )
    cached = _get_cached_shard_index(connector, cache_key)
    if cached is not None:
        return cached

    if index_location == "start":
        payload = connector.read_byte_range(resolved, start=0, end=encoded_index_size, use_cache=True)
    elif index_location == "end":
        payload = connector.read_byte_tail(resolved, length=encoded_index_size, use_cache=True)
    else:
        raise ValueError(f"Unsupported Zarr v3 sharding index_location: {index_location}")

    record_zarr_shard_index_read()
    decoded = _decode_bytes(payload, index_codecs)
    expected_entries = int(np.prod(chunks_per_shard))
    index = np.frombuffer(decoded, dtype="<u8")
    if index.size != expected_entries * 2:
        raise ValueError(
            f"Unexpected shard index size: {index.size} uint64 values, expected {expected_entries * 2}"
        )
    reshaped = index.reshape(chunks_per_shard + (2,))
    _put_cached_shard_index(connector, cache_key, reshaped)
    return reshaped


def clear_zarr_shard_index_cache() -> None:
    global _SHARD_INDEX_CACHE_SIZE
    with _SHARD_INDEX_CACHE_LOCK:
        _SHARD_INDEX_CACHE.clear()
        _SHARD_INDEX_CACHE_SIZE = 0


def _shard_index_cache_key(
    *,
    connector: OCIObjectStorageConnector,
    resolved: str,
    shard_shape: tuple[int, ...],
    inner_chunk_shape: tuple[int, ...],
    index_codecs: list[dict[str, Any]],
    index_location: str,
    encoded_index_size: int,
) -> tuple[object, ...]:
    return (
        id(connector),
        resolved,
        shard_shape,
        inner_chunk_shape,
        json.dumps(index_codecs, sort_keys=True, separators=(",", ":")),
        index_location,
        encoded_index_size,
    )


def _get_cached_shard_index(
    connector: OCIObjectStorageConnector,
    cache_key: tuple[object, ...],
) -> np.ndarray | None:
    max_entries, max_bytes = _shard_index_cache_limits(connector)
    if max_entries <= 0 or max_bytes <= 0:
        return None
    with _SHARD_INDEX_CACHE_LOCK:
        cached = _SHARD_INDEX_CACHE.get(cache_key)
        if cached is None:
            return None
        _SHARD_INDEX_CACHE.move_to_end(cache_key)
        return cached[0]


def _put_cached_shard_index(
    connector: OCIObjectStorageConnector,
    cache_key: tuple[object, ...],
    index: np.ndarray,
) -> None:
    global _SHARD_INDEX_CACHE_SIZE
    max_entries, max_bytes = _shard_index_cache_limits(connector)
    if max_entries <= 0 or max_bytes <= 0 or index.nbytes > max_bytes:
        return
    with _SHARD_INDEX_CACHE_LOCK:
        existing = _SHARD_INDEX_CACHE.pop(cache_key, None)
        if existing is not None:
            _SHARD_INDEX_CACHE_SIZE -= existing[1]
        _SHARD_INDEX_CACHE[cache_key] = (index, index.nbytes)
        _SHARD_INDEX_CACHE_SIZE += index.nbytes
        while len(_SHARD_INDEX_CACHE) > max_entries or _SHARD_INDEX_CACHE_SIZE > max_bytes:
            _, (_, evicted_size) = _SHARD_INDEX_CACHE.popitem(last=False)
            _SHARD_INDEX_CACHE_SIZE -= evicted_size


def _shard_index_cache_limits(connector: OCIObjectStorageConnector) -> tuple[int, int]:
    settings = getattr(connector, "_settings", None)
    entries = getattr(settings, "zarr_shard_index_cache_entries", _DEFAULT_SHARD_INDEX_CACHE_ENTRIES)
    bytes_limit = getattr(settings, "zarr_shard_index_cache_bytes", _DEFAULT_SHARD_INDEX_CACHE_BYTES)
    return max(int(entries), 0), max(int(bytes_limit), 0)


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
