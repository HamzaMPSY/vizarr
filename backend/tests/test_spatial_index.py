import pytest

from app.index.spatial_index import DatasetSpatialIndex
from app.index.spatial_index import bounds_intersect_bbox
from app.index.spatial_index import parse_bbox_query
from app.models.dataset import DatasetBounds
from app.models.dataset import DatasetMeta


def test_parse_bbox_query_accepts_antimeridian_bbox() -> None:
    assert parse_bbox_query("170,-5,-170,5") == (170.0, -5.0, -170.0, 5.0)


def test_parse_bbox_query_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="west,south,east,north"):
        parse_bbox_query("1,2,3")
    with pytest.raises(ValueError, match="finite numbers"):
        parse_bbox_query("1,2,nan,4")
    with pytest.raises(ValueError, match="longitude"):
        parse_bbox_query("-181,0,10,5")
    with pytest.raises(ValueError, match="south"):
        parse_bbox_query("0,10,20,5")


def test_bounds_intersect_bbox_supports_antimeridian_dataset_bounds() -> None:
    bounds = DatasetBounds(west=170.0, south=-10.0, east=-170.0, north=10.0)

    assert bounds_intersect_bbox(bounds, (175.0, -5.0, -175.0, 5.0)) is True
    assert bounds_intersect_bbox(bounds, (-10.0, -5.0, 10.0, 5.0)) is False


def test_dataset_spatial_index_returns_intersecting_dataset_ids() -> None:
    index = DatasetSpatialIndex(
        [
            DatasetMeta(
                id="west",
                name="west",
                description="West dataset",
                variables=[],
                bounds=DatasetBounds(west=-120.0, south=30.0, east=-100.0, north=40.0),
            ),
            DatasetMeta(
                id="east",
                name="east",
                description="East dataset",
                variables=[],
                bounds=DatasetBounds(west=30.0, south=-5.0, east=31.0, north=-4.0),
            ),
            DatasetMeta(
                id="unbounded",
                name="unbounded",
                description="Unbounded dataset",
                variables=[],
                bounds=None,
            ),
        ]
    )

    assert index.query_ids((29.0, -6.0, 32.0, -3.0)) == {"east"}
