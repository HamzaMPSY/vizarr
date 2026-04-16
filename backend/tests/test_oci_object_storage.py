from collections import OrderedDict
from threading import Lock
from types import SimpleNamespace

import oci

from app.core.oci_object_storage import OCIObjectStorageConnector


class _FakeFilesystem:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cat_file(self, path: str) -> bytes:
        self.calls.append(path)
        return f"payload:{path}".encode("utf-8")


class _FakeClient:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.head_paths: set[str] = set()

    def put_object(
        self,
        *,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        content_type: str,
    ) -> None:
        self.put_calls.append(
            {
                "namespace_name": namespace_name,
                "bucket_name": bucket_name,
                "object_name": object_name,
                "body": put_object_body,
                "content_type": content_type,
            }
        )
        self.head_paths.add(object_name)

    def head_object(
        self,
        *,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
    ) -> None:
        if object_name not in self.head_paths:
            raise oci.exceptions.ServiceError(
                status=404,
                code="NotFound",
                headers={},
                message="not found",
                request_id="req",
            )


def _connector() -> OCIObjectStorageConnector:
    connector = OCIObjectStorageConnector.__new__(OCIObjectStorageConnector)
    connector._text_cache = OrderedDict()
    connector._bytes_cache = OrderedDict()
    connector._bytes_cache_size = 0
    connector._text_cache_lock = Lock()
    connector._bytes_cache_lock = Lock()
    connector._settings = SimpleNamespace(oci_bucket="bucket")
    connector._namespace = "namespace"
    connector._client = _FakeClient()
    connector._filesystem = _FakeFilesystem()
    return connector


def test_read_text_uses_connector_cache() -> None:
    connector = _connector()
    filesystem = connector._filesystem

    first = connector.read_text("bucket/path.json", use_cache=True)
    second = connector.read_text("bucket/path.json", use_cache=True)

    assert first == "payload:bucket/path.json"
    assert second == first
    assert filesystem.calls == ["bucket/path.json"]


def test_read_bytes_uses_connector_cache() -> None:
    connector = _connector()
    filesystem = connector._filesystem

    first = connector.read_bytes("bucket/chunk", use_cache=True)
    second = connector.read_bytes("bucket/chunk", use_cache=True)

    assert first == b"payload:bucket/chunk"
    assert second == first
    assert filesystem.calls == ["bucket/chunk"]


def test_write_bytes_evicts_cached_reads_and_writes_to_object_storage() -> None:
    connector = _connector()
    connector._text_cache["bucket@namespace/browse/path.json"] = "stale"
    connector._bytes_cache["bucket@namespace/browse/path.json"] = b"stale"
    connector._bytes_cache_size = len(b"stale")

    connector.write_bytes("oci://bucket@namespace/browse/path.json", b"fresh", content_type="application/json")

    assert connector._client.put_calls == [
        {
            "namespace_name": "namespace",
            "bucket_name": "bucket",
            "object_name": "browse/path.json",
            "body": b"fresh",
            "content_type": "application/json",
        }
    ]
    assert connector._text_cache == OrderedDict()
    assert connector._bytes_cache == OrderedDict()
    assert connector._bytes_cache_size == 0


def test_object_exists_uses_head_object() -> None:
    connector = _connector()

    assert connector.object_exists("oci://bucket@namespace/missing.json") is False
    connector.write_text("oci://bucket@namespace/present.json", "{\"ok\":true}")

    assert connector.object_exists("oci://bucket@namespace/present.json") is True
