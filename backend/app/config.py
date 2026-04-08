from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Vizarr POC API"
    app_port: int = 8000
    use_synthetic_data: bool = True
    redis_url: str = "redis://localhost:6379/0"
    tile_cache_ttl: int = 3600
    colormap_default: str = "viridis"
    default_dataset_id: str = "demo-global"
    default_time_index: int = 0
    storage_backend: str = "synthetic"
    oci_config_profile: str = "prof"
    oci_config_file: str = "/home/app/.oci/config"
    oci_namespace: str = ""
    oci_bucket: str = ""
    oci_prefix: str = ""
    oci_dataset_id: str = ""
    oci_dataset_name: str = ""
    oci_dataset_description: str = ""
    oci_zarr_path: str = ""
    oci_zarr_consolidated: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
