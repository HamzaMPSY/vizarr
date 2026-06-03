from datetime import UTC
from datetime import datetime
from threading import Lock
from uuid import uuid4

from app.models.jobs import BrowseGenerationJobRecord


class BrowseGenerationJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, BrowseGenerationJobRecord] = {}
        self._lock = Lock()

    def create_job(
        self,
        *,
        dataset_id: str,
        variables: list[str],
        time_indices: list[int],
        zoom_levels: list[int],
        overwrite: bool,
        retry_of_job_id: str | None = None,
    ) -> BrowseGenerationJobRecord:
        previous = self.get_job(retry_of_job_id) if retry_of_job_id is not None else None
        job = BrowseGenerationJobRecord(
            job_id=f"browse_{uuid4().hex[:12]}",
            dataset_id=dataset_id,
            status="queued",
            created_at=datetime.now(tz=UTC),
            variables=variables,
            time_indices=time_indices,
            zoom_levels=zoom_levels,
            overwrite=overwrite,
            total_artifacts=len(variables) * len(time_indices) * len(zoom_levels),
            retry_of_job_id=retry_of_job_id,
            attempt=(previous.attempt + 1) if previous is not None else 1,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str | None) -> BrowseGenerationJobRecord | None:
        if job_id is None:
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = datetime.now(tz=UTC)

    def record_artifact(self, job_id: str, *, generated: bool) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.completed_artifacts = min(job.completed_artifacts + 1, job.total_artifacts)
            if generated:
                job.generated_artifacts += 1
            else:
                job.reused_artifacts += 1

    def mark_succeeded(self, job_id: str, *, manifest_path: str | None, generated: int, reused: int) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "succeeded"
            job.finished_at = datetime.now(tz=UTC)
            job.manifest_path = manifest_path
            job.generated_artifacts = generated
            job.reused_artifacts = reused
            job.completed_artifacts = job.total_artifacts
            job.error_message = None

    def mark_failed(self, job_id: str, error_message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "failed"
            job.finished_at = datetime.now(tz=UTC)
            job.error_message = error_message
