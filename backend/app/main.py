from contextlib import asynccontextmanager

import xarray as xr
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.router import router as api_router
from app.core.browse_tiles import prewarm_browse_overviews
from app.core.browse_tiles import start_background_browse_prewarm
from app.config import get_settings
from app.core.cache import CacheClient
from app.core.cache import connect_cache
from app.core.dataset_catalog import has_direct_store_target
from app.core.dataset_catalog import warm_catalog_index
from app.core.datasets import build_registry
from app.core.datasets import build_registry_from_dataset
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.oci_auth import OCIAuthExpiredError
from app.core.zarr_reader import open_oci_zarr_dataset
from app.index.catalog_store import build_index_records
from app.index.planner_index import PlannerIndex
from app.services.export_jobs import ExportJobStore
from app.services.planner import PlannerService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.planner_index = PlannerIndex()
    app.state.planner = PlannerService(settings, app.state.planner_index)
    app.state.export_job_store = ExportJobStore()
    app.state.storage_connector = None
    app.state.dataset_catalog = None
    app.state.dataset_manifest = None
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
        if not settings.oci_zarr_path:
            direct_store_target = has_direct_store_target(settings)
            eager_catalog_entry_state = settings.browse_prewarm_enabled and direct_store_target
            await run_in_threadpool(
                warm_catalog_index,
                app,
                eager_catalog_entry_state,
            )
            if settings.browse_prewarm_enabled and direct_store_target:
                await run_in_threadpool(
                    prewarm_browse_overviews,
                    settings,
                    connector,
                    app.state.dataset_catalog,
                    settings.browse_prewarm_all_variables,
                )
                if not settings.browse_prewarm_all_variables:
                    start_background_browse_prewarm(
                        settings,
                        connector,
                        app.state.dataset_catalog,
                    )
    else:
        app.state.registry = build_registry()
    app.state.planner_index.replace(build_index_records(app))
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


@app.exception_handler(OCIAuthExpiredError)
async def handle_oci_auth_expired(_request: Request, exc: OCIAuthExpiredError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
    )


app.include_router(api_router, prefix="/api")
app.state.settings = settings
app.state.planner_index = PlannerIndex()
app.state.planner = PlannerService(settings, app.state.planner_index)
app.state.export_job_store = ExportJobStore()
app.state.storage_connector = None
app.state.dataset_catalog = None
app.state.dataset_manifest = None
app.state.cache = CacheClient(client=None, ttl=settings.tile_cache_ttl)
if settings.storage_backend == "oci_zarr":
    dataset_id = settings.oci_dataset_id or settings.oci_bucket or "oci-zarr"
    dataset_name = settings.oci_dataset_name or dataset_id
    dataset_description = (
        settings.oci_dataset_description
        or f"OCI Object Storage browser for {settings.oci_bucket}/{settings.oci_prefix}"
    )
    app.state.registry = build_registry_from_dataset(
        dataset=xr.Dataset(),
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        dataset_description=dataset_description,
    )
else:
    app.state.registry = build_registry()
app.state.planner_index.replace(build_index_records(app))
