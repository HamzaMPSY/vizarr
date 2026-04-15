import hashlib
import json
from datetime import date
from math import ceil

from app.config import Settings
from app.models.plans import QueryPlan
from app.models.requests import ClipRequest
from app.models.requests import ExportRequest
from app.models.requests import PreviewRequest
from app.models.requests import StatsRequest


class PlannerService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browse_styles = {
            item.strip()
            for item in settings.browse_enabled_styles.split(",")
            if item.strip()
        }

    def plan_tile_request(
        self,
        *,
        collection_id: str,
        style: str,
        z: int,
        x: int,
        y: int,
        time_index: int,
        params: dict[str, object],
    ) -> QueryPlan:
        payload = {
            "collection_id": collection_id,
            "style": style,
            "z": z,
            "x": x,
            "y": y,
            "time_index": time_index,
            "params": params,
        }
        return self._build_plan(
            request_class="tile",
            representation=self._choose_representation(style=style, default="serving"),
            execution_path="interactive",
            payload=payload,
            expected_chunk_count=max(1, int(params.get("band_count", 1))),
            notes=["Tile requests stay on the interactive plane."],
        )

    def plan_preview(self, request: PreviewRequest) -> QueryPlan:
        factor = max(1, ceil(request.max_size / 512))
        return self._build_plan(
            request_class="preview",
            representation=self._choose_representation(style=request.style, default="serving"),
            execution_path="interactive",
            payload=request.model_dump(mode="json", exclude_none=True),
            expected_chunk_count=max(1, len(request.bands) * factor * factor),
            notes=["Preview requests are capped by max_size and stay interactive."],
        )

    def plan_stats(self, request: StatsRequest) -> QueryPlan:
        return self._build_plan(
            request_class="stats",
            representation="serving",
            execution_path="interactive",
            payload=request.model_dump(mode="json", exclude_none=True),
            expected_chunk_count=max(1, len(request.bands) * len(request.metrics)),
            notes=["Stats requests prefer serving cubes for repeated analytical reads."],
        )

    def plan_clip(self, request: ClipRequest) -> QueryPlan:
        thresholds_exceeded: list[str] = []
        if len(request.bands) > self._settings.interactive_max_clip_bands:
            thresholds_exceeded.append("band_count")

        span_days = _date_span_days(request.start, request.end)
        if span_days > self._settings.interactive_max_clip_days:
            thresholds_exceeded.append("date_span")

        execution_path = "batch" if thresholds_exceeded else "interactive"
        request_class = "export" if execution_path == "batch" else "small_clip"
        representation = "source" if execution_path == "batch" else "serving"
        notes = [
            "Clip requests stay interactive only when they remain under strict thresholds."
        ]
        if execution_path == "batch":
            notes.append("Oversized clip rerouted to batch export path.")

        return self._build_plan(
            request_class=request_class,
            representation=representation,
            execution_path=execution_path,
            payload=request.model_dump(mode="json", exclude_none=True),
            expected_chunk_count=max(1, len(request.bands) * max(span_days, 1)),
            thresholds_exceeded=thresholds_exceeded,
            notes=notes,
        )

    def plan_export(self, request: ExportRequest) -> QueryPlan:
        return self._build_plan(
            request_class="export",
            representation="source",
            execution_path="batch",
            payload=request.model_dump(mode="json", exclude_none=True),
            expected_chunk_count=max(1, len(request.bands) * max(_date_span_days(request.start, request.end), 1)),
            notes=["Exports are durable batch jobs and read canonical or full-fidelity representations."],
        )

    def _choose_representation(self, *, style: str, default: str) -> str:
        if style in self._browse_styles:
            return "browse"
        return default

    def _build_plan(
        self,
        *,
        request_class: str,
        representation: str,
        execution_path: str,
        payload: dict[str, object],
        expected_chunk_count: int,
        thresholds_exceeded: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> QueryPlan:
        fingerprint = build_request_fingerprint(payload)
        return QueryPlan(
            planner_version=self._settings.planner_version,
            request_class=request_class,
            chosen_representation=representation,
            execution_path=execution_path,
            request_fingerprint=fingerprint,
            response_cache_key=f"artifact:{request_class}:{representation}:{fingerprint}",
            plan_cache_key=f"plan:{fingerprint}",
            candidate_cubes=[str(payload.get("collection_id", "unknown"))],
            expected_chunk_count=expected_chunk_count,
            thresholds_exceeded=thresholds_exceeded or [],
            notes=notes or [],
        )


def build_request_fingerprint(payload: dict[str, object]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _date_span_days(start: date | None, end: date | None) -> int:
    if start is None or end is None:
        return 1
    return max((end - start).days + 1, 1)
