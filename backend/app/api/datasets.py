from fastapi import APIRouter, HTTPException, Request

from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.dataset_catalog import get_or_build_catalog
from app.models.dataset import DatasetMeta, VariableMeta


router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetMeta])
async def list_datasets(request: Request) -> list[DatasetMeta]:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
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
        ensure_catalog_entry_ready(entry, request.app.state.storage_connector)
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
        ensure_catalog_entry_ready(entry, request.app.state.storage_connector)
        return entry.meta.variables
    meta = request.app.state.registry.meta
    if dataset_id != meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return meta.variables
