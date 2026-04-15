from collections import OrderedDict

from app.core.oci_object_storage import OCIObjectStorageConnector


class _FakeFilesystem:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cat_file(self, path: str) -> bytes:
        self.calls.append(path)
        return f"payload:{path}".encode("utf-8")


def test_read_text_uses_connector_cache() -> None:
    connector = OCIObjectStorageConnector.__new__(OCIObjectStorageConnector)
    connector._text_cache = OrderedDict()
    connector._bytes_cache = OrderedDict()
    connector._bytes_cache_size = 0
    filesystem = _FakeFilesystem()
    connector._filesystem = filesystem

    first = connector.read_text("bucket/path.json", use_cache=True)
    second = connector.read_text("bucket/path.json", use_cache=True)

    assert first == "payload:bucket/path.json"
    assert second == first
    assert filesystem.calls == ["bucket/path.json"]


def test_read_bytes_uses_connector_cache() -> None:
    connector = OCIObjectStorageConnector.__new__(OCIObjectStorageConnector)
    connector._text_cache = OrderedDict()
    connector._bytes_cache = OrderedDict()
    connector._bytes_cache_size = 0
    filesystem = _FakeFilesystem()
    connector._filesystem = filesystem

    first = connector.read_bytes("bucket/chunk", use_cache=True)
    second = connector.read_bytes("bucket/chunk", use_cache=True)

    assert first == b"payload:bucket/chunk"
    assert second == first
    assert filesystem.calls == ["bucket/chunk"]
