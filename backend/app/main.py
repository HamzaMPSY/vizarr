from contextlib import asynccontextmanager

import xarray as xr
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router as api_router
from app.config import get_settings
from app.core.cache import connect_cache
from app.core.datasets import build_registry
from app.core.datasets import build_registry_from_dataset
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.zarr_reader import open_oci_zarr_dataset


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.storage_connector = None
    app.state.dataset_catalog = None
    if settings.storage_backend == "oci_zarr":
        connector = OCIObjectStorageConnector(settings)
        app.state.storage_connector = connector
        dataset_id = settings.oci_dataset_id or settings.oci_bucket or "oci-zarr"
        dataset_name = settings.oci_dataset_name or dataset_id
        dataset_description = (
            settings.oci_dataset_description
            or f"OCI Object Storage browser for {settings.oci_bucket}/{settings.oci_prefix}"
        )

        if settings.oci_zarr_path:
            _, dataset = open_oci_zarr_dataset(settings)
            app.state.registry = build_registry_from_dataset(
                dataset=dataset,
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                dataset_description=dataset_description,
            )
        else:
            app.state.registry = build_registry_from_dataset(
                dataset=xr.Dataset(),
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                dataset_description=dataset_description,
            )
    else:
        app.state.registry = build_registry()
    app.state.cache = await connect_cache(settings.redis_url, settings.tile_cache_ttl)
    yield
    await app.state.cache.close()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
