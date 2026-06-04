from typing import Literal

from pydantic import BaseModel, Field

from app.models.jobs import JobStatus
from app.models.plans import ExecutionPath, QueryPlan


class PlannedQueryResponse(BaseModel):
    request_id: str
    result_type: Literal["preview", "stats", "small_clip"]
    execution_path: ExecutionPath
    cache_hit: bool = False
    artifact_id: str | None = None
    plan: QueryPlan


class SourceReadbackDiagnostics(BaseModel):
    storage_backend: str
    source_path: str | None = None
    array_name: str | None = None
    dtype: str | None = None
    source_crs: str | None = None
    source_window: dict[str, int | None] | None = None
    chunk_shape: list[int] | None = None
    object_get_count: int = 0
    byte_range_get_count: int = 0
    object_bytes_read: int = 0
    zarr_chunk_count: int = 0
    zarr_shard_index_reads: int = 0
    notes: list[str] = Field(default_factory=list)


class SourcePointReadbackResponse(BaseModel):
    result_type: Literal["source_point"] = "source_point"
    dataset_id: str
    variable: str
    time_index: int
    lon: float
    lat: float
    value: int | float | bool | None
    unit: str | None = None
    is_nodata: bool
    pixel_x: int | None = None
    pixel_y: int | None = None
    diagnostics: SourceReadbackDiagnostics | None = None


class SourceBBoxReadbackResponse(BaseModel):
    result_type: Literal["source_bbox"] = "source_bbox"
    dataset_id: str
    variable: str
    time_index: int
    bbox: list[float]
    shape: list[int]
    values: list[list[int | float | bool | None]]
    unit: str | None = None
    valid_count: int
    diagnostics: SourceReadbackDiagnostics | None = None


class RangeStatsResponse(BaseModel):
    result_type: Literal["range_stats"] = "range_stats"
    dataset_id: str
    variable: str
    time_index: int
    bbox: list[float] | None = None
    stats_source: Literal["metadata", "sampled_bbox"]
    min: float | None = None
    max: float | None = None
    p02: float | None = None
    p98: float | None = None
    histogram_bins: list[float] = Field(default_factory=list)
    histogram_counts: list[int] = Field(default_factory=list)
    valid_count: int = 0
    unit: str | None = None
    notes: list[str] = Field(default_factory=list)


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


class BrowseGenerationAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus
    result_type: Literal["browse_generation"] = "browse_generation"
    dataset_id: str
    progress: float
    total_artifacts: int
    completed_artifacts: int
    can_retry: bool = False


class BrowseGenerationStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    job_type: str
    dataset_id: str
    progress: float
    total_artifacts: int
    completed_artifacts: int
    generated_artifacts: int
    reused_artifacts: int
    variables: list[str]
    time_indices: list[int]
    zoom_levels: list[int]
    manifest_path: str | None = None
    error_message: str | None = None
    attempt: int
    retry_of_job_id: str | None = None
    can_retry: bool
