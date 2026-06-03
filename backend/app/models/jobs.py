from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.plans import QueryPlan


JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobType = Literal["export", "clip_handoff", "browse_generation"]


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


class BrowseGenerationJobRecord(BaseModel):
    job_id: str
    job_type: Literal["browse_generation"] = "browse_generation"
    dataset_id: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    variables: list[str]
    time_indices: list[int]
    zoom_levels: list[int]
    overwrite: bool = False
    total_artifacts: int = 0
    completed_artifacts: int = 0
    generated_artifacts: int = 0
    reused_artifacts: int = 0
    manifest_path: str | None = None
    error_message: str | None = None
    attempt: int = 1
    retry_of_job_id: str | None = None

    @property
    def progress(self) -> float:
        if self.total_artifacts <= 0:
            return 0.0
        return min(self.completed_artifacts / self.total_artifacts, 1.0)

    @property
    def can_retry(self) -> bool:
        return self.status == "failed"
