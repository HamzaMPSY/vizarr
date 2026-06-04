import hashlib
import json
import logging
import time
from typing import TypeVar

import redis
from pydantic import BaseModel
from pydantic import ValidationError
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)
JOB_RECORD_SCHEMA_VERSION = 1
JobModelT = TypeVar("JobModelT", bound=BaseModel)


class JobStoreUnavailable(RuntimeError):
    """Raised when a durable job write/read cannot be completed clearly."""


class JobRecordStore:
    def __init__(self, *, redis_client: redis.Redis | None = None, ttl: int = 86_400) -> None:
        self._redis = redis_client
        self._ttl = max(int(ttl), 1)
        self._memory: dict[str, tuple[float, str]] = {}

    @property
    def durable(self) -> bool:
        return self._redis is not None

    @property
    def ttl(self) -> int:
        return self._ttl

    @property
    def mode(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def set_model(self, record: BaseModel) -> None:
        job_id = getattr(record, "job_id")
        key = job_record_key(str(job_id))
        payload = serialize_job_record(record)
        if self._redis is not None:
            try:
                self._redis.setex(key, self._ttl, payload)
            except RedisError as exc:
                raise JobStoreUnavailable(
                    "Redis job store unavailable while writing job status; job was not acknowledged"
                ) from exc
        self._set_memory(key, payload)

    def get_model(self, job_id: str, model_type: type[JobModelT]) -> JobModelT | None:
        key = job_record_key(job_id)
        payload = self._get_raw(key)
        if payload is None:
            return None
        record = deserialize_job_record(payload, model_type)
        if record is None:
            logger.warning("Ignoring malformed job record for job_id=%s", job_id)
            self._delete_key(key)
            return None
        return record

    def claim_active_browse_job(self, *, dataset_id: str, request_fingerprint: str, job_id: str) -> str | None:
        key = browse_active_job_key(dataset_id, request_fingerprint)
        if self._redis is not None:
            try:
                claimed = self._redis.set(key, job_id, ex=self._ttl, nx=True)
                if claimed:
                    self._set_memory(key, job_id)
                    return job_id
                return _as_text(self._redis.get(key))
            except RedisError as exc:
                raise JobStoreUnavailable("Redis job store unavailable while claiming browse generation job") from exc

        existing = self._get_memory(key)
        if existing is not None:
            return existing
        self._set_memory(key, job_id)
        return job_id

    def get_active_browse_job_id(self, *, dataset_id: str, request_fingerprint: str) -> str | None:
        return self._get_raw(browse_active_job_key(dataset_id, request_fingerprint))

    def clear_active_browse_job(self, *, dataset_id: str, request_fingerprint: str, job_id: str) -> None:
        key = browse_active_job_key(dataset_id, request_fingerprint)
        current = self._get_raw(key)
        if current == job_id:
            self._delete_key(key)

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()
        self._memory.clear()

    def _get_raw(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                value = self._redis.get(key)
            except RedisError as exc:
                fallback = self._get_memory(key)
                if fallback is not None:
                    return fallback
                raise JobStoreUnavailable("Redis job store unavailable while reading job status") from exc
            if value is not None:
                return _as_text(value)
        return self._get_memory(key)

    def _delete_key(self, key: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(key)
            except RedisError:
                logger.warning("Failed to delete malformed job key from Redis", exc_info=True)
        self._memory.pop(key, None)

    def _set_memory(self, key: str, value: str) -> None:
        self._evict_expired_memory()
        self._memory[key] = (time.monotonic() + self._ttl, value)

    def _get_memory(self, key: str) -> str | None:
        self._evict_expired_memory()
        entry = self._memory.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._memory.pop(key, None)
            return None
        return value

    def _evict_expired_memory(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _value) in self._memory.items() if expires_at <= now]
        for key in expired:
            self._memory.pop(key, None)


def connect_job_record_store(redis_url: str, ttl: int) -> JobRecordStore:
    try:
        client = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        logger.info("Redis job store connected at %s", redis_url)
        return JobRecordStore(redis_client=client, ttl=ttl)
    except Exception as exc:
        logger.warning("Redis job store unavailable, using in-memory job status: %s", exc)
        return JobRecordStore(ttl=ttl)


def serialize_job_record(record: BaseModel) -> str:
    payload = {
        "schema_version": JOB_RECORD_SCHEMA_VERSION,
        "record_type": str(getattr(record, "job_type", "unknown")),
        "record": json.loads(record.model_dump_json()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def deserialize_job_record(payload: str, model_type: type[JobModelT]) -> JobModelT | None:
    try:
        parsed = json.loads(payload)
        if parsed.get("schema_version") != JOB_RECORD_SCHEMA_VERSION:
            return None
        record = parsed.get("record")
        if not isinstance(record, dict):
            return None
        return model_type.model_validate(record)
    except (json.JSONDecodeError, TypeError, ValidationError):
        return None


def job_record_key(job_id: str) -> str:
    return f"job:{job_id}"


def browse_active_job_key(dataset_id: str, request_fingerprint: str) -> str:
    dataset_digest = hashlib.sha1(dataset_id.encode("utf-8")).hexdigest()[:20]
    return f"job-active:browse:{dataset_digest}:{request_fingerprint}"


def _as_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
