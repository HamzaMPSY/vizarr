import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.core.auth import filter_dataset_ids
from app.core.browse_tiles import build_and_store_browse_overviews
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.dataset_catalog import get_or_build_catalog
from app.core.serving_profile import build_dataset_serving_profile
from app.index.spatial_index import BBox
from app.index.spatial_index import bounds_intersect_bbox
from app.index.spatial_index import parse_bbox_query
from app.models.artifacts import BrowseGenerationAcceptedResponse
from app.models.artifacts import BrowseGenerationStatusResponse
from app.models.dataset import BrowseCoverage, DatasetMeta, DatasetServingProfile, VariableMeta
from app.models.jobs import BrowseGenerationJobRecord
from app.models.requests import BrowseGenerationRequest


router = APIRouter(prefix="/datasets", tags=["datasets"])
logger = logging.getLogger(__name__)


@router.get("", response_model=list[DatasetMeta])
async def list_datasets(
    request: Request,
    response: Response,
    bbox: str | None = Query(default=None, description="Optional west,south,east,north WGS84 viewport filter"),
) -> list[DatasetMeta]:
    auth_context = getattr(request.state, "auth_context", None)
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        manifest = getattr(request.app.state, "dataset_manifest", None)
        connector = request.app.state.storage_connector
        diagnostics = getattr(request.app.state, "dataset_manifest_diagnostics", {}) or {}
        _set_manifest_headers(response, diagnostics)
        needs_metadata_hydration = manifest is None or any(not item.variables for item in manifest)

        if manifest is not None and not needs_metadata_hydration:
            datasets = manifest
            return _filter_datasets_for_request(request, auth_context, datasets, bbox)

        catalog = getattr(request.app.state, "dataset_catalog", None)
        if catalog is None and manifest is not None:
            datasets = manifest
            return _filter_datasets_for_request(request, auth_context, datasets, bbox)

        catalog = get_or_build_catalog(request.app)
        diagnostics = getattr(request.app.state, "dataset_manifest_diagnostics", {}) or {}
        _set_manifest_headers(response, diagnostics)

        if needs_metadata_hydration and connector is not None:
            for entry in catalog.values():
                if entry.meta.variables:
                    continue
                try:
                    await run_in_threadpool(
                        ensure_catalog_entry_metadata_ready,
                        entry,
                        connector,
                    )
                except Exception:
                    continue
            manifest = [entry.meta.model_copy(deep=True) for entry in catalog.values()]
            request.app.state.dataset_manifest = manifest

        if manifest is not None:
            datasets = manifest
        else:
            datasets = [entry.meta for entry in catalog.values()]
        return _filter_datasets_for_request(request, auth_context, datasets, bbox)
    meta = request.app.state.registry.meta
    return _filter_datasets_for_request(request, auth_context, [meta], bbox)


def _filter_datasets_for_request(
    request: Request,
    auth_context,
    datasets: list[DatasetMeta],
    raw_bbox: str | None,
) -> list[DatasetMeta]:
    allowed_ids = filter_dataset_ids(auth_context, [item.id for item in datasets])
    filtered = datasets if allowed_ids is None else [item for item in datasets if item.id in allowed_ids]
    if raw_bbox is None:
        return filtered
    try:
        bbox = parse_bbox_query(raw_bbox)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if bbox is None:
        return filtered
    return _filter_datasets_by_bbox(request, filtered, bbox)


def _filter_datasets_by_bbox(request: Request, datasets: list[DatasetMeta], bbox: BBox) -> list[DatasetMeta]:
    spatial_index = getattr(request.app.state, "dataset_spatial_index", None)
    indexed_ids: set[str] = set()
    matching_ids: set[str] = set()
    if spatial_index is not None:
        indexed_records = spatial_index.all()
        indexed_ids = {record.dataset_id for record in indexed_records}
        dataset_ids = {item.id for item in datasets if item.bounds is not None}
        if dataset_ids.issubset(indexed_ids):
            matching_ids = spatial_index.query_ids(bbox)
            return [item for item in datasets if item.id in matching_ids]

    return [item for item in datasets if bounds_intersect_bbox(item.bounds, bbox)]


def _set_manifest_headers(response: Response, diagnostics: dict[str, Any]) -> None:
    source = diagnostics.get("source")
    status = diagnostics.get("status")
    generated_at = diagnostics.get("generated_at")
    if source is not None:
        response.headers["X-Dataset-Manifest-Source"] = str(source)
    if status is not None:
        response.headers["X-Dataset-Manifest-Status"] = str(status)
    if generated_at is not None:
        response.headers["X-Dataset-Manifest-Generated-At"] = str(generated_at)


