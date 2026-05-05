from datetime import UTC, datetime
from uuid import uuid4

from app.models.jobs import ExportJobRecord
from app.models.jobs import JobType
from app.models.plans import QueryPlan


class ExportJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ExportJobRecord] = {}

    def create_job(
        self,
        *,
        job_type: JobType,
        request_fingerprint: str,
        output_format: str,
        plan_snapshot: QueryPlan,
    ) -> ExportJobRecord:
        job_id = f"job_{uuid4().hex[:12]}"
        job = ExportJobRecord(
            job_id=job_id,
            job_type=job_type,
            status="queued",
            request_fingerprint=request_fingerprint,
            output_format=output_format,
            created_at=datetime.now(tz=UTC),
            plan_snapshot=plan_snapshot,
        )
        self._jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> ExportJobRecord | None:
        return self._jobs.get(job_id)
