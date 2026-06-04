import math
from dataclasses import dataclass
from typing import Iterable

from app.core.tile_generator import tile_to_bbox
from app.models.dataset import DatasetBounds
from app.models.dataset import DatasetMeta


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class DatasetSpatialIndexRecord:
    dataset_id: str
    bounds: DatasetBounds


class DatasetSpatialIndex:
    def __init__(self, datasets: Iterable[DatasetMeta] | None = None) -> None:
        self._records: list[DatasetSpatialIndexRecord] = []
        if datasets is not None:
            self.replace(datasets)

    def replace(self, datasets: Iterable[DatasetMeta]) -> None:
        self._records = [
            DatasetSpatialIndexRecord(dataset_id=dataset.id, bounds=dataset.bounds)
            for dataset in datasets
            if dataset.bounds is not None and bounds_are_valid(dataset.bounds)
        ]

    def all(self) -> list[DatasetSpatialIndexRecord]:
        return list(self._records)

    def query_ids(self, bbox: BBox) -> set[str]:
        return {
            record.dataset_id
            for record in self._records
            if bounds_intersect_bbox(record.bounds, bbox)
        }


def build_dataset_spatial_index_records(app, *, allow_catalog_build: bool = True) -> list[DatasetMeta]:
    settings = app.state.settings
    if settings.storage_backend == "oci_zarr" and getattr(app.state, "storage_connector", None) is not None:
        catalog = getattr(app.state, "dataset_catalog", None)
        if catalog is None and allow_catalog_build:
            from app.core.dataset_catalog import get_or_build_catalog

            catalog = get_or_build_catalog(app)
        if catalog is not None:
            return [entry.meta for entry in catalog.values()]

        manifest = getattr(app.state, "dataset_manifest", None)
        if manifest:
            return [
                item if isinstance(item, DatasetMeta) else DatasetMeta.model_validate(item)
                for item in manifest
            ]

    return [app.state.registry.meta]


def parse_bbox_query(raw_bbox: str | None) -> BBox | None:
    if raw_bbox is None:
        return None
    parts = raw_bbox.split(",")
    if len(parts) != 4:
        raise ValueError("bbox must contain west,south,east,north")
    try:
        west, south, east, north = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ValueError("bbox values must be finite numbers") from exc

    values = (west, south, east, north)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bbox values must be finite numbers")
    if west < -180.0 or west > 180.0 or east < -180.0 or east > 180.0:
        raise ValueError("bbox longitude values must be between -180 and 180")
    if south < -90.0 or south > 90.0 or north < -90.0 or north > 90.0:
        raise ValueError("bbox latitude values must be between -90 and 90")
    if south > north:
        raise ValueError("bbox south must be less than or equal to north")
    if west == east:
        raise ValueError("bbox west and east must not be equal")
    return west, south, east, north


def bounds_are_valid(bounds: DatasetBounds) -> bool:
    values = (bounds.west, bounds.south, bounds.east, bounds.north)
    if not all(math.isfinite(value) for value in values):
        return False
    if bounds.west < -180.0 or bounds.west > 180.0 or bounds.east < -180.0 or bounds.east > 180.0:
        return False
    if bounds.south < -90.0 or bounds.south > 90.0 or bounds.north < -90.0 or bounds.north > 90.0:
        return False
    return bounds.south <= bounds.north and bounds.west != bounds.east


def bounds_intersect_bbox(bounds: DatasetBounds | None, bbox: BBox) -> bool:
    if bounds is None or not bounds_are_valid(bounds):
        return False
    west, south, east, north = bbox
    if north < bounds.south or south > bounds.north:
        return False
    return any(
        _segments_intersect(dataset_west, dataset_east, query_west, query_east)
        for dataset_west, dataset_east in _longitude_segments(bounds.west, bounds.east)
        for query_west, query_east in _longitude_segments(west, east)
    )


def tile_intersects_bounds(bounds: DatasetBounds | None, z: int, x: int, y: int) -> bool:
    if bounds is None:
        return True
    return bounds_intersect_bbox(bounds, tile_to_bbox(z, x, y))


def _longitude_segments(west: float, east: float) -> list[tuple[float, float]]:
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def _segments_intersect(west_a: float, east_a: float, west_b: float, east_b: float) -> bool:
    return not (east_a < west_b or west_a > east_b)
