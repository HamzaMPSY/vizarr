import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.models.artifacts import ExportAcceptedResponse
from app.models.artifacts import PlannedQueryResponse
from app.models.artifacts import RangeStatsResponse
from app.models.artifacts import SourceBBoxReadbackResponse
from app.models.artifacts import SourcePointReadbackResponse
from app.models.requests import ClipRequest
from app.models.requests import PreviewRequest
from app.models.requests import StatsRequest
from app.core.dataset_catalog import get_or_build_catalog
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.readback import ReadbackWindowTooLarge
from app.core.readback import read_projected_bbox
from app.core.readback import read_projected_point
from app.core.readback import read_synthetic_bbox
from app.core.readback import read_synthetic_point
from app.index.spatial_index import parse_bbox_query


router = APIRouter(prefix="/query", tags=["query"])


@router.get("/point", response_model=SourcePointReadbackResponse)
async def query_point(
    request: Request,
    dataset_id: str = Query(min_length=1),
    variable: str = Query(min_length=1),
    lon: float = Query(ge=-180.0, le=180.0),
    lat: float = Query(ge=-90.0, le=90.0),
    time_index: int = Query(default=0, ge=0),
    diagnostics: bool = Query(default=False),
) -> SourcePointReadbackResponse:
    try:
        return await run_in_threadpool(
            _query_point_sync,
            request,
            dataset_id,
            variable,
            lon,
            lat,
            time_index,
            diagnostics,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Variable not found") from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/bbox", response_model=SourceBBoxReadbackResponse)
async def query_bbox(
    request: Request,
    dataset_id: str = Query(min_length=1),
    variable: str = Query(min_length=1),
    bbox: str = Query(description="west,south,east,north in WGS84"),
    time_index: int = Query(default=0, ge=0),
    max_width: int = Query(default=64, ge=1, le=512),
    max_height: int = Query(default=64, ge=1, le=512),
    diagnostics: bool = Query(default=False),
) -> SourceBBoxReadbackResponse:
    try:
        parsed_bbox = parse_bbox_query(bbox)
        assert parsed_bbox is not None
        return await run_in_threadpool(
            _query_bbox_sync,
            request,
            dataset_id,
            variable,
            parsed_bbox,
            time_index,
            max_width,
            max_height,
            diagnostics,
        )
    except ReadbackWindowTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Variable not found") from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/range", response_model=RangeStatsResponse)
async def query_range(
    request: Request,
    dataset_id: str = Query(min_length=1),
    variable: str = Query(min_length=1),
    time_index: int = Query(default=0, ge=0),
    bbox: str | None = Query(default=None, description="Optional west,south,east,north WGS84 sample window"),
    bins: int = Query(default=32, ge=2, le=128),
    max_width: int = Query(default=128, ge=1, le=512),
    max_height: int = Query(default=128, ge=1, le=512),
) -> RangeStatsResponse:
    try:
        parsed_bbox = parse_bbox_query(bbox) if bbox else None
        return await run_in_threadpool(
            _query_range_sync,
            request,
            dataset_id,
            variable,
            time_index,
            parsed_bbox,
            bins,
            max_width,
            max_height,
        )
    except ReadbackWindowTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Variable not found") from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/preview", response_model=PlannedQueryResponse)
async def create_preview(request: Request, payload: PreviewRequest) -> PlannedQueryResponse:
    plan = request.app.state.planner.plan_preview(payload)
    artifact_id = f"art_{plan.request_fingerprint[:12]}"
    return PlannedQueryResponse(
        request_id=plan.request_fingerprint,
        result_type="preview",
        execution_path=plan.execution_path,
        artifact_id=artifact_id,
        plan=plan,
    )


def _query_point_sync(
    request: Request,
    dataset_id: str,
    variable: str,
    lon: float,
    lat: float,
    time_index: int,
    diagnostics: bool,
) -> SourcePointReadbackResponse:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return read_projected_point(
            connector=request.app.state.storage_connector,
            entry=entry,
            variable=variable,
            lon=lon,
            lat=lat,
            time_index=time_index,
            include_diagnostics=diagnostics,
        )

    registry = request.app.state.registry
    if dataset_id != registry.meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return read_synthetic_point(
        registry=registry,
        variable=variable,
        lon=lon,
        lat=lat,
        time_index=time_index,
        include_diagnostics=diagnostics,
    )


def _query_bbox_sync(
    request: Request,
    dataset_id: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    time_index: int,
    max_width: int,
    max_height: int,
    diagnostics: bool,
) -> SourceBBoxReadbackResponse:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return read_projected_bbox(
            connector=request.app.state.storage_connector,
            entry=entry,
            variable=variable,
            bbox=bbox,
            time_index=time_index,
            max_width=max_width,
            max_height=max_height,
            include_diagnostics=diagnostics,
        )

    registry = request.app.state.registry
    if dataset_id != registry.meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return read_synthetic_bbox(
        registry=registry,
        variable=variable,
        bbox=bbox,
        time_index=time_index,
        max_width=max_width,
        max_height=max_height,
        include_diagnostics=diagnostics,
    )


