from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.models.dataset import DatasetBounds
from app.models.plans import Representation


@dataclass(frozen=True)
class CubeIndexRecord:
    cube_id: str
    collection_id: str
    representation: Representation
    path: str
    bands: tuple[str, ...]
    version: str
    bbox_wgs84: DatasetBounds | None = None
    time_start: date | None = None
    time_end: date | None = None
    style: str | None = None
    crs: str | None = None


class PlannerIndex:
    def __init__(self, records: Iterable[CubeIndexRecord] | None = None) -> None:
        self._records = list(records or [])

    def replace(self, records: Iterable[CubeIndexRecord]) -> None:
        self._records = list(records)

    def all(self) -> list[CubeIndexRecord]:
        return list(self._records)

    def find_candidates(
        self,
        *,
        collection_id: str,
        bands: list[str] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        start: date | None = None,
        end: date | None = None,
        style: str | None = None,
    ) -> list[CubeIndexRecord]:
        required_bands = set(bands or [])
        matches: list[CubeIndexRecord] = []

        for record in self._records:
            if record.collection_id != collection_id:
                continue
            if required_bands and record.bands and not required_bands.issubset(set(record.bands)):
                continue
            if style is not None and record.representation == "browse" and record.style is not None and record.style != style:
                continue
            if bbox is not None and record.bbox_wgs84 is not None and not _bboxes_intersect(record.bbox_wgs84, bbox):
                continue
            if start is not None and end is not None and record.time_start and record.time_end:
                if record.time_end < start or record.time_start > end:
                    continue
            matches.append(record)

        return matches


def _bboxes_intersect(bounds: DatasetBounds, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return not (
        east < bounds.west
        or west > bounds.east
        or north < bounds.south
        or south > bounds.north
    )
