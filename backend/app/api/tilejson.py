from urllib.parse import quote
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.dataset_catalog import get_or_build_catalog
from app.core.tilejson import build_dataset_tilejson
from app.core.tilejson import build_registry_tilejson
from app.models.dataset import TileJSON


router = APIRouter(prefix="/tilejson", tags=["tilejson"])


@router.get("/{dataset_id}/{variable}", response_model=TileJSON)
async def get_tilejson(
    dataset_id: str,
    variable: str,
    request: Request,
    time_index: int = Query(default=0, ge=0),
    colormap: str = Query(default="viridis"),
    vmin: float | None = None,
    vmax: float | None = None,
) -> TileJSON:
    tile_template = _tile_template(
        request=request,
        dataset_id=dataset_id,
        variable=variable,
        time_index=time_index,
        colormap=colormap,
        vmin=vmin,
        vmax=vmax,
    )
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return build_dataset_tilejson(
            settings,
            request.app.state.storage_connector,
            entry,
            variable=variable,
            time_index=time_index,
            tile_template=tile_template,
        )

    registry = request.app.state.registry
    if dataset_id != registry.meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if variable not in {item.id for item in registry.meta.variables}:
        raise HTTPException(status_code=404, detail="Variable not found")

    return build_registry_tilejson(
        name=f"{registry.meta.name}:{variable}",
        bounds=registry.meta.bounds,
        native_resolution_m=registry.meta.native_resolution_m,
        tile_template=tile_template,
    )


def _tile_template(
    *,
    request: Request,
    dataset_id: str,
    variable: str,
    time_index: int,
    colormap: str,
    vmin: float | None,
    vmax: float | None,
) -> str:
    query = {
        "time_index": str(time_index),
        "colormap": colormap,
    }
    if vmin is not None:
        query["vmin"] = str(vmin)
    if vmax is not None:
        query["vmax"] = str(vmax)
    encoded_dataset = quote(dataset_id, safe="")
    encoded_variable = quote(variable, safe="")
    path = f"/api/tiles/{encoded_dataset}/{encoded_variable}/{{z}}/{{x}}/{{y}}?{urlencode(query)}"
    return f"{str(request.base_url).rstrip('/')}{path}"
