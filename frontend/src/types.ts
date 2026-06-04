export interface VariableStats {
  min: number;
  max: number;
  p02: number;
  p98: number;
}

export interface VariableMeta {
  id: string;
  name: string;
  unit: string;
  time_steps: number;
  stats: VariableStats;
  display_vmin?: number | null;
  display_vmax?: number | null;
  default_colormap?: string | null;
}

export interface CompositeStyle {
  id: string;
  name: string;
  description: string;
  bands: string[];
}

export interface DatasetBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export type BBox = [number, number, number, number];

export interface RangeStatsResponse {
  result_type: "range_stats";
  dataset_id: string;
  variable: string;
  time_index: number;
  bbox?: BBox | null;
  stats_source: "metadata" | "sampled_bbox";
  min: number | null;
  max: number | null;
  p02: number | null;
  p98: number | null;
  histogram_bins: number[];
  histogram_counts: number[];
  valid_count: number;
  unit?: string | null;
  notes: string[];
}

export interface DatasetMeta {
  id: string;
  name: string;
  description: string;
  variables: VariableMeta[];
  composite_styles: CompositeStyle[];
  bounds?: DatasetBounds | null;
  native_resolution_m?: number | null;
  crs_wkt?: string | null;
  crs_authority?: string | null;
  time_values?: string[] | null;
  zarr_format?: number | null;
  zarr_consolidated?: boolean | null;
  zarr_proxy_root?: string | null;
  multiscale_store_path?: string | null;
  multiscale_zarr_format?: number | null;
  multiscale_zarr_consolidated?: boolean | null;
  multiscale_proxy_root?: string | null;
  multiscale_population_strategy?: string | null;
  multiscale_prepopulated_zoom_max?: number | null;
  multiscale_max_zoom?: number | null;
  layout_validation?: LayoutValidation | null;
}

export interface ChunkLayout {
  sharded: boolean;
  shard_shape?: number[] | null;
  inner_chunk_shape?: number[] | null;
}

export interface LayoutValidationIssue {
  code: string;
  message: string;
  remediation: string;
}

export interface LayoutValidation {
  adapter_name?: string | null;
  adapter_priority?: number | null;
  accepted: boolean;
  data_array_name?: string | null;
  band_array_name?: string | null;
  variable_array_names: Record<string, string>;
  matched_dimensions: string[];
  accepted_dimensions: string[];
  required_metadata: string[];
  crs_transform_conventions: string[];
  tile_capabilities: string[];
  readback_capabilities: string[];
  issues: LayoutValidationIssue[];
}

export type BrowseCoverageStatus = 'missing' | 'partial' | 'complete' | 'queued' | 'running' | 'failed';

export interface BrowseCoverage {
  expected_zoom_levels: number[];
  available_zoom_levels: number[];
  missing_variables: string[];
  missing_time_steps: Record<string, number[]>;
  last_generated_at?: string | null;
  generation_status: BrowseCoverageStatus;
  expected_artifact_count: number;
  available_artifact_count: number;
}

export type ServingProfileGap =
  | 'missing_data_array_metadata'
  | 'missing_dimension_metadata'
  | 'unsupported_dimension_order'
  | 'missing_crs_metadata'
  | 'missing_spatial_transform'
  | 'missing_browser_proxy'
  | 'missing_multiscale_pyramid'
  | 'multiscale_store_not_browser_readable'
  | 'missing_browse_overviews'
  | 'incomplete_browse_overview_coverage';

export interface DatasetServingProfile {
  dataset_id: string;
  zarr_format?: number | null;
  zarr_consolidated?: boolean | null;
  zarr_proxy_root?: string | null;
  multiscale_store_path?: string | null;
  multiscale_zarr_format?: number | null;
  multiscale_zarr_consolidated?: boolean | null;
  multiscale_proxy_root?: string | null;
  multiscale_population_strategy?: string | null;
  multiscale_prepopulated_zoom_max?: number | null;
  multiscale_max_zoom?: number | null;
  data_array_name?: string | null;
  variable_ids: string[];
  has_multiscale: boolean;
  multiscale_paths: string[];
  multiscale_levels?: MultiscaleLevelProfile[];
  browse_overview_zoom_levels: number[];
  browse_overview_max_zoom?: number | null;
  browse_coverage: BrowseCoverage;
  chunk_layout?: ChunkLayout | null;
  layout_validation?: LayoutValidation | null;
  supported_rendering_modes: string[];
  browser_multiscale_ready: boolean;
  browser_gpu_ready?: boolean;
  browser_gpu_reason?: string | null;
  browser_gpu_gaps?: string[];
  seamless_rendering_ready: boolean;
  seamless_rendering_gaps: ServingProfileGap[];
}

export interface MultiscaleLevelProfile {
  path: string;
  browse_zoom?: number | null;
  bbox_wgs84?: BBox | null;
  bbox_epsg3857?: BBox | null;
  shape: number[];
  chunks: number[];
  dtype?: string | null;
  compressor?: unknown;
  filters?: unknown;
  order?: string | null;
  dimension_separator: "." | "/";
  browser_readable: boolean;
  browser_gpu_compatible: boolean;
  gaps: string[];
}

export interface TileJson {
  tilejson: string;
  name: string;
  tiles: string[];
  bounds: BBox;
  center?: [number, number, number] | null;
  minzoom: number;
  maxzoom: number;
  detail_minzoom?: number | null;
  has_coarse_fallback: boolean;
  coarse_representation?: string | null;
}

export interface BrowseGenerationAcceptedResponse {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  result_type: "browse_generation";
  dataset_id: string;
  progress: number;
  total_artifacts: number;
  completed_artifacts: number;
  can_retry: boolean;
}
