from datetime import date

from app.index.planner_index import CubeIndexRecord
from app.index.planner_index import PlannerIndex
from app.models.dataset import DatasetBounds


def test_planner_index_prunes_by_collection_bands_bbox_and_time() -> None:
    index = PlannerIndex(
        [
            CubeIndexRecord(
                cube_id="cube-a:serving",
                collection_id="sentinel2",
                representation="serving",
                path="oci://bucket/collections/sentinel2/serving/cube-a.zarr",
                bands=("B04", "B08"),
                version="v1",
                bbox_wgs84=DatasetBounds(west=0.0, south=0.0, east=5.0, north=5.0),
                time_start=date(2026, 1, 1),
                time_end=date(2026, 1, 31),
            ),
            CubeIndexRecord(
                cube_id="cube-b:serving",
                collection_id="landsat",
                representation="serving",
                path="oci://bucket/collections/landsat/serving/cube-b.zarr",
                bands=("B04", "B08"),
                version="v1",
                bbox_wgs84=DatasetBounds(west=0.0, south=0.0, east=5.0, north=5.0),
                time_start=date(2026, 1, 1),
                time_end=date(2026, 1, 31),
            ),
        ]
    )

    matches = index.find_candidates(
        collection_id="sentinel2",
        bands=["B04"],
        bbox=(1.0, 1.0, 2.0, 2.0),
        start=date(2026, 1, 10),
        end=date(2026, 1, 20),
    )

    assert [item.cube_id for item in matches] == ["cube-a:serving"]
