from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.dataset_catalog import get_or_build_catalog
from app.core.serving_profile import build_dataset_serving_profile
from app.models.dataset import DatasetMeta, DatasetServingProfile, VariableMeta


router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetMeta])
async def list_datasets(request: Request) -> list[DatasetMeta]:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        manifest = getattr(request.app.state, "dataset_manifest", None)
        catalog = get_or_build_catalog(request.app)
        connector = request.app.state.storage_connector
        needs_metadata_hydration = manifest is None or any(not item.variables for item in manifest)

        if needs_metadata_hydration and connector is not None:
            for entry in catalog.values():
                if entry.meta.variables:
                    continue
                try:
                    await run_in_threadpool(
                        ensure_catalog_entry_metadata_ready,
                        entry,
                        connector,
                    )
                except Exception:
                    continue
            manifest = [entry.meta.model_copy(deep=True) for entry in catalog.values()]
            request.app.state.dataset_manifest = manifest

        if manifest is not None:
            return manifest
        return [entry.meta for entry in catalog.values()]
    return [request.app.state.registry.meta]


@router.get("/{dataset_id}", response_model=DatasetMeta)
async def get_dataset(dataset_id: str, request: Request) -> DatasetMeta:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            await run_in_threadpool(
                ensure_catalog_entry_ready,
                entry,
                request.app.state.storage_connector,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return entry.meta
    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return meta


@router.get("/{dataset_id}/variables", response_model=list[VariableMeta])
async def list_variables(dataset_id: str, request: Request) -> list[VariableMeta]:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            await run_in_threadpool(
                ensure_catalog_entry_metadata_ready,
                entry,
                request.app.state.storage_connector,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return entry.meta.variables
    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return meta.variables


@router.get("/{dataset_id}/serving-profile", response_model=DatasetServingProfile)
async def get_dataset_serving_profile(dataset_id: str, request: Request) -> DatasetServingProfile:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            return await run_in_threadpool(
                build_dataset_serving_profile,
                settings,
                request.app.state.storage_connector,
                entry,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return DatasetServingProfile(
        dataset_id=meta.id,
        zarr_format=meta.zarr_format,
        zarr_consolidated=meta.zarr_consolidated,
        zarr_proxy_root=meta.zarr_proxy_root,
        multiscale_store_path=meta.multiscale_store_path,
        multiscale_zarr_format=meta.multiscale_zarr_format,
        multiscale_zarr_consolidated=meta.multiscale_zarr_consolidated,
        multiscale_proxy_root=meta.multiscale_proxy_root,
        multiscale_population_strategy=meta.multiscale_population_strategy,
        multiscale_prepopulated_zoom_max=meta.multiscale_prepopulated_zoom_max,
        multiscale_max_zoom=meta.multiscale_max_zoom,
        data_array_name=None,
        variable_ids=[item.id for item in meta.variables],
        has_multiscale=False,
        multiscale_paths=[],
        browse_overview_zoom_levels=[],
        browse_overview_max_zoom=None,
        chunk_layout=None,
        supported_rendering_modes=["dynamic_tiles"],
        browser_multiscale_ready=False,
        seamless_rendering_ready=False,
        seamless_rendering_gaps=["missing_browser_proxy", "missing_multiscale_pyramid", "missing_browse_overviews"],
    )
