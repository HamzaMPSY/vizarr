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

    decoded = _read_chunk_bytes(
        connector=connector,
        object_path=build_chunk_object_path(
            store_path=store_path,
            array_name=array_name,
            separator=metadata.separator,
            chunk_indices=(0,),
        ),
        codecs=metadata.codecs,
    )
    return np.frombuffer(decoded, dtype=metadata.dtype, count=metadata.shape[0]).copy()


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

    decoded = _read_chunk_bytes(
        connector=connector,
        object_path=build_chunk_object_path(
            store_path=store_path,
            array_name=array_name,
            separator=metadata.separator,
            chunk_indices=(0,),
        ),
        codecs=metadata.codecs,
    )
    chars_per_value = int(data_type["configuration"]["length_bytes"]) // 4
    text = decoded.decode("utf-32-le")
    values = [
        text[index : index + chars_per_value].rstrip("\x00")
        for index in range(0, len(text), chars_per_value)
    ]
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

    chunk_t, chunk_b, chunk_y, chunk_x = metadata.chunk_shape
    if chunk_t != 1 or chunk_b != 1:
        raise ValueError(f"{array_name} chunk layout is not supported: {metadata.chunk_shape}")

    fill_value = metadata.fill_value if metadata.fill_value is not None else 0
    window = np.full((y_stop - y_start, x_stop - x_start), fill_value, dtype=metadata.dtype)

    y_chunk_start = y_start // chunk_y
    y_chunk_stop = (y_stop - 1) // chunk_y + 1
    x_chunk_start = x_start // chunk_x
    x_chunk_stop = (x_stop - 1) // chunk_x + 1

    for y_chunk_index in range(y_chunk_start, y_chunk_stop):
        for x_chunk_index in range(x_chunk_start, x_chunk_stop):
            chunk = load_4d_chunk(
                connector=connector,
                store_path=store_path,
                array_name=array_name,
                metadata=metadata,
                chunk_indices=(time_index, band_index, y_chunk_index, x_chunk_index),
            )

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
    object_path = build_chunk_object_path(
        store_path=store_path,
        array_name=array_name,
        separator=metadata.separator,
        chunk_indices=chunk_indices,
    )
    chunk_shape = tuple(
        _resolved_chunk_length(size=size, chunk_size=chunk_size, chunk_index=chunk_index)
        for size, chunk_size, chunk_index in zip(metadata.shape, metadata.chunk_shape, chunk_indices, strict=True)
    )

    decoded = _read_chunk_bytes(
        connector=connector,
        object_path=object_path,
        codecs=metadata.codecs,
        missing_ok=True,
    )
    if decoded is None:
        fill_value = metadata.fill_value if metadata.fill_value is not None else 0
        return np.full(chunk_shape, fill_value, dtype=metadata.dtype)

    flat = np.frombuffer(decoded, dtype=metadata.dtype)
    expected_size = int(np.prod(chunk_shape))
    if flat.size == expected_size:
        return flat.reshape(chunk_shape)

    full_chunk_size = int(np.prod(metadata.chunk_shape))
    if flat.size == full_chunk_size:
        return flat.reshape(metadata.chunk_shape)

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
    filesystem = connector.get_filesystem()
    resolved = connector.build_oci_uri(object_path).removeprefix("oci://")
    try:
        payload = filesystem.cat_file(resolved)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise

    return _decode_bytes(payload, codecs)


def _decode_bytes(payload: bytes, codecs: list[dict[str, Any]]) -> bytes:
    result = payload
    for codec in reversed(codecs):
        codec_name = codec["name"]
        configuration = codec.get("configuration", {})
        if codec_name == "zstd":
            result = numcodecs.Zstd(**configuration).decode(result)
            continue
        if codec_name == "bytes":
            continue
        raise ValueError(f"Unsupported Zarr v3 codec: {codec_name}")
    return result


def _dtype_from_data_type(data_type: Any) -> np.dtype:
    if isinstance(data_type, str):
        try:
            return _DTYPE_MAP[data_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported Zarr v3 data type: {data_type}") from exc

    raise ValueError(f"Unsupported Zarr v3 data type spec: {data_type!r}")
