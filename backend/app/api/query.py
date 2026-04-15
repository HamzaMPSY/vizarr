from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.artifacts import ExportAcceptedResponse
from app.models.artifacts import PlannedQueryResponse
from app.models.requests import ClipRequest
from app.models.requests import PreviewRequest
from app.models.requests import StatsRequest


router = APIRouter(prefix="/query", tags=["query"])


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
