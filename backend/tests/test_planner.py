from datetime import date

from app.config import Settings
from app.index.planner_index import CubeIndexRecord
from app.index.planner_index import PlannerIndex
from app.models.dataset import DatasetBounds
from app.models.requests import ClipRequest
from app.models.requests import PreviewRequest
from app.services.planner import PlannerService
from app.services.planner import build_request_fingerprint


def test_build_request_fingerprint_is_order_independent() -> None:
    first = {"collection_id": "sentinel2", "style": "ndvi-default", "z": 10, "x": 12, "y": 20}
    second = {"y": 20, "x": 12, "z": 10, "style": "ndvi-default", "collection_id": "sentinel2"}

    assert build_request_fingerprint(first) == build_request_fingerprint(second)


def test_preview_plan_prefers_browse_for_known_styles() -> None:
    planner = PlannerService(
        Settings(),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse:ndvi-default",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse/ndvi-default",
                    bands=("B04", "B08"),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                    style="ndvi-default",
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04", "B08"),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
            ]
        ),
    )
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
    assert plan.selected_cube == "sentinel2:browse:ndvi-default"


def test_tile_plan_prefers_browse_below_zoom_threshold() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=6,
        x=32,
        y=30,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.request_class == "tile"
    assert plan.chosen_representation == "browse"
    assert plan.selected_cube == "sentinel2:browse"


def test_tile_plan_prefers_browse_over_pyramid_when_available() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:pyramid",
                    collection_id="sentinel2",
                    representation="pyramid",
                    path="oci://bucket/collections/sentinel2/multiscale.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                    native_resolution_m=10.0,
                    population_strategy="prepopulated_then_lazy",
                    prepopulated_zoom_max=12,
                    multiscale_max_zoom=14,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=6,
        x=32,
        y=30,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.request_class == "tile"
    assert plan.chosen_representation == "browse"
    assert plan.selected_cube == "sentinel2:browse"
    assert plan.response_cache_key.startswith("artifact:tile:browse:")


def test_tile_plan_prefers_serving_over_pyramid_above_browse_zoom() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:pyramid",
                    collection_id="sentinel2",
                    representation="pyramid",
                    path="oci://bucket/collections/sentinel2/multiscale.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                    population_strategy="prepopulated_then_lazy",
                    prepopulated_zoom_max=12,
                    multiscale_max_zoom=14,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
            ]
        ),
    )

    z14_plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=14,
        x=8192,
        y=8192,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )
    z15_plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=15,
        x=16384,
        y=16384,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert z14_plan.chosen_representation == "serving"
    assert z15_plan.chosen_representation == "serving"


def test_tile_plan_prefers_prepopulated_pyramid_above_browse_zoom_within_prepopulated_ceiling() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:pyramid",
                    collection_id="sentinel2",
                    representation="pyramid",
                    path="oci://bucket/collections/sentinel2/multiscale.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                    population_strategy="prepopulated_then_lazy",
                    prepopulated_zoom_max=12,
                    multiscale_max_zoom=14,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=10,
        x=512,
        y=512,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.chosen_representation == "pyramid"
    assert plan.selected_cube == "sentinel2:pyramid"


def test_tile_plan_uses_pyramid_only_as_fallback_when_no_browse_or_serving_exists() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:pyramid",
                    collection_id="sentinel2",
                    representation="pyramid",
                    path="oci://bucket/collections/sentinel2/multiscale.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=30.39, south=-2.09, east=30.81, north=-1.05),
                    native_resolution_m=1.1,
                    population_strategy="prepopulated_then_lazy",
                    prepopulated_zoom_max=12,
                    multiscale_max_zoom=17,
                ),
            ]
        ),
    )

    z17_plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=17,
        x=76677,
        y=66082,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )
    assert z17_plan.chosen_representation == "pyramid"
    assert z17_plan.selected_cube == "sentinel2:pyramid"


def test_tile_plan_does_not_prefer_fully_lazy_pyramid_above_browse_zoom() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:pyramid",
                    collection_id="sentinel2",
                    representation="pyramid",
                    path="oci://bucket/collections/sentinel2/multiscale.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                    population_strategy="lazy_on_demand",
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=10,
        x=512,
        y=512,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.chosen_representation == "serving"


def test_tile_plan_prefers_serving_above_zoom_threshold() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=12,
        x=2048,
        y=2048,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.request_class == "tile"
    assert plan.chosen_representation == "serving"
    assert plan.selected_cube == "sentinel2:serving"


def test_tile_plan_does_not_reuse_browse_above_browse_max_zoom() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=1.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=1.0,
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=10,
        x=512,
        y=512,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.request_class == "tile"
    assert plan.chosen_representation == "serving"
    assert plan.selected_cube == "sentinel2:serving"


def test_tile_plan_uses_native_resolution_to_keep_browse_for_still_coarse_view() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=10.0,
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=8,
        x=128,
        y=128,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.chosen_representation == "browse"
    assert "61.1x coarser" in plan.notes[1]


def test_tile_plan_uses_native_resolution_to_choose_serving_for_coarse_dataset() -> None:
    planner = PlannerService(
        Settings(browse_tile_max_zoom=8),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:browse",
                    collection_id="sentinel2",
                    representation="browse",
                    path="oci://bucket/collections/sentinel2/browse",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=500.0,
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B04",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
                    native_resolution_m=500.0,
                ),
            ]
        ),
    )

    plan = planner.plan_tile_request(
        collection_id="sentinel2",
        style="viridis",
        z=7,
        x=64,
        y=64,
        time_index=0,
        params={"variable": "B04", "band_count": 1},
    )

    assert plan.chosen_representation == "serving"
    assert "2.4x coarser" in plan.notes[1]


def test_clip_plan_reroutes_oversized_requests_to_batch() -> None:
    planner = PlannerService(
        Settings(),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:source",
                    collection_id="sentinel2",
                    representation="source",
                    path="oci://bucket/collections/sentinel2/source/cube.zarr",
                    bands=("B02", "B03", "B04", "B08", "B11"),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
                CubeIndexRecord(
                    cube_id="sentinel2:serving",
                    collection_id="sentinel2",
                    representation="serving",
                    path="oci://bucket/collections/sentinel2/serving/cube.zarr",
                    bands=("B02", "B03", "B04", "B08", "B11"),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                ),
            ]
        ),
    )
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
    assert plan.selected_cube == "sentinel2:source"


def test_stats_plan_falls_back_to_source_when_serving_is_missing() -> None:
    planner = PlannerService(
        Settings(),
        PlannerIndex(
            [
                CubeIndexRecord(
                    cube_id="sentinel2:source",
                    collection_id="sentinel2",
                    representation="source",
                    path="oci://bucket/collections/sentinel2/source/cube.zarr",
                    bands=("B08",),
                    version="v1",
                    bbox_wgs84=DatasetBounds(west=-10.0, south=-10.0, east=10.0, north=10.0),
                    time_start=date(2026, 1, 1),
                    time_end=date(2026, 1, 31),
                )
            ]
        ),
    )

    from app.models.requests import StatsRequest

    plan = planner.plan_stats(
        StatsRequest(
            collection_id="sentinel2",
            aoi_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
            bands=["B08"],
            metrics=["mean", "p95"],
        )
    )

    assert plan.request_class == "stats"
    assert plan.chosen_representation == "source"
    assert plan.selected_cube == "sentinel2:source"
