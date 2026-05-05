from pydantic import BaseModel


class DatasetBounds(BaseModel):
    west: float
    south: float
    east: float
    north: float


class VariableStats(BaseModel):
    min: float
    max: float
    p02: float
    p98: float


class VariableMeta(BaseModel):
    id: str
    name: str
    unit: str
    time_steps: int
    stats: VariableStats
    display_vmin: float | None = None
    display_vmax: float | None = None
    default_colormap: str | None = None


class DatasetMeta(BaseModel):
    id: str
    name: str
    description: str
    variables: list[VariableMeta]
    bounds: DatasetBounds | None = None
    native_resolution_m: float | None = None
    time_values: list[str] | None = None
