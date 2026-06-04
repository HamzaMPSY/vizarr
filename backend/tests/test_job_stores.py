import json

import pytest
from redis.exceptions import RedisError

from app.models.jobs import ExportJobRecord
from app.models.plans import QueryPlan
from app.services.browse_jobs import BrowseGenerationJobStore
from app.services.export_jobs import ExportJobStore
from app.services.job_records import JobRecordStore
from app.services.job_records import JobStoreUnavailable
from app.services.job_records import browse_active_job_key
from app.services.job_records import job_record_key


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.closed = False

    def ping(self) -> bool:
        return True

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.data:
                deleted += 1
            self.data.pop(key, None)
            self.ttls.pop(key, None)
        return deleted

    def close(self) -> None:
        self.closed = True


class _FailingRedis(_FakeRedis):
    def setex(self, key: str, ttl: int, value: str) -> bool:
        raise RedisError("write failed")


def test_export_job_store_reads_redis_record_after_app_state_reset() -> None:
    redis = _FakeRedis()
    record_store = JobRecordStore(redis_client=redis, ttl=123)
    export_store = ExportJobStore(record_store)

    job = export_store.create_job(
        job_type="export",
        request_fingerprint="fingerprint-1",
        output_format="zarr",
        plan_snapshot=_plan(),
    )
    reset_store = ExportJobStore(JobRecordStore(redis_client=redis, ttl=123))

    reloaded = reset_store.get_job(job.job_id)

    assert reloaded == job
    assert redis.ttls[job_record_key(job.job_id)] == 123


def test_browse_generation_store_uses_redis_active_index_and_ttl() -> None:
    redis = _FakeRedis()
    store = BrowseGenerationJobStore(JobRecordStore(redis_client=redis, ttl=456))

    first, first_created = store.create_or_get_active_job(
        dataset_id="dataset-1",
        variables=["B4"],
        time_indices=[0],
        zoom_levels=[0, 1],
        overwrite=False,
    )
    duplicate, duplicate_created = store.create_or_get_active_job(
        dataset_id="dataset-1",
        variables=["B4"],
        time_indices=[0],
        zoom_levels=[1, 0],
        overwrite=False,
    )
    reset_store = BrowseGenerationJobStore(JobRecordStore(redis_client=redis, ttl=456))
    reloaded = reset_store.get_job(first.job_id)
    active_key = browse_active_job_key(first.dataset_id, first.request_fingerprint)

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.job_id == first.job_id
    assert reloaded == first
    assert redis.ttls[job_record_key(first.job_id)] == 456
    assert redis.ttls[active_key] == 456


def test_job_store_falls_back_to_memory_without_redis() -> None:
    export_store = ExportJobStore(JobRecordStore(ttl=60))

    job = export_store.create_job(
        job_type="clip_handoff",
        request_fingerprint="fingerprint-2",
        output_format="geotiff",
        plan_snapshot=_plan(),
    )

    assert export_store.get_job(job.job_id) == job


def test_malformed_redis_payload_is_ignored_and_removed() -> None:
    redis = _FakeRedis()
    key = job_record_key("job_bad")
    redis.data[key] = json.dumps({"schema_version": 1, "record": {"job_id": "job_bad"}})
    record_store = JobRecordStore(redis_client=redis, ttl=60)

    assert record_store.get_model("job_bad", ExportJobRecord) is None
    assert key not in redis.data


def test_redis_write_failure_is_not_silently_acknowledged() -> None:
    export_store = ExportJobStore(JobRecordStore(redis_client=_FailingRedis(), ttl=60))

    with pytest.raises(JobStoreUnavailable):
        export_store.create_job(
            job_type="export",
            request_fingerprint="fingerprint-3",
            output_format="zarr",
            plan_snapshot=_plan(),
        )


def _plan() -> QueryPlan:
    return QueryPlan(
        planner_version="v1",
        collection_id="demo-global",
        request_class="export",
        chosen_representation="source",
        execution_path="batch",
        request_fingerprint="fingerprint-1",
        response_cache_key="response",
        plan_cache_key="plan",
    )