def _query_range_sync(
    request: Request,
    dataset_id: str,
    variable: str,
    time_index: int,
    bbox: tuple[float, float, float, float] | None,
    bins: int,
    max_width: int,
    max_height: int,
) -> RangeStatsResponse:
    if bbox is None:
        return _metadata_range_stats(request, dataset_id, variable, time_index)

    bbox_response = _query_bbox_sync(
        request,
        dataset_id,
        variable,
        bbox,
        time_index,
        max_width,
        max_height,
        False,
    )
    return _sampled_range_stats(
        dataset_id=dataset_id,
        variable=variable,
        time_index=time_index,
        bbox=list(bbox),
        values=bbox_response.values,
        bins=bins,
        unit=bbox_response.unit,
    )


def _metadata_range_stats(
    request: Request,
    dataset_id: str,
    variable: str,
    time_index: int,
) -> RangeStatsResponse:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        entry = ensure_catalog_entry_metadata_ready(entry, request.app.state.storage_connector)
        selected = next((item for item in entry.meta.variables if item.id == variable), None)
    else:
        registry = request.app.state.registry
        if dataset_id != registry.meta.id:
            raise HTTPException(status_code=404, detail="Dataset not found")
        selected = next((item for item in registry.meta.variables if item.id == variable), None)

    if selected is None:
        raise KeyError(variable)

    return RangeStatsResponse(
        dataset_id=dataset_id,
        variable=variable,
        time_index=time_index,
        stats_source="metadata",
        min=selected.stats.min,
        max=selected.stats.max,
        p02=selected.stats.p02,
        p98=selected.stats.p98,
        valid_count=0,
        unit=selected.unit,
        notes=["metadata stats only; provide bbox to sample active-view range and histogram"],
    )


def _sampled_range_stats(
    *,
    dataset_id: str,
    variable: str,
    time_index: int,
    bbox: list[float],
    values: list[list[int | float | bool | None]],
    bins: int,
    unit: str | None,
) -> RangeStatsResponse:
    finite_values = np.asarray(
        [float(value) for row in values for value in row if isinstance(value, int | float) and np.isfinite(float(value))],
        dtype=np.float64,
    )
    if finite_values.size == 0:
        return RangeStatsResponse(
            dataset_id=dataset_id,
            variable=variable,
            time_index=time_index,
            bbox=bbox,
            stats_source="sampled_bbox",
            valid_count=0,
            unit=unit,
            notes=["sampled bbox did not contain finite source values"],
        )

    p02, p98 = np.percentile(finite_values, [2, 98])
    counts, edges = np.histogram(finite_values, bins=bins)
    return RangeStatsResponse(
        dataset_id=dataset_id,
        variable=variable,
        time_index=time_index,
        bbox=bbox,
        stats_source="sampled_bbox",
        min=float(np.min(finite_values)),
        max=float(np.max(finite_values)),
        p02=float(p02),
        p98=float(p98),
        histogram_bins=[float(value) for value in edges.tolist()],
        histogram_counts=[int(value) for value in counts.tolist()],
        valid_count=int(finite_values.size),
        unit=unit,
    )


@router.post("/stats", response_model=PlannedQueryResponse)
async def create_stats(request: Request, payload: StatsRequest) -> PlannedQueryResponse:
    plan = request.app.state.planner.plan_stats(payload)
    artifact_id = f"art_{plan.request_fingerprint[:12]}"
    return PlannedQueryResponse(
        request_id=plan.request_fingerprint,
        result_type="stats",
        execution_path=plan.execution_path,
        artifact_id=artifact_id,
        plan=plan,
    )


@router.post("/clip", response_model=PlannedQueryResponse | ExportAcceptedResponse)
async def create_clip(request: Request, payload: ClipRequest):
    plan = request.app.state.planner.plan_clip(payload)
    if plan.execution_path == "batch":
        job = request.app.state.export_job_store.create_job(
            job_type="clip_handoff",
            request_fingerprint=plan.request_fingerprint,
            output_format=payload.output_format,
            plan_snapshot=plan,
        )
        body = ExportAcceptedResponse(
            job_id=job.job_id,
            status=job.status,
            plan=plan,
        )
        return JSONResponse(status_code=202, content=body.model_dump(mode="json"))

    artifact_id = f"art_{plan.request_fingerprint[:12]}"
    return PlannedQueryResponse(
        request_id=plan.request_fingerprint,
        result_type="small_clip",
        execution_path=plan.execution_path,
        artifact_id=artifact_id,
        plan=plan,
    )
