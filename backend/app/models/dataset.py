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
    zarr_format: int | None = None
    zarr_consolidated: bool | None = None
    zarr_proxy_root: str | None = None
    multiscale_store_path: str | None = None
    multiscale_zarr_format: int | None = None
    multiscale_zarr_consolidated: bool | None = None
    multiscale_proxy_root: str | None = None
    multiscale_population_strategy: str | None = None
    multiscale_prepopulated_zoom_max: int | None = None
    multiscale_max_zoom: int | None = None


class ChunkLayout(BaseModel):
    sharded: bool
    shard_shape: list[int] | None = None
    inner_chunk_shape: list[int] | None = None


class DatasetServingProfile(BaseModel):
    dataset_id: str
    zarr_format: int | None = None
    zarr_consolidated: bool | None = None
    zarr_proxy_root: str | None = None
    multiscale_store_path: str | None = None
    multiscale_zarr_format: int | None = None
    multiscale_zarr_consolidated: bool | None = None
    multiscale_proxy_root: str | None = None
    multiscale_population_strategy: str | None = None
    multiscale_prepopulated_zoom_max: int | None = None
    multiscale_max_zoom: int | None = None
    data_array_name: str | None = None
    variable_ids: list[str]
    has_multiscale: bool
    multiscale_paths: list[str]
    browse_overview_zoom_levels: list[int]
    browse_overview_max_zoom: int | None = None
    chunk_layout: ChunkLayout | None = None
    supported_rendering_modes: list[str]
    browser_multiscale_ready: bool
    seamless_rendering_ready: bool
    seamless_rendering_gaps: list[str]


class TileJSON(BaseModel):
    tilejson: str = "3.0.0"
    name: str
    tiles: list[str]
    bounds: list[float]
    minzoom: int
    maxzoom: int
    detail_minzoom: int | None = None
    has_coarse_fallback: bool = False
    coarse_representation: str | None = None
