import hashlib
import json
from datetime import date
from math import ceil, cos, pi
import re

from app.config import Settings
from app.index.planner_index import CubeIndexRecord
from app.index.planner_index import PlannerIndex
from app.models.dataset import DatasetBounds
from app.models.plans import QueryPlan
from app.models.requests import ClipRequest
from app.models.requests import ExportRequest
from app.models.requests import PreviewRequest
from app.models.requests import StatsRequest
from app.core.tile_generator import tile_to_bbox


class PlannerService:
    def __init__(self, settings: Settings, planner_index: PlannerIndex) -> None:
        self._settings = settings
        self._index = planner_index

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
        bbox = tile_to_bbox(z, x, y)
        requested_resolution_m = _tile_resolution_meters_per_pixel(bbox, z)
        candidates = self._index.find_candidates(
            collection_id=collection_id,
            bands=[str(params.get("variable"))],
            bbox=bbox,
            style=style,
        )
        selected_candidates, representation = self._select_candidates(
            request_class="tile",
            style=style,
            candidates=candidates,
            z=z,
            requested_resolution_m=requested_resolution_m,
        )
        return self._build_plan(
            collection_id=collection_id,
            request_class="tile",
            representation=representation,
            execution_path="interactive",
            payload=payload,
            candidates=selected_candidates,
            expected_chunk_count=max(1, len(selected_candidates) * int(params.get("band_count", 1))),
            notes=_append_candidate_note(
                base=[
                    "Tile requests stay on the interactive plane.",
                    _tile_resolution_note(
                        requested_resolution_m=requested_resolution_m,
                        native_resolution_m=_native_resolution_meters(candidates),
                    ),
                ],
                candidates=selected_candidates,
            ),
        )

    def plan_preview(self, request: PreviewRequest) -> QueryPlan:
        factor = max(1, ceil(request.max_size / 512))
        bbox = bbox_from_wkt(request.aoi_wkt)
        candidates = self._index.find_candidates(
            collection_id=request.collection_id,
            bands=request.bands,
            bbox=bbox,
            start=request.start,
            end=request.end,
            style=request.style,
        )
        selected_candidates, representation = self._select_candidates(
            request_class="preview",
            style=request.style,
            candidates=candidates,
            z=None,
        )
        return self._build_plan(
            collection_id=request.collection_id,
            request_class="preview",
            representation=representation,
            execution_path="interactive",
            payload=request.model_dump(mode="json", exclude_none=True),
            candidates=selected_candidates,
            expected_chunk_count=max(1, max(len(selected_candidates), 1) * len(request.bands) * factor * factor),
            notes=_append_candidate_note(
                base=["Preview requests are capped by max_size and stay interactive."],
                candidates=selected_candidates,
            ),
        )

    def plan_stats(self, request: StatsRequest) -> QueryPlan:
        bbox = bbox_from_wkt(request.aoi_wkt)
        candidates = self._index.find_candidates(
            collection_id=request.collection_id,
            bands=request.bands,
            bbox=bbox,
            start=request.start,
            end=request.end,
        )
        selected_candidates, representation = self._select_candidates(
            request_class="stats",
            style=None,
            candidates=candidates,
            z=None,
        )
        return self._build_plan(
            collection_id=request.collection_id,
            request_class="stats",
            representation=representation,
            execution_path="interactive",
            payload=request.model_dump(mode="json", exclude_none=True),
            candidates=selected_candidates,
            expected_chunk_count=max(1, max(len(selected_candidates), 1) * len(request.bands) * len(request.metrics)),
            notes=_append_candidate_note(
                base=["Stats requests prefer serving cubes for repeated analytical reads."],
                candidates=selected_candidates,
            ),
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
        bbox = bbox_from_wkt(request.aoi_wkt)
        candidates = self._index.find_candidates(
            collection_id=request.collection_id,
            bands=request.bands,
            bbox=bbox,
            start=request.start,
            end=request.end,
        )
        selected_candidates, interactive_representation = self._select_candidates(
            request_class="small_clip",
            style=None,
            candidates=candidates,
            z=None,
        )
        representation = "source" if execution_path == "batch" else interactive_representation
        if execution_path == "batch":
            selected_candidates = [item for item in candidates if item.representation == "source"] or selected_candidates
        notes = [
            "Clip requests stay interactive only when they remain under strict thresholds."
        ]
        if execution_path == "batch":
            notes.append("Oversized clip rerouted to batch export path.")

        return self._build_plan(
            collection_id=request.collection_id,
            request_class=request_class,
            representation=representation,
            execution_path=execution_path,
            payload=request.model_dump(mode="json", exclude_none=True),
            candidates=selected_candidates,
            expected_chunk_count=max(1, max(len(selected_candidates), 1) * len(request.bands) * max(span_days, 1)),
            thresholds_exceeded=thresholds_exceeded,
            notes=_append_candidate_note(base=notes, candidates=selected_candidates),
        )

    def plan_export(self, request: ExportRequest) -> QueryPlan:
        bbox = bbox_from_wkt(request.aoi_wkt)
        candidates = self._index.find_candidates(
            collection_id=request.collection_id,
            bands=request.bands,
            bbox=bbox,
            start=request.start,
            end=request.end,
        )
        selected_candidates, _ = self._select_candidates(
            request_class="export",
            style=None,
            candidates=candidates,
            z=None,
        )
        return self._build_plan(
            collection_id=request.collection_id,
            request_class="export",
            representation="source",
            execution_path="batch",
            payload=request.model_dump(mode="json", exclude_none=True),
            candidates=selected_candidates,
            expected_chunk_count=max(
                1,
                max(len(selected_candidates), 1) * len(request.bands) * max(_date_span_days(request.start, request.end), 1),
            ),
            notes=_append_candidate_note(
                base=["Exports are durable batch jobs and read canonical or full-fidelity representations."],
                candidates=selected_candidates,
            ),
        )

    def _select_candidates(
        self,
        *,
        request_class: str,
        style: str | None,
        candidates: list[CubeIndexRecord],
        z: int | None,
        requested_resolution_m: float | None = None,
    ) -> tuple[list[CubeIndexRecord], str]:
        if request_class == "export":
            priorities = ("source",)
        elif request_class in {"tile", "preview"}:
            if request_class == "tile":
                priorities = self._tile_representation_priorities(
                    candidates=candidates,
                    z=z,
                    requested_resolution_m=requested_resolution_m,
                )
            else:
                priorities = ("browse", "serving", "source")
        else:
            priorities = ("serving", "source", "browse")

        for representation in priorities:
            subset = [item for item in candidates if item.representation == representation]
            if request_class == "preview" and representation == "browse" and style is not None:
                subset = [item for item in subset if item.style == style]
            if subset:
                return subset, representation

        fallback_representation = priorities[-1]
        return [], fallback_representation

    def _tile_representation_priorities(
        self,
        *,
        candidates: list[CubeIndexRecord],
        z: int | None,
        requested_resolution_m: float | None,
    ) -> tuple[str, ...]:
        pyramid_candidates = [item for item in candidates if item.representation == "pyramid"]
        non_pyramid_priorities = self._non_pyramid_tile_priorities(
            candidates=candidates,
            z=z,
            requested_resolution_m=requested_resolution_m,
        )
        if pyramid_candidates and self._should_prefer_pyramid_for_tile(pyramid_candidates, z):
            return _prepend_priority(non_pyramid_priorities, "pyramid")
        if pyramid_candidates:
            return _append_priority(non_pyramid_priorities, "pyramid")
        return non_pyramid_priorities

    def _should_prefer_pyramid_for_tile(
        self,
        candidates: list[CubeIndexRecord],
        z: int | None,
    ) -> bool:
        if z is None:
            return False
        if z <= self._settings.browse_tile_max_zoom:
            return False

        for candidate in candidates:
            multiscale_max_zoom = getattr(candidate, "multiscale_max_zoom", None)
            if multiscale_max_zoom is not None and z > multiscale_max_zoom:
                continue

            population_strategy = getattr(candidate, "population_strategy", None)
            if population_strategy not in {"prepopulated_then_lazy", "prepopulated", "eager"}:
                continue

            prepopulated_zoom_max = getattr(candidate, "prepopulated_zoom_max", None)
            if prepopulated_zoom_max is not None and z > prepopulated_zoom_max:
                continue

            return True
        return False

    def _non_pyramid_tile_priorities(
        self,
        *,
        candidates: list[CubeIndexRecord],
        z: int | None,
        requested_resolution_m: float | None,
    ) -> tuple[str, ...]:
        browse_candidates = [item for item in candidates if item.representation == "browse"]
        serving_candidates = [item for item in candidates if item.representation == "serving"]
        source_candidates = [item for item in candidates if item.representation == "source"]
        native_resolution_m = _native_resolution_meters(serving_candidates or source_candidates or candidates)

        # Browse overviews stop at browse_tile_max_zoom. Above that point we must
        # switch to serving/source so zooming can keep refining instead of
        # repeatedly sampling the same top browse level.
        if z is not None and z > self._settings.browse_tile_max_zoom:
            return ("serving", "source", "browse")

        if requested_resolution_m is not None and native_resolution_m is not None and browse_candidates:
            browse_threshold = native_resolution_m * self._settings.browse_tile_native_resolution_ratio
            serving_threshold = native_resolution_m * self._settings.serving_tile_native_resolution_ratio
            if requested_resolution_m >= browse_threshold:
                return ("browse", "serving", "source")
            if requested_resolution_m <= serving_threshold:
                return ("serving", "source", "browse")

        if z is None:
            return ("serving", "source", "browse")
        return ("browse", "serving", "source")

    def _build_plan(
        self,
        *,
        collection_id: str,
        request_class: str,
        representation: str,
        execution_path: str,
        payload: dict[str, object],
        candidates: list[CubeIndexRecord],
        expected_chunk_count: int,
        thresholds_exceeded: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> QueryPlan:
        fingerprint = build_request_fingerprint(payload)
        return QueryPlan(
            planner_version=self._settings.planner_version,
            collection_id=collection_id,
            request_class=request_class,
            chosen_representation=representation,
            execution_path=execution_path,
            request_fingerprint=fingerprint,
            response_cache_key=f"artifact:{request_class}:{representation}:{fingerprint}",
            plan_cache_key=f"plan:{fingerprint}",
            candidate_cubes=[item.cube_id for item in candidates],
            candidate_paths=[item.path for item in candidates],
            selected_cube=candidates[0].cube_id if candidates else None,
            selected_path=candidates[0].path if candidates else None,
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


def bbox_from_wkt(wkt: str) -> tuple[float, float, float, float] | None:
    numbers = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", wkt)
    if len(numbers) < 4 or len(numbers) % 2 != 0:
        return None

    xs = [float(value) for value in numbers[0::2]]
    ys = [float(value) for value in numbers[1::2]]
    return min(xs), min(ys), max(xs), max(ys)


def _append_candidate_note(
    *,
    base: list[str],
    candidates: list[CubeIndexRecord],
) -> list[str]:
    if candidates:
        return base + [f"Planner pruned to {len(candidates)} candidate cube(s) before execution."]
    return base + ["Planner found no matching indexed cube candidates."]


def _tile_resolution_meters_per_pixel(bbox: tuple[float, float, float, float], z: int) -> float:
    _west, south, _east, north = bbox
    center_lat = max(min((south + north) / 2.0, 85.05112878), -85.05112878)
    return 156543.03392804097 * cos(center_lat * pi / 180.0) / (2**z)


def _native_resolution_meters(candidates: list[CubeIndexRecord]) -> float | None:
    values = [item.native_resolution_m for item in candidates if item.native_resolution_m is not None and item.native_resolution_m > 0]
    if not values:
        return None
    return min(values)


def _tile_resolution_note(
    *,
    requested_resolution_m: float,
    native_resolution_m: float | None,
) -> str:
    if native_resolution_m is None:
        return f"Requested tile scale is about {requested_resolution_m:.1f} m/px; planner fell back to zoom policy."
    ratio = requested_resolution_m / native_resolution_m
    return (
        f"Requested tile scale is about {requested_resolution_m:.1f} m/px versus native {native_resolution_m:.1f} m/px "
        f"({ratio:.1f}x coarser)."
    )


def _append_priority(priorities: tuple[str, ...], representation: str) -> tuple[str, ...]:
    if representation in priorities:
        return priorities
    return priorities + (representation,)


def _prepend_priority(priorities: tuple[str, ...], representation: str) -> tuple[str, ...]:
    if representation in priorities:
        return (representation,) + tuple(item for item in priorities if item != representation)
    return (representation,) + priorities
