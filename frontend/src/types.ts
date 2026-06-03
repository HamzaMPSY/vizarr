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
}

export interface ChunkLayout {
  sharded: boolean;
  shard_shape?: number[] | null;
  inner_chunk_shape?: number[] | null;
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
  data_array_name?: string | null;
  variable_ids: string[];
  has_multiscale: boolean;
  multiscale_paths: string[];
  multiscale_levels?: MultiscaleLevelProfile[];
  browse_overview_zoom_levels: number[];
  browse_overview_max_zoom?: number | null;
  browse_coverage: BrowseCoverage;
  chunk_layout?: ChunkLayout | null;
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
  bbox_wgs84?: [number, number, number, number] | null;
  bbox_epsg3857?: [number, number, number, number] | null;
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
  bounds: [number, number, number, number];
  center?: [number, number, number] | null;
  minzoom: number;
  maxzoom: number;
  detail_minzoom?: number | null;
  has_coarse_fallback: boolean;
  coarse_representation?: string | null;
}
