from typing import Literal

from pydantic import BaseModel, Field


RequestClass = Literal["tile", "preview", "stats", "small_clip", "export"]
ExecutionPath = Literal["interactive", "batch"]
Representation = Literal["browse", "serving", "source"]


class QueryPlan(BaseModel):
    planner_version: str
    request_class: RequestClass
    chosen_representation: Representation
    execution_path: ExecutionPath
    request_fingerprint: str
    response_cache_key: str
    plan_cache_key: str
    candidate_cubes: list[str] = Field(default_factory=list)
    expected_chunk_count: int = 0
    thresholds_exceeded: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
