from typing import Literal

from pydantic import BaseModel

from app.models.jobs import JobStatus
from app.models.plans import ExecutionPath, QueryPlan


class PlannedQueryResponse(BaseModel):
    request_id: str
    result_type: Literal["preview", "stats", "small_clip"]
    execution_path: ExecutionPath
    cache_hit: bool = False
    artifact_id: str | None = None
    plan: QueryPlan


class ExportAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus
    result_type: Literal["export"] = "export"
    plan: QueryPlan


class ExportStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    job_type: str
    output_format: str
    request_fingerprint: str
    output_path: str | None = None
    error_message: str | None = None
