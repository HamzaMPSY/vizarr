from collections import OrderedDict
from threading import Lock
from threading import RLock
from types import SimpleNamespace

import oci

from app.core.oci_object_storage import OCIObjectStorageConnector


class _FakeFilesystem:
    def __init__(self, responses: list[bytes | Exception] | None = None) -> None:
        self.calls: list[str] = []
        self._responses = list(responses or [])

    def cat_file(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        self.calls.append(path)
        if self._responses:
            response = self._responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return f"payload:{path}".encode("utf-8")


class _FakeClient:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.head_paths: set[str] = set()
        self.put_errors: list[Exception] = []
        self.head_errors: list[Exception] = []

    def put_object(
        self,
        *,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        content_type: str,
    ) -> None:
        if self.put_errors:
            raise self.put_errors.pop(0)
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
        if self.head_errors:
            raise self.head_errors.pop(0)
        if object_name not in self.head_paths:
            raise oci.exceptions.ServiceError(
                status=404,
                code="NotFound",
                headers={},
                message="not found",
                request_id="req",
            )
        return SimpleNamespace(
            data=SimpleNamespace(
                content_length=1,
                etag="etag",
                content_type="application/json",
            )
        )


def _service_error(status: int, code: str = "NotAuthenticated") -> oci.exceptions.ServiceError:
    return oci.exceptions.ServiceError(
        status=status,
        code=code,
        headers={},
        message=code,
        request_id="req",
    )


def _connector() -> OCIObjectStorageConnector:
    connector = OCIObjectStorageConnector.__new__(OCIObjectStorageConnector)
    connector._text_cache = OrderedDict()
    connector._bytes_cache = OrderedDict()
    connector._bytes_cache_size = 0
    connector._text_cache_lock = Lock()
    connector._bytes_cache_lock = Lock()
    connector._auth_lock = RLock()
    connector._auth_generation = 0
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

    assert first == "payload:bucket@namespace/path.json"
    assert second == first
    assert filesystem.calls == ["bucket@namespace/path.json"]


def test_read_bytes_uses_connector_cache() -> None:
    connector = _connector()
    filesystem = connector._filesystem

    first = connector.read_bytes("bucket/chunk", use_cache=True)
    second = connector.read_bytes("bucket/chunk", use_cache=True)

    assert first == b"payload:bucket@namespace/chunk"
    assert second == first
    assert filesystem.calls == ["bucket@namespace/chunk"]


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


def test_read_text_refreshes_and_retries_after_auth_error() -> None:
    connector = _connector()
    first_filesystem = _FakeFilesystem(responses=[_service_error(401)])
    second_filesystem = _FakeFilesystem(responses=[b"fresh-after-refresh"])
    connector._filesystem = first_filesystem
    refresh_calls: list[str] = []

    def _refresh() -> None:
        refresh_calls.append("refresh")
        connector._filesystem = second_filesystem

    connector.refresh = _refresh  # type: ignore[method-assign]

    assert connector.read_text("bucket/path.json") == "fresh-after-refresh"
    assert refresh_calls == ["refresh"]
    assert first_filesystem.calls == ["bucket@namespace/path.json"]
    assert second_filesystem.calls == ["bucket@namespace/path.json"]


def test_read_bytes_refreshes_and_retries_after_permission_error() -> None:
    connector = _connector()
    first_filesystem = _FakeFilesystem(responses=[PermissionError("expired token")])
    second_filesystem = _FakeFilesystem(responses=[b"fresh-bytes"])
    connector._filesystem = first_filesystem
    refresh_calls: list[str] = []

    def _refresh() -> None:
        refresh_calls.append("refresh")
        connector._filesystem = second_filesystem

    connector.refresh = _refresh  # type: ignore[method-assign]

    assert connector.read_bytes("bucket/chunk") == b"fresh-bytes"
    assert refresh_calls == ["refresh"]
    assert first_filesystem.calls == ["bucket@namespace/chunk"]
    assert second_filesystem.calls == ["bucket@namespace/chunk"]


def test_read_bytes_retries_again_after_second_auth_error() -> None:
    connector = _connector()
    first_filesystem = _FakeFilesystem(responses=[PermissionError("expired token")])
    second_filesystem = _FakeFilesystem(responses=[PermissionError("stale retry"), b"fresh-after-second-retry"])
    connector._filesystem = first_filesystem
    refresh_calls: list[str] = []

    def _refresh() -> None:
        refresh_calls.append("refresh")
        connector._filesystem = second_filesystem
        connector._auth_generation += 1

    connector.refresh = _refresh  # type: ignore[method-assign]

    assert connector.read_bytes("bucket/chunk") == b"fresh-after-second-retry"
    assert refresh_calls == ["refresh", "refresh"]
    assert first_filesystem.calls == ["bucket@namespace/chunk"]
    assert second_filesystem.calls == ["bucket@namespace/chunk", "bucket@namespace/chunk"]


def test_object_exists_refreshes_and_retries_after_auth_error() -> None:
    connector = _connector()
    first_client = _FakeClient()
    first_client.head_errors = [_service_error(401)]
    second_client = _FakeClient()
    second_client.head_paths.add("present.json")
    connector._client = first_client
    refresh_calls: list[str] = []

    def _refresh() -> None:
        refresh_calls.append("refresh")
        connector._client = second_client

    connector.refresh = _refresh  # type: ignore[method-assign]

    assert connector.object_exists("oci://bucket@namespace/present.json") is True
    assert refresh_calls == ["refresh"]


def test_write_text_refreshes_and_retries_after_auth_error() -> None:
    connector = _connector()
    first_client = _FakeClient()
    first_client.put_errors = [_service_error(401)]
    second_client = _FakeClient()
    connector._client = first_client
    refresh_calls: list[str] = []

    def _refresh() -> None:
        refresh_calls.append("refresh")
        connector._client = second_client

    connector.refresh = _refresh  # type: ignore[method-assign]

    connector.write_text("oci://bucket@namespace/present.json", "{\"ok\":true}")

    assert refresh_calls == ["refresh"]
    assert second_client.put_calls == [
        {
            "namespace_name": "namespace",
            "bucket_name": "bucket",
            "object_name": "present.json",
            "body": b"{\"ok\":true}",
            "content_type": "application/json",
        }
    ]


def test_run_with_auth_retry_skips_duplicate_refresh_when_generation_has_advanced() -> None:
    connector = _connector()
    connector._auth_generation = 2
    calls = {"attempts": 0, "refresh": 0}

    def _operation():
        calls["attempts"] += 1
        if calls["attempts"] == 1:
            connector._auth_generation = 3
            raise _service_error(401)
        return "ok"

    def _refresh() -> None:
        calls["refresh"] += 1

    connector._refresh_locked = _refresh  # type: ignore[method-assign]

    assert connector._run_with_auth_retry(_operation, operation_name="test") == "ok"
    assert calls == {"attempts": 2, "refresh": 0}