@router.get("/{dataset_id}", response_model=DatasetMeta)
async def get_dataset(dataset_id: str, request: Request) -> DatasetMeta:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            await run_in_threadpool(
                ensure_catalog_entry_ready,
                entry,
                request.app.state.storage_connector,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return entry.meta
    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return meta


@router.get("/{dataset_id}/variables", response_model=list[VariableMeta])
async def list_variables(dataset_id: str, request: Request) -> list[VariableMeta]:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            await run_in_threadpool(
                ensure_catalog_entry_metadata_ready,
                entry,
                request.app.state.storage_connector,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return entry.meta.variables
    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return meta.variables


@router.get("/{dataset_id}/serving-profile", response_model=DatasetServingProfile)
async def get_dataset_serving_profile(dataset_id: str, request: Request) -> DatasetServingProfile:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            return await run_in_threadpool(
                build_dataset_serving_profile,
                settings,
                request.app.state.storage_connector,
                entry,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return DatasetServingProfile(
        dataset_id=meta.id,
        zarr_format=meta.zarr_format,
        zarr_consolidated=meta.zarr_consolidated,
        zarr_proxy_root=meta.zarr_proxy_root,
        multiscale_store_path=meta.multiscale_store_path,
        multiscale_zarr_format=meta.multiscale_zarr_format,
        multiscale_zarr_consolidated=meta.multiscale_zarr_consolidated,
        multiscale_proxy_root=meta.multiscale_proxy_root,
        multiscale_population_strategy=meta.multiscale_population_strategy,
        multiscale_prepopulated_zoom_max=meta.multiscale_prepopulated_zoom_max,
        multiscale_max_zoom=meta.multiscale_max_zoom,
        data_array_name=None,
        variable_ids=[item.id for item in meta.variables],
        has_multiscale=False,
        multiscale_paths=[],
        multiscale_levels=[],
        browse_overview_zoom_levels=[],
        browse_overview_max_zoom=None,
        browse_coverage=BrowseCoverage(),
        chunk_layout=None,
        supported_rendering_modes=["dynamic_tiles"],
        browser_multiscale_ready=False,
        browser_gpu_ready=False,
        browser_gpu_reason="missing_multiscale_proxy; missing_multiscale_pyramid",
        browser_gpu_gaps=["missing_multiscale_proxy", "missing_multiscale_pyramid"],
        seamless_rendering_ready=False,
        seamless_rendering_gaps=["missing_browser_proxy", "missing_multiscale_pyramid", "missing_browse_overviews"],
    )


@router.post("/{dataset_id}/browse-generation", response_model=BrowseGenerationAcceptedResponse, status_code=202)
async def create_browse_generation(
    dataset_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: BrowseGenerationRequest | None = None,
) -> BrowseGenerationAcceptedResponse:
    settings = request.app.state.settings
    if settings.storage_backend != "oci_zarr":
        raise HTTPException(status_code=400, detail="Browse generation is only available for OCI-backed datasets")
    payload = payload or BrowseGenerationRequest()
    catalog = get_or_build_catalog(request.app)
    entry = catalog.get(dataset_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        await run_in_threadpool(
            ensure_catalog_entry_metadata_ready,
            entry,
            request.app.state.storage_connector,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    variables = _browse_generation_variables(entry, payload.variables)
    time_indices = _browse_generation_time_indices(entry, variables, payload.time_indices)
    zoom_levels = _browse_generation_zoom_levels(settings, payload.zoom_levels)
    if payload.retry_job_id is not None:
        retry_job = request.app.state.browse_generation_job_store.get_job(payload.retry_job_id)
        if retry_job is None or retry_job.dataset_id != entry.id:
            raise HTTPException(status_code=404, detail="Browse generation retry job not found")
        if not retry_job.can_retry:
            raise HTTPException(status_code=409, detail="Browse generation job is not retryable")
    job, created = request.app.state.browse_generation_job_store.create_or_get_active_job(
        dataset_id=entry.id,
        variables=variables,
        time_indices=time_indices,
        zoom_levels=zoom_levels,
        overwrite=payload.overwrite,
        retry_of_job_id=payload.retry_job_id,
    )
    if created:
        background_tasks.add_task(
            _run_browse_generation_job,
            request.app.state.browse_generation_job_store,
            job.job_id,
            settings,
            request.app.state.cache,
            request.app.state.storage_connector,
            entry,
            variables,
            time_indices,
            zoom_levels,
            payload.overwrite,
        )
    return BrowseGenerationAcceptedResponse(
        job_id=job.job_id,
        status=job.status,
        dataset_id=job.dataset_id,
        progress=job.progress,
        total_artifacts=job.total_artifacts,
        completed_artifacts=job.completed_artifacts,
        can_retry=job.can_retry,
    )


@router.get("/{dataset_id}/browse-generation/{job_id}", response_model=BrowseGenerationStatusResponse)
async def get_browse_generation(dataset_id: str, job_id: str, request: Request) -> BrowseGenerationStatusResponse:
    job = request.app.state.browse_generation_job_store.get_job(job_id)
    if job is None or job.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Browse generation job not found")
    return _browse_generation_status_response(job)


async def _run_browse_generation_job(
    job_store,
    job_id: str,
    settings,
    cache,
    connector,
    entry,
    variables: list[str],
    time_indices: list[int],
    zoom_levels: list[int],
    overwrite: bool,
) -> None:
    try:
        job_store.mark_running(job_id)
        summary = await run_in_threadpool(
            build_and_store_browse_overviews,
            settings=settings,
            connector=connector,
            entry=entry,
            variables=variables,
            time_indices=time_indices,
            zoom_levels=zoom_levels,
            overwrite=overwrite,
            progress_callback=lambda generated: job_store.record_artifact(job_id, generated=generated),
        )
        await cache.invalidate_dataset_tiles(entry.id)
        job_store.mark_succeeded(
            job_id,
            manifest_path=_string_or_none(summary.get("manifest_path")),
            generated=int(summary.get("generated", 0)),
            reused=int(summary.get("reused", 0)),
        )
    except Exception as exc:
        try:
            job_store.mark_failed(job_id, str(exc))
        except Exception:
            logger.warning("Failed to persist browse generation failure for job_id=%s", job_id, exc_info=True)


def _browse_generation_status_response(job: BrowseGenerationJobRecord) -> BrowseGenerationStatusResponse:
    return BrowseGenerationStatusResponse(
        job_id=job.job_id,
        status=job.status,
        job_type=job.job_type,
        dataset_id=job.dataset_id,
        progress=job.progress,
        total_artifacts=job.total_artifacts,
        completed_artifacts=job.completed_artifacts,
        generated_artifacts=job.generated_artifacts,
        reused_artifacts=job.reused_artifacts,
        variables=job.variables,
        time_indices=job.time_indices,
        zoom_levels=job.zoom_levels,
        manifest_path=job.manifest_path,
        error_message=job.error_message,
        attempt=job.attempt,
        retry_of_job_id=job.retry_of_job_id,
        can_retry=job.can_retry,
    )


def _browse_generation_variables(entry, requested_variables: list[str] | None) -> list[str]:
    available = [item.id for item in entry.meta.variables] or list(entry.band_names)
    selected = _unique_preserving_order(requested_variables or available)
    unknown = sorted(set(selected).difference(available))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown browse variable(s): {', '.join(unknown)}")
    return selected


def _browse_generation_time_indices(
    entry,
    variables: list[str],
    requested_time_indices: list[int] | None,
) -> list[int]:
    max_time_steps = max((_variable_time_steps(entry, variable) for variable in variables), default=1)
    selected = requested_time_indices or list(range(max_time_steps))
    invalid = [value for value in selected if value >= max_time_steps]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown browse time index: {invalid[0]}")
    return sorted(set(selected))


def _browse_generation_zoom_levels(settings, requested_zoom_levels: list[int] | None) -> list[int]:
    max_zoom = int(settings.browse_tile_max_zoom)
    selected = requested_zoom_levels or list(range(0, max_zoom + 1))
    invalid = [value for value in selected if value > max_zoom]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Browse zoom exceeds configured max: {invalid[0]}")
    return sorted(set(selected))


def _variable_time_steps(entry, variable: str) -> int:
    variable_meta = next((item for item in entry.meta.variables if item.id == variable), None)
    if variable_meta is not None:
        return max(int(variable_meta.time_steps), 1)
    if entry.meta.time_values:
        return max(len(entry.meta.time_values), 1)
    return 1


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _unique_preserving_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
