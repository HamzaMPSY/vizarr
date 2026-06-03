from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Vizarr POC API"
    app_environment: str = "development"
    app_port: int = 8000
    auth_enabled: bool = False
    auth_api_keys: str = ""
    use_synthetic_data: bool = True
    redis_url: str = "redis://localhost:6379/0"
    tile_cache_ttl: int = 3600
    tile_cache_display_range_decimals: int = 3
    tile_cache_custom_range_enabled: bool = True
    direct_tile_max_parallel_chunk_reads: int = 8
    direct_tile_max_object_gets: int = 0
    direct_tile_max_byte_range_gets: int = 0
    direct_tile_max_object_bytes: int = 0
    direct_tile_max_zarr_chunks: int = 0
    direct_tile_max_shard_index_reads: int = 0
    oci_text_cache_max_entries: int = 256
    oci_bytes_cache_max_entries: int = 128
    oci_bytes_cache_max_bytes: int = 134217728
    zarr_shard_index_cache_entries: int = 4096
    zarr_shard_index_cache_bytes: int = 67108864
    colormap_default: str = "viridis"
    planner_version: str = "v1"
    browse_enabled_styles: str = "ndvi-default,rgb-default"
    browse_tile_max_zoom: int = 8
    browse_tile_native_resolution_ratio: float = 12.0
    serving_tile_native_resolution_ratio: float = 6.0
    browse_overview_max_size: int = 1536
    browse_local_cache_dir: str = ".cache/browse"
    browse_prewarm_enabled: bool = True
    browse_prewarm_all_variables: bool = False
    browse_request_build_enabled: bool = False
    browse_dev_fallback_enabled: bool = True
    interactive_max_clip_bands: int = 4
    interactive_max_clip_days: int = 31
    default_dataset_id: str = "demo-global"
    default_time_index: int = 0
    storage_backend: str = "synthetic"
    oci_auth_mode: str = "auto"
    oci_config_profile: str = "prof"
    oci_config_file: str = "/home/app/.oci/config"
    oci_namespace: str = ""
    oci_bucket: str = ""
    oci_prefix: str = ""
    oci_browse_prefix_root: str = "browse"
    oci_multiscale_prefix_root: str = "multiscale"
    oci_dataset_id: str = ""
    oci_dataset_name: str = ""
    oci_dataset_description: str = ""
    oci_zarr_path: str = ""
    oci_zarr_consolidated: bool = True
    tile_debug_headers_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
