import hashlib
import json
from datetime import UTC
from datetime import datetime
from threading import Lock
from uuid import uuid4

from app.models.jobs import BrowseGenerationJobRecord
from app.services.job_records import JobRecordStore
from app.services.job_records import JobStoreUnavailable


_ACTIVE_STATUSES = {"queued", "running"}


class BrowseGenerationJobStore:
    def __init__(self, record_store: JobRecordStore | None = None) -> None:
        self._records = record_store or JobRecordStore()
        self._lock = Lock()

    def create_or_get_active_job(
        self,
        *,
        dataset_id: str,
        variables: list[str],
        time_indices: list[int],
        zoom_levels: list[int],
        overwrite: bool,
        retry_of_job_id: str | None = None,
    ) -> tuple[BrowseGenerationJobRecord, bool]:
        request_fingerprint = browse_generation_request_fingerprint(
            dataset_id=dataset_id,
            variables=variables,
            time_indices=time_indices,
            zoom_levels=zoom_levels,
            overwrite=overwrite,
        )
        with self._lock:
            existing = self._active_job_for_fingerprint_locked(dataset_id, request_fingerprint)
            if existing is not None:
                return existing, False
            job = self._build_job_locked(
                dataset_id=dataset_id,
                variables=variables,
                time_indices=time_indices,
                zoom_levels=zoom_levels,
                overwrite=overwrite,
                retry_of_job_id=retry_of_job_id,
                request_fingerprint=request_fingerprint,
            )
            claimed_job = self._claim_and_store_active_job_locked(job)
            return claimed_job, claimed_job.job_id == job.job_id

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
        request_fingerprint = browse_generation_request_fingerprint(
            dataset_id=dataset_id,
            variables=variables,
            time_indices=time_indices,
            zoom_levels=zoom_levels,
            overwrite=overwrite,
        )
        with self._lock:
            job = self._build_job_locked(
                dataset_id=dataset_id,
                variables=variables,
                time_indices=time_indices,
                zoom_levels=zoom_levels,
                overwrite=overwrite,
                retry_of_job_id=retry_of_job_id,
                request_fingerprint=request_fingerprint,
            )
            self._records.set_model(job)
            self._records.claim_active_browse_job(
                dataset_id=dataset_id,
                request_fingerprint=request_fingerprint,
                job_id=job.job_id,
            )
            return job

    def _build_job_locked(
        self,
        *,
        dataset_id: str,
        variables: list[str],
        time_indices: list[int],
        zoom_levels: list[int],
        overwrite: bool,
        retry_of_job_id: str | None,
        request_fingerprint: str,
    ) -> BrowseGenerationJobRecord:
        previous = self._get_job_unlocked(retry_of_job_id) if retry_of_job_id is not None else None
        return BrowseGenerationJobRecord(
            job_id=f"browse_{uuid4().hex[:12]}",
            dataset_id=dataset_id,
            request_fingerprint=request_fingerprint,
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

    def _claim_and_store_active_job_locked(
        self,
        job: BrowseGenerationJobRecord,
    ) -> BrowseGenerationJobRecord:
        claimed_job_id = self._records.claim_active_browse_job(
            dataset_id=job.dataset_id,
            request_fingerprint=job.request_fingerprint,
            job_id=job.job_id,
        )
        if claimed_job_id != job.job_id:
            existing = self._get_active_claimed_job_locked(job, claimed_job_id)
            if existing is not None:
                return existing
            self._records.clear_active_browse_job(
                dataset_id=job.dataset_id,
                request_fingerprint=job.request_fingerprint,
                job_id=claimed_job_id or "",
            )
            claimed_job_id = self._records.claim_active_browse_job(
                dataset_id=job.dataset_id,
                request_fingerprint=job.request_fingerprint,
                job_id=job.job_id,
            )
            if claimed_job_id != job.job_id:
                existing = self._get_active_claimed_job_locked(job, claimed_job_id)
                if existing is not None:
                    return existing
                raise JobStoreUnavailable("Unable to claim browse generation job slot")
        try:
            self._records.set_model(job)
        except Exception:
            self._records.clear_active_browse_job(
                dataset_id=job.dataset_id,
                request_fingerprint=job.request_fingerprint,
                job_id=job.job_id,
            )
            raise
        return job

    def _get_active_claimed_job_locked(
        self,
        job: BrowseGenerationJobRecord,
        claimed_job_id: str | None,
    ) -> BrowseGenerationJobRecord | None:
        if claimed_job_id is None:
            return None
        existing = self._get_job_unlocked(claimed_job_id)
        if existing is not None and existing.status in _ACTIVE_STATUSES:
            return existing
        self._records.clear_active_browse_job(
            dataset_id=job.dataset_id,
            request_fingerprint=job.request_fingerprint,
            job_id=claimed_job_id,
        )
        return None

    def _active_job_for_fingerprint_locked(
        self,
        dataset_id: str,
        request_fingerprint: str,
    ) -> BrowseGenerationJobRecord | None:
        job_id = self._records.get_active_browse_job_id(
            dataset_id=dataset_id,
            request_fingerprint=request_fingerprint,
        )
        job = self._get_job_unlocked(job_id)
        if job is not None and job.status in _ACTIVE_STATUSES:
            return job
        if job_id is not None:
            self._records.clear_active_browse_job(
                dataset_id=dataset_id,
                request_fingerprint=request_fingerprint,
                job_id=job_id,
            )
        return None

    def get_job(self, job_id: str | None) -> BrowseGenerationJobRecord | None:
        if job_id is None:
            return None
        with self._lock:
            return self._get_job_unlocked(job_id)

    def _get_job_unlocked(self, job_id: str | None) -> BrowseGenerationJobRecord | None:
        if job_id is None:
            return None
        return self._records.get_model(job_id, BrowseGenerationJobRecord)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._require_job_unlocked(job_id)
            self._records.set_model(
                job.model_copy(update={"status": "running", "started_at": datetime.now(tz=UTC)})
            )

    def record_artifact(self, job_id: str, *, generated: bool) -> None:
        with self._lock:
            job = self._require_job_unlocked(job_id)
            completed_artifacts = min(job.completed_artifacts + 1, job.total_artifacts)
            self._records.set_model(
                job.model_copy(
                    update={
                        "completed_artifacts": completed_artifacts,
                        "generated_artifacts": job.generated_artifacts + (1 if generated else 0),
                        "reused_artifacts": job.reused_artifacts + (0 if generated else 1),
                    }
                )
            )

    def mark_succeeded(self, job_id: str, *, manifest_path: str | None, generated: int, reused: int) -> None:
        with self._lock:
            job = self._require_job_unlocked(job_id)
            self._records.set_model(
                job.model_copy(
                    update={
                        "status": "succeeded",
                        "finished_at": datetime.now(tz=UTC),
                        "manifest_path": manifest_path,
                        "generated_artifacts": generated,
                        "reused_artifacts": reused,
                        "completed_artifacts": job.total_artifacts,
                        "error_message": None,
                    }
                )
            )
            self._records.clear_active_browse_job(
                dataset_id=job.dataset_id,
                request_fingerprint=job.request_fingerprint,
                job_id=job.job_id,
            )

    def mark_failed(self, job_id: str, error_message: str) -> None:
        with self._lock:
            job = self._require_job_unlocked(job_id)
            self._records.set_model(
                job.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(tz=UTC),
                        "error_message": error_message,
                    }
                )
            )
            self._records.clear_active_browse_job(
                dataset_id=job.dataset_id,
                request_fingerprint=job.request_fingerprint,
                job_id=job.job_id,
            )

    def _require_job_unlocked(self, job_id: str) -> BrowseGenerationJobRecord:
        job = self._get_job_unlocked(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


def browse_generation_request_fingerprint(
    *,
    dataset_id: str,
    variables: list[str],
    time_indices: list[int],
    zoom_levels: list[int],
    overwrite: bool,
) -> str:
    payload = {
        "dataset_id": dataset_id,
        "variables": sorted(set(variables)),
        "time_indices": sorted(set(time_indices)),
        "zoom_levels": sorted(set(zoom_levels)),
        "overwrite": overwrite,
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
