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
  browse_overview_zoom_levels: number[];
  browse_overview_max_zoom?: number | null;
  chunk_layout?: ChunkLayout | null;
  supported_rendering_modes: string[];
  browser_multiscale_ready: boolean;
  seamless_rendering_ready: boolean;
  seamless_rendering_gaps: string[];
}

export interface TileJson {
  tilejson: string;
  name: string;
  tiles: string[];
  bounds: [number, number, number, number];
  minzoom: number;
  maxzoom: number;
  detail_minzoom?: number | null;
  has_coarse_fallback: boolean;
  coarse_representation?: string | null;
}
