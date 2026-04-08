from pydantic import BaseModel, Field


class TileQueryParams(BaseModel):
    time_index: int = Field(default=0, ge=0)
    colormap: str = "viridis"
    vmin: float | None = None
    vmax: float | None = None

