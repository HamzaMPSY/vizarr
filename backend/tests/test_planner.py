from datetime import date

from app.config import Settings
from app.models.requests import ClipRequest
from app.models.requests import PreviewRequest
from app.services.planner import PlannerService
from app.services.planner import build_request_fingerprint


def test_build_request_fingerprint_is_order_independent() -> None:
    first = {"collection_id": "sentinel2", "style": "ndvi-default", "z": 10, "x": 12, "y": 20}
    second = {"y": 20, "x": 12, "z": 10, "style": "ndvi-default", "collection_id": "sentinel2"}

    assert build_request_fingerprint(first) == build_request_fingerprint(second)


def test_preview_plan_prefers_browse_for_known_styles() -> None:
    planner = PlannerService(Settings())
    plan = planner.plan_preview(
        PreviewRequest(
            collection_id="sentinel2",
            aoi_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            bands=["B08", "B04"],
            style="ndvi-default",
            max_size=1024,
        )
    )

    assert plan.request_class == "preview"
    assert plan.execution_path == "interactive"
    assert plan.chosen_representation == "browse"


def test_clip_plan_reroutes_oversized_requests_to_batch() -> None:
    planner = PlannerService(Settings())
    plan = planner.plan_clip(
        ClipRequest(
            collection_id="sentinel2",
            aoi_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            bands=["B02", "B03", "B04", "B08", "B11"],
            output_format="zarr",
        )
    )

    assert plan.request_class == "export"
    assert plan.execution_path == "batch"
    assert plan.chosen_representation == "source"
    assert set(plan.thresholds_exceeded) == {"band_count", "date_span"}
