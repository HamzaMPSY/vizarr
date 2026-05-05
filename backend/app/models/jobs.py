from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.plans import QueryPlan


JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobType = Literal["export", "clip_handoff"]


class ExportJobRecord(BaseModel):
    job_id: str
    job_type: JobType
    status: JobStatus
    request_fingerprint: str
    output_format: str
    created_at: datetime
    plan_snapshot: QueryPlan
    output_path: str | None = None
    error_message: str | None = None
