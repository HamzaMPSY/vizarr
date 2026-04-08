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


class DatasetMeta(BaseModel):
    id: str
    name: str
    description: str
    variables: list[VariableMeta]
    bounds: DatasetBounds | None = None
