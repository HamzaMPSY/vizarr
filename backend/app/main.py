from contextlib import asynccontextmanager
from threading import Thread

import xarray as xr
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.router import router as api_router
from app.api.websockets import router as websocket_router
from app.core.browse_tiles import prewarm_browse_overviews
from app.core.browse_tiles import start_background_browse_prewarm
from app.config import get_settings
from app.core.auth import authenticate_http_request
from app.core.cache import CacheClient
from app.core.cache import InFlightRequestCoalescer
from app.core.cache import connect_cache
from app.core.dataset_catalog import has_direct_store_target
from app.core.dataset_catalog import load_dataset_manifest_from_object
from app.core.dataset_catalog import warm_catalog_index
from app.core.datasets import build_registry
from app.core.datasets import build_registry_from_dataset
from app.core.oci_object_storage import OCIObjectStorageConnector
from app.core.oci_auth import OCIAuthExpiredError
from app.core.rate_limit import ApiKeyRateLimiter
from app.core.rate_limit import connect_rate_limiter
from app.core.zarr_reader import open_oci_zarr_dataset
from app.index.catalog_store import build_index_records
from app.index.planner_index import PlannerIndex
from app.index.spatial_index import DatasetSpatialIndex
from app.index.spatial_index import build_dataset_spatial_index_records
from app.services.browse_jobs import BrowseGenerationJobStore
from app.services.export_jobs import ExportJobStore
from app.services.job_records import JobRecordStore
from app.services.job_records import JobStoreUnavailable
from app.services.job_records import connect_job_record_store
from app.services.planner import PlannerService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.planner_index = PlannerIndex()
    app.state.planner = PlannerService(settings, app.state.planner_index)
    app.state.dataset_spatial_index = DatasetSpatialIndex()
    app.state.job_record_store = connect_job_record_store(settings.redis_url, settings.job_store_ttl)
    app.state.export_job_store = ExportJobStore(app.state.job_record_store)
    app.state.browse_generation_job_store = BrowseGenerationJobStore(app.state.job_record_store)
    app.state.api_key_rate_limiter = await connect_rate_limiter(
        settings.redis_url,
        limit=settings.api_key_rate_limit_per_minute,
        window_seconds=settings.api_key_rate_limit_window_seconds,
    )
    app.state.tile_request_coalescer = InFlightRequestCoalescer()
    app.state.storage_connector = None
    app.state.dataset_catalog = None
    app.state.dataset_manifest = None
    allow_startup_catalog_build = True
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
            if direct_store_target:
                await run_in_threadpool(
                    warm_catalog_index,
                    app,
                    eager_catalog_entry_state,
                    True,
                )
                if settings.browse_prewarm_enabled:
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
                await run_in_threadpool(load_dataset_manifest_from_object, app)
                start_background_catalog_refresh(app)
                allow_startup_catalog_build = False
    else:
        app.state.registry = build_registry()
    refresh_query_indexes(app, allow_catalog_build=allow_startup_catalog_build)
    app.state.cache = await connect_cache(settings.redis_url, settings.tile_cache_ttl)
    yield
    await app.state.cache.close()
    await app.state.api_key_rate_limiter.close()
    app.state.job_record_store.close()


def refresh_query_indexes(app: FastAPI, *, allow_catalog_build: bool = True) -> None:
    app.state.planner_index.replace(build_index_records(app, allow_catalog_build=allow_catalog_build))
    app.state.dataset_spatial_index.replace(
        build_dataset_spatial_index_records(app, allow_catalog_build=allow_catalog_build)
    )


def _split_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.replace("\n", ",").split(",") if item.strip()]


def _cors_allowed_origins(settings) -> list[str]:
    configured = _split_csv(getattr(settings, "cors_allowed_origins", ""))
    environment = str(getattr(settings, "app_environment", "development")).strip().lower()
    production = environment in {"prod", "production"}
    if configured:
        return [origin for origin in configured if not (production and origin == "*")]
    return [] if production else ["*"]


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(settings),
    allow_credentials=False,
    allow_methods=_split_csv(settings.cors_allowed_methods),
    allow_headers=_split_csv(settings.cors_allowed_headers),
    expose_headers=_split_csv(settings.cors_exposed_headers),
)


