from dataclasses import dataclass
from collections import OrderedDict
from threading import Lock
from threading import RLock
import logging
import time

import fsspec
import oci

from app.config import Settings
from app.core.oci_auth import OCIAuthContext, OCIAuthExpiredError, get_oci_auth_context

logger = logging.getLogger(__name__)


@dataclass
class OCIObjectSummary:
    name: str
    size: int | None
    etag: str | None


@dataclass
class ZarrStoreSummary:
    path: str
    consolidated: bool
    zarr_format: int


@dataclass
class OCIObjectInfo:
    name: str
    size: int | None
    etag: str | None
    content_type: str | None


class OCIObjectStorageConnector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth: OCIAuthContext | None = None
        self._client: oci.object_storage.ObjectStorageClient | None = None
        self._namespace: str | None = None
        self._filesystem = None
        self._text_cache: OrderedDict[str, str] = OrderedDict()
        self._bytes_cache: OrderedDict[str, bytes] = OrderedDict()
        self._bytes_cache_size = 0
        self._text_cache_lock = Lock()
        self._bytes_cache_lock = Lock()
        self._auth_lock = RLock()
        self._auth_generation = 0

    def _build_auth(self) -> OCIAuthContext:
        return get_oci_auth_context(
            profile_name=self._settings.oci_config_profile,
            config_file=self._settings.oci_config_file,
        )

    def refresh(self) -> None:
        with self._auth_lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        self._auth = self._build_auth()
        self._client = oci.object_storage.ObjectStorageClient(
            config=self._auth.config,
            signer=self._auth.signer,
        )
        self._namespace = self._settings.oci_namespace or self._client.get_namespace().data
        self._filesystem = None
        self._auth_generation += 1

    @property
    def client(self) -> oci.object_storage.ObjectStorageClient:
        if self._client is None:
            with self._auth_lock:
                if self._client is None:
                    self._refresh_locked()
        return self._client

    @property
    def auth(self) -> OCIAuthContext:
        if self._auth is None:
            with self._auth_lock:
                if self._auth is None:
                    self._refresh_locked()
        return self._auth

    def get_filesystem(self):
        if self._filesystem is not None:
            return self._filesystem
        with self._auth_lock:
            if self._filesystem is not None:
                return self._filesystem
            auth = self.auth
            if self.namespace:
                self._filesystem = fsspec.filesystem(
                    "oci",
                    config=auth.config,
                    signer=auth.signer,
                    namespace=self.namespace,
                )
            else:
                self._filesystem = fsspec.filesystem("oci", config=auth.config, signer=auth.signer)
        return self._filesystem

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            with self._auth_lock:
                if self._namespace is None:
                    self._refresh_locked()
        return self._namespace

    def build_oci_uri(self, object_path: str) -> str:
        cleaned = object_path.lstrip("/")
        return f"oci://{self._settings.oci_bucket}@{self.namespace}/{cleaned}"

    @staticmethod
    def _raise_not_found_as_file_error(error: Exception, object_path: str) -> None:
        if isinstance(error, oci.exceptions.ServiceError) and error.status == 404:
            raise FileNotFoundError(object_path) from error
        raise error

    @staticmethod
    def _is_auth_error(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, PermissionError):
                return True
            status = getattr(current, "status", None)
            code = getattr(current, "code", None)
            if status == 401 or code == "NotAuthenticated":
                return True
            current = current.__cause__ or current.__context__
        return False

    def _run_with_auth_retry(self, operation, *, operation_name: str):
        max_attempts = 3
        for attempt in range(max_attempts):
            generation = self._auth_generation
            try:
                return operation()
            except Exception as error:
                if self._is_auth_error(error):
                    if attempt >= max_attempts - 1:
                        raise
                    with self._auth_lock:
                        should_refresh = self._auth_generation == generation
                    try:
                        if should_refresh:
                            logger.warning(
                                "OCI auth expired during %s; refreshing session and retrying (%d/%d)",
                                operation_name,
                                attempt + 1,
                                max_attempts - 1,
                            )
                            self.refresh()
                        else:
                            logger.warning(
                                "OCI auth expired during %s; another request already refreshed the session, retrying (%d/%d)",
                                operation_name,
                                attempt + 1,
                                max_attempts - 1,
                            )
                    except OCIAuthExpiredError:
                        raise
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise

    def read_text(
        self,
        object_path: str,
        *,
        use_cache: bool = True,
    ) -> str:
        resolved = self._filesystem_path(object_path)
        if use_cache:
            with self._text_cache_lock:
                if resolved in self._text_cache:
                    self._text_cache.move_to_end(resolved)
                    return self._text_cache[resolved]

        try:
            payload = self._run_with_auth_retry(
                lambda: self.get_filesystem().cat_file(resolved).decode("utf-8"),
                operation_name=f"read_text({resolved})",
            )
        except Exception as error:
            self._raise_not_found_as_file_error(error, resolved)
        if use_cache:
            with self._text_cache_lock:
                self._text_cache[resolved] = payload
                self._text_cache.move_to_end(resolved)
                while len(self._text_cache) > 256:
                    self._text_cache.popitem(last=False)
        return payload

    def read_bytes(
        self,
        object_path: str,
        *,
        use_cache: bool = False,
    ) -> bytes:
        resolved = self._filesystem_path(object_path)
        if use_cache:
            with self._bytes_cache_lock:
                if resolved in self._bytes_cache:
                    self._bytes_cache.move_to_end(resolved)
                    return self._bytes_cache[resolved]

        try:
            payload = self._run_with_auth_retry(
                lambda: self.get_filesystem().cat_file(resolved),
                operation_name=f"read_bytes({resolved})",
            )
        except Exception as error:
            self._raise_not_found_as_file_error(error, resolved)
        if use_cache:
            with self._bytes_cache_lock:
                self._bytes_cache[resolved] = payload
                self._bytes_cache.move_to_end(resolved)
                self._bytes_cache_size += len(payload)
                while len(self._bytes_cache) > 128 or self._bytes_cache_size > 128 * 1024 * 1024:
                    _, evicted = self._bytes_cache.popitem(last=False)
                    self._bytes_cache_size -= len(evicted)
        return payload

    def write_bytes(
        self,
        object_path: str,
        payload: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        resolved = object_path.removeprefix("oci://")
        self._run_with_auth_retry(
            lambda: self.client.put_object(
                namespace_name=self.namespace,
                bucket_name=self._settings.oci_bucket,
                object_name=self._object_name_from_path(resolved),
                put_object_body=payload,
                content_type=content_type,
            ),
            operation_name=f"write_bytes({resolved})",
        )
        self._evict_cached_object(resolved)

    def write_text(
        self,
        object_path: str,
        payload: str,
        *,
        content_type: str = "application/json",
    ) -> None:
        resolved = object_path.removeprefix("oci://")
        self._run_with_auth_retry(
            lambda: self.client.put_object(
                namespace_name=self.namespace,
                bucket_name=self._settings.oci_bucket,
                object_name=self._object_name_from_path(resolved),
                put_object_body=payload.encode("utf-8"),
                content_type=content_type,
            ),
            operation_name=f"write_text({resolved})",
        )
        self._evict_cached_object(resolved)

    def object_exists(self, object_path: str) -> bool:
        resolved = object_path.removeprefix("oci://")
        try:
            self._run_with_auth_retry(
                lambda: self.client.head_object(
                    namespace_name=self.namespace,
                    bucket_name=self._settings.oci_bucket,
                    object_name=self._object_name_from_path(resolved),
                ),
                operation_name=f"object_exists({resolved})",
            )
            return True
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return False
            raise

    def head_object(self, object_path: str) -> OCIObjectInfo:
        resolved = object_path.removeprefix("oci://")
        try:
            response = self._run_with_auth_retry(
                lambda: self.client.head_object(
                    namespace_name=self.namespace,
                    bucket_name=self._settings.oci_bucket,
                    object_name=self._object_name_from_path(resolved),
                ),
                operation_name=f"head_object({resolved})",
            )
        except Exception as error:
            self._raise_not_found_as_file_error(error, resolved)

        data = response.data
        return OCIObjectInfo(
            name=self._object_name_from_path(resolved),
            size=getattr(data, "content_length", None),
            etag=getattr(data, "etag", None),
            content_type=getattr(data, "content_type", None),
        )

    def read_byte_range(
        self,
        object_path: str,
        *,
        start: int | None = None,
        end: int | None = None,
        use_cache: bool = False,
    ) -> bytes:
        resolved = self._filesystem_path(object_path)
        cache_key = f"{resolved}::{start if start is not None else ''}:{end if end is not None else ''}"
        if use_cache:
            with self._bytes_cache_lock:
                if cache_key in self._bytes_cache:
                    self._bytes_cache.move_to_end(cache_key)
                    return self._bytes_cache[cache_key]

        try:
            payload = self._run_with_auth_retry(
                lambda: self.get_filesystem().cat_file(resolved, start=start, end=end),
                operation_name=f"read_byte_range({resolved})",
            )
        except Exception as error:
            self._raise_not_found_as_file_error(error, resolved)
        if use_cache:
            with self._bytes_cache_lock:
                self._bytes_cache[cache_key] = payload
                self._bytes_cache.move_to_end(cache_key)
                self._bytes_cache_size += len(payload)
                while len(self._bytes_cache) > 128 or self._bytes_cache_size > 128 * 1024 * 1024:
                    _, evicted = self._bytes_cache.popitem(last=False)
                    self._bytes_cache_size -= len(evicted)
        return payload

    def read_byte_tail(
        self,
        object_path: str,
        *,
        length: int,
        use_cache: bool = False,
    ) -> bytes:
        if length <= 0:
            return b""

        resolved = self._filesystem_path(object_path)
        cache_key = f"{resolved}::tail:{length}"
        if use_cache:
            with self._bytes_cache_lock:
                if cache_key in self._bytes_cache:
                    self._bytes_cache.move_to_end(cache_key)
                    return self._bytes_cache[cache_key]

        try:
            payload = self._run_with_auth_retry(
                lambda: self.client.get_object(
                    namespace_name=self.namespace,
                    bucket_name=self._settings.oci_bucket,
                    object_name=self._object_name_from_path(resolved),
                    range=f"bytes=-{length}",
                ).data.content,
                operation_name=f"read_byte_tail({resolved})",
            )
        except Exception as error:
            self._raise_not_found_as_file_error(error, resolved)
        if use_cache:
            with self._bytes_cache_lock:
                self._bytes_cache[cache_key] = payload
                self._bytes_cache.move_to_end(cache_key)
                self._bytes_cache_size += len(payload)
                while len(self._bytes_cache) > 128 or self._bytes_cache_size > 128 * 1024 * 1024:
                    _, evicted = self._bytes_cache.popitem(last=False)
                    self._bytes_cache_size -= len(evicted)
        return payload

    def _object_name_from_path(self, object_path: str) -> str:
        resolved = object_path.removeprefix("oci://").lstrip("/")
        bucket_prefix = f"{self._settings.oci_bucket}@{self.namespace}/"
        if resolved.startswith(bucket_prefix):
            return resolved[len(bucket_prefix) :]
        bucket_name_prefix = f"{self._settings.oci_bucket}/"
        if resolved.startswith(bucket_name_prefix):
            return resolved[len(bucket_name_prefix) :]
        if "/" in resolved and "@" in resolved.split("/", 1)[0]:
            return resolved.split("/", 1)[1]
        return resolved

    def _filesystem_path(self, object_path: str) -> str:
        resolved = object_path.removeprefix("oci://").lstrip("/")
        bucket_prefix = f"{self._settings.oci_bucket}@{self.namespace}/"
        if resolved.startswith(bucket_prefix):
            return resolved
        bucket_name_prefix = f"{self._settings.oci_bucket}/"
        if resolved.startswith(bucket_name_prefix):
            return f"{bucket_prefix}{resolved[len(bucket_name_prefix):]}"
        if "/" in resolved and "@" in resolved.split("/", 1)[0]:
            return resolved
        return f"{bucket_prefix}{resolved}"

    def _evict_cached_object(self, resolved_path: str) -> None:
        with self._text_cache_lock:
            self._text_cache.pop(resolved_path, None)
        with self._bytes_cache_lock:
            keys_to_remove = [key for key in self._bytes_cache if key == resolved_path or key.startswith(f"{resolved_path}::")]
            for key in keys_to_remove:
                removed = self._bytes_cache.pop(key, None)
                if removed is not None:
                    self._bytes_cache_size -= len(removed)

    def list_objects(self, prefix: str | None = None, limit: int = 200) -> list[OCIObjectSummary]:
        effective_prefix = self._settings.oci_prefix if prefix is None else prefix
        next_start_with = None
        remaining = limit
        results: list[OCIObjectSummary] = []

        while remaining > 0:
            response = self._run_with_auth_retry(
                lambda: self.client.list_objects(
                    namespace_name=self.namespace,
                    bucket_name=self._settings.oci_bucket,
                    prefix=effective_prefix,
                    start=next_start_with,
                    limit=min(remaining, 1000),
                ),
                operation_name=f"list_objects({effective_prefix})",
            )
            for item in response.data.objects:
                results.append(
                    OCIObjectSummary(
                        name=item.name,
                        size=getattr(item, "size", None),
                        etag=getattr(item, "etag", None),
                    )
                )
                remaining -= 1
                if remaining <= 0:
                    break
            if not response.data.next_start_with or remaining <= 0:
                break
            next_start_with = response.data.next_start_with

        return results

    def list_prefixes(self, prefix: str | None = None) -> list[str]:
        effective_prefix = self._settings.oci_prefix if prefix is None else prefix
        next_start_with = None
        prefixes: list[str] = []
        while True:
            response = self._run_with_auth_retry(
                lambda: self.client.list_objects(
                    namespace_name=self.namespace,
                    bucket_name=self._settings.oci_bucket,
                    prefix=effective_prefix,
                    start=next_start_with,
                    delimiter="/",
                ),
                operation_name=f"list_prefixes({effective_prefix})",
            )
            prefixes.extend(response.data.prefixes or [])
            if not response.data.next_start_with:
                break
            next_start_with = response.data.next_start_with
        return prefixes

    def list_zarr_stores(self, prefix: str | None = None, limit: int = 2000) -> list[ZarrStoreSummary]:
        effective_prefix = self._settings.oci_prefix if prefix is None else prefix
        objects = self.list_objects(prefix=effective_prefix, limit=limit)
        stores: dict[str, ZarrStoreSummary] = {}

        for item in objects:
            if item.name.endswith("/zarr.json"):
                store_path = item.name[: -len("/zarr.json")]
                stores[store_path] = ZarrStoreSummary(
                    path=store_path,
                    consolidated=False,
                    zarr_format=3,
                )
            if item.name.endswith(".zmetadata"):
                store_path = item.name[: -len("/.zmetadata")] if item.name.endswith("/.zmetadata") else item.name
                stores[store_path] = ZarrStoreSummary(
                    path=store_path,
                    consolidated=True,
                    zarr_format=2,
                )
            elif item.name.endswith(".zgroup"):
                store_path = item.name[: -len("/.zgroup")] if item.name.endswith("/.zgroup") else item.name
                stores.setdefault(
                    store_path,
                    ZarrStoreSummary(
                        path=store_path,
                        consolidated=False,
                        zarr_format=2,
                    ),
                )

        return sorted(stores.values(), key=lambda item: item.path)
