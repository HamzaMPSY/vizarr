from fastapi import APIRouter, HTTPException, Request

from app.models.artifacts import ExportAcceptedResponse
from app.models.artifacts import ExportStatusResponse
from app.models.requests import ExportRequest


router = APIRouter(prefix="/exports", tags=["exports"])


@router.post("", response_model=ExportAcceptedResponse)
async def create_export(request: Request, payload: ExportRequest) -> ExportAcceptedResponse:
    plan = request.app.state.planner.plan_export(payload)
    job = request.app.state.export_job_store.create_job(
        job_type="export",
        request_fingerprint=plan.request_fingerprint,
        output_format=payload.output_format,
        plan_snapshot=plan,
    )
    return ExportAcceptedResponse(
        job_id=job.job_id,
        status=job.status,
        plan=plan,
    )


@router.get("/{job_id}", response_model=ExportStatusResponse)
async def get_export(job_id: str, request: Request) -> ExportStatusResponse:
    job = request.app.state.export_job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return ExportStatusResponse(
        job_id=job.job_id,
        status=job.status,
        job_type=job.job_type,
        output_format=job.output_format,
        request_fingerprint=job.request_fingerprint,
        output_path=job.output_path,
        error_message=job.error_message,
    )