@app.exception_handler(OCIAuthExpiredError)
async def handle_oci_auth_expired(_request: Request, exc: OCIAuthExpiredError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
    )


@app.exception_handler(JobStoreUnavailable)
async def handle_job_store_unavailable(_request: Request, exc: JobStoreUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
    )


@app.middleware("http")
async def require_api_auth(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        try:
            context = authenticate_http_request(request)
            if context is not None:
                limiter = getattr(request.app.state, "api_key_rate_limiter", None)
                if limiter is not None and limiter.enabled:
                    result = await limiter.check(context.token_digest)
                    if not result.allowed:
                        return JSONResponse(
                            status_code=429,
                            content={"detail": "API key rate limit exceeded"},
                            headers={
                                "Retry-After": str(result.retry_after),
                                "X-RateLimit-Limit": str(result.limit),
                                "X-RateLimit-Remaining": str(result.remaining),
                            },
                        )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    return await call_next(request)


app.include_router(api_router, prefix="/api")
app.include_router(websocket_router)
app.state.settings = settings
app.state.planner_index = PlannerIndex()
app.state.planner = PlannerService(settings, app.state.planner_index)
app.state.dataset_spatial_index = DatasetSpatialIndex()
app.state.job_record_store = JobRecordStore(ttl=settings.job_store_ttl)
app.state.export_job_store = ExportJobStore(app.state.job_record_store)
app.state.browse_generation_job_store = BrowseGenerationJobStore(app.state.job_record_store)
app.state.api_key_rate_limiter = ApiKeyRateLimiter(
    limit=settings.api_key_rate_limit_per_minute,
    window_seconds=settings.api_key_rate_limit_window_seconds,
)
app.state.tile_request_coalescer = InFlightRequestCoalescer()
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
refresh_query_indexes(app)


def start_background_catalog_refresh(app: FastAPI) -> Thread:
    diagnostics = dict(getattr(app.state, "dataset_manifest_diagnostics", {}) or {})
    app.state.dataset_manifest_diagnostics = {
        **diagnostics,
        "refresh_status": "queued",
    }
    thread = Thread(
        target=_run_background_catalog_refresh,
        args=(app,),
        daemon=True,
        name="dataset-catalog-refresh",
    )
    thread.start()
    return thread


def _run_background_catalog_refresh(app: FastAPI) -> None:
    try:
        diagnostics = dict(getattr(app.state, "dataset_manifest_diagnostics", {}) or {})
        app.state.dataset_manifest_diagnostics = {
            **diagnostics,
            "refresh_status": "running",
        }
        warm_catalog_index(
            app,
            eager_entry_state=False,
            persist_manifest=True,
            eager_manifest_metadata=True,
        )
        refresh_query_indexes(app)
        _invalidate_catalog_tile_cache(app)
        diagnostics = dict(getattr(app.state, "dataset_manifest_diagnostics", {}) or {})
        app.state.dataset_manifest_diagnostics = {
            **diagnostics,
            "refresh_status": "succeeded",
        }
    except Exception as exc:
        diagnostics = dict(getattr(app.state, "dataset_manifest_diagnostics", {}) or {})
        app.state.dataset_manifest_diagnostics = {
            **diagnostics,
            "refresh_status": "failed",
            "error": str(exc),
        }


def _invalidate_catalog_tile_cache(app: FastAPI) -> None:
    cache = getattr(app.state, "cache", None)
    catalog = getattr(app.state, "dataset_catalog", None)
    if cache is None or catalog is None:
        return
    for dataset_id in list(catalog):
        try:
            cache.invalidate_dataset_tiles_blocking(dataset_id)
        except Exception:
            continue
