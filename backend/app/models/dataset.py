from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


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


class CompositeStyle(BaseModel):
    id: str
    name: str
    description: str
    bands: list[str]


class LayoutValidationIssue(BaseModel):
    code: str
    message: str
    remediation: str


class LayoutValidation(BaseModel):
    adapter_name: str | None = None
    adapter_priority: int | None = None
    accepted: bool = False
    data_array_name: str | None = None
    band_array_name: str | None = None
    variable_array_names: dict[str, str] = Field(default_factory=dict)
    matched_dimensions: list[str] = Field(default_factory=list)
    accepted_dimensions: list[str] = Field(default_factory=list)
    required_metadata: list[str] = Field(default_factory=list)
    crs_transform_conventions: list[str] = Field(default_factory=list)
    tile_capabilities: list[str] = Field(default_factory=list)
    readback_capabilities: list[str] = Field(default_factory=list)
    issues: list[LayoutValidationIssue] = Field(default_factory=list)


class DatasetMeta(BaseModel):
    id: str
    name: str
    description: str
    variables: list[VariableMeta]
    composite_styles: list[CompositeStyle] = Field(default_factory=list)
    bounds: DatasetBounds | None = None
    native_resolution_m: float | None = None
    crs_wkt: str | None = None
    crs_authority: str | None = None
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
    layout_validation: LayoutValidation | None = None


class ChunkLayout(BaseModel):
    sharded: bool
    shard_shape: list[int] | None = None
    inner_chunk_shape: list[int] | None = None


class MultiscaleLevelProfile(BaseModel):
    path: str
    browse_zoom: int | None = None
    bbox_wgs84: list[float] | None = None
    bbox_epsg3857: list[float] | None = None
    shape: list[int] = Field(default_factory=list)
    chunks: list[int] = Field(default_factory=list)
    dtype: str | None = None
    compressor: Any | None = None
    filters: Any | None = None
    order: str | None = None
    dimension_separator: Literal[".", "/"] = "."
    browser_readable: bool = False
    browser_gpu_compatible: bool = False
    gaps: list[str] = Field(default_factory=list)


BrowseCoverageStatus = Literal["missing", "partial", "complete", "queued", "running", "failed"]


class BrowseCoverage(BaseModel):
    expected_zoom_levels: list[int] = Field(default_factory=list)
    available_zoom_levels: list[int] = Field(default_factory=list)
    missing_variables: list[str] = Field(default_factory=list)
    missing_time_steps: dict[str, list[int]] = Field(default_factory=dict)
    last_generated_at: datetime | None = None
    generation_status: BrowseCoverageStatus = "missing"
    expected_artifact_count: int = 0
    available_artifact_count: int = 0


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
    multiscale_levels: list[MultiscaleLevelProfile] = Field(default_factory=list)
    browse_overview_zoom_levels: list[int]
    browse_overview_max_zoom: int | None = None
    browse_coverage: BrowseCoverage
    chunk_layout: ChunkLayout | None = None
    layout_validation: LayoutValidation | None = None
    supported_rendering_modes: list[str]
    browser_multiscale_ready: bool
    browser_gpu_ready: bool = False
    browser_gpu_reason: str | None = None
    browser_gpu_gaps: list[str] = Field(default_factory=list)
    seamless_rendering_ready: bool
    seamless_rendering_gaps: list[str]


class TileJSON(BaseModel):
    tilejson: str = "3.0.0"
    name: str
    tiles: list[str]
    bounds: list[float]
    center: list[float] | None = None
    minzoom: int
    maxzoom: int
    detail_minzoom: int | None = None
    has_coarse_fallback: bool = False
    coarse_representation: str | None = None
