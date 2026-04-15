from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class QueryRequestBase(BaseModel):
    collection_id: str = Field(min_length=1)
    aoi_wkt: str = Field(min_length=1)
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def validate_time_window(self) -> "QueryRequestBase":
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class PreviewRequest(QueryRequestBase):
    bands: list[str] = Field(min_length=1)
    style: str = Field(min_length=1)
    max_size: int = Field(default=1024, ge=64, le=8192)


StatsMetric = Literal["mean", "min", "max", "p50", "p95", "p98"]


class StatsRequest(QueryRequestBase):
    bands: list[str] = Field(min_length=1)
    metrics: list[StatsMetric] = Field(min_length=1)


ClipOutputFormat = Literal["zarr", "geotiff", "tiff", "png"]


class ClipRequest(QueryRequestBase):
    bands: list[str] = Field(min_length=1)
    output_format: ClipOutputFormat = "zarr"


ExportOutputFormat = Literal["zarr", "geotiff", "parquet"]


class ExportRequest(QueryRequestBase):
    bands: list[str] = Field(min_length=1)
    output_format: ExportOutputFormat = "zarr"
