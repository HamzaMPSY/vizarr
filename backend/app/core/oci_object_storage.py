from dataclasses import dataclass

import fsspec
import oci

from app.config import Settings
from app.core.oci_auth import OCIAuthContext, get_oci_auth_context


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


class OCIObjectStorageConnector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth: OCIAuthContext | None = None
        self._client: oci.object_storage.ObjectStorageClient | None = None
        self._namespace: str | None = None

    def _build_auth(self) -> OCIAuthContext:
        return get_oci_auth_context(
            profile_name=self._settings.oci_config_profile,
            config_file=self._settings.oci_config_file,
        )

    def refresh(self) -> None:
        self._auth = self._build_auth()
        self._client = oci.object_storage.ObjectStorageClient(
            config=self._auth.config,
            signer=self._auth.signer,
        )
        self._namespace = self._settings.oci_namespace or self._client.get_namespace().data

    @property
    def client(self) -> oci.object_storage.ObjectStorageClient:
        if self._client is None:
            self.refresh()
        return self._client

    @property
    def auth(self) -> OCIAuthContext:
        if self._auth is None:
            self.refresh()
        return self._auth

    def get_filesystem(self):
        auth = self.auth
        if self.namespace:
            return fsspec.filesystem(
                "oci",
                config=auth.config,
                signer=auth.signer,
                namespace=self.namespace,
            )
        return fsspec.filesystem("oci", config=auth.config, signer=auth.signer)

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            self.refresh()
        return self._namespace

    def build_oci_uri(self, object_path: str) -> str:
        cleaned = object_path.lstrip("/")
        return f"oci://{self._settings.oci_bucket}@{self.namespace}/{cleaned}"

    def list_objects(self, prefix: str | None = None, limit: int = 200) -> list[OCIObjectSummary]:
        effective_prefix = self._settings.oci_prefix if prefix is None else prefix
        next_start_with = None
        remaining = limit
        results: list[OCIObjectSummary] = []

        while remaining > 0:
            response = self.client.list_objects(
                namespace_name=self.namespace,
                bucket_name=self._settings.oci_bucket,
                prefix=effective_prefix,
                start=next_start_with,
                limit=min(remaining, 1000),
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
            response = self.client.list_objects(
                namespace_name=self.namespace,
                bucket_name=self._settings.oci_bucket,
                prefix=effective_prefix,
                start=next_start_with,
                delimiter="/",
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
