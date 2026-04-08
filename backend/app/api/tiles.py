from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.core.cache import build_tile_cache_key
from app.core.dataset_catalog import get_or_build_catalog
from app.core.projected_tile_generator import generate_projected_band_tile
from app.core.tile_generator import generate_tile


router = APIRouter(prefix="/tiles", tags=["tiles"])


@router.get("/{dataset_id}/{variable}/{z}/{x}/{y}")
async def get_tile(
    dataset_id: str,
    variable: str,
    z: int,
    x: int,
    y: int,
    request: Request,
    time_index: int = Query(default=0, ge=0),
    colormap: str = Query(default="viridis"),
    vmin: float | None = None,
    vmax: float | None = None,
) -> Response:
    settings = request.app.state.settings
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        if variable not in entry.band_indices:
            raise HTTPException(status_code=404, detail="Variable not found")

        cache_key = build_tile_cache_key(
            {
                "dataset_id": dataset_id,
                "variable": variable,
                "z": z,
                "x": x,
                "y": y,
                "time_index": time_index,
                "colormap": colormap,
                "vmin": vmin,
                "vmax": vmax,
            }
        )
        cached = await request.app.state.cache.get(cache_key)
        selected_variable = next(item for item in entry.meta.variables if item.id == variable)
        if cached is not None:
            return Response(
                cached,
                media_type="image/webp",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Cache-Status": "HIT",
                    "X-Data-Vmin": str(vmin if vmin is not None else selected_variable.stats.p02),
                    "X-Data-Vmax": str(vmax if vmax is not None else selected_variable.stats.p98),
                },
            )

        tile_bytes, (actual_vmin, actual_vmax) = generate_projected_band_tile(
            connector=request.app.state.storage_connector,
            entry=entry,
            variable=variable,
            z=z,
            x=x,
            y=y,
            time_index=time_index,
            colormap=colormap,
            vmin=vmin,
            vmax=vmax,
        )
        await request.app.state.cache.set(cache_key, tile_bytes)
        return Response(
            tile_bytes,
            media_type="image/webp",
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Cache-Status": "MISS",
                "X-Data-Vmin": str(actual_vmin),
                "X-Data-Vmax": str(actual_vmax),
            },
        )

    registry = request.app.state.registry
    if dataset_id != registry.meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    variable_ids = {item.id for item in registry.meta.variables}
    if variable not in variable_ids:
        raise HTTPException(status_code=404, detail="Variable not found")

    cache_key = build_tile_cache_key(
        {
            "dataset_id": dataset_id,
            "variable": variable,
            "z": z,
            "x": x,
            "y": y,
            "time_index": time_index,
            "colormap": colormap,
            "vmin": vmin,
            "vmax": vmax,
        }
    )

    cached = await request.app.state.cache.get(cache_key)
    if cached is not None:
        vmin_value, vmax_value = next(
            (item.stats.p02, item.stats.p98) for item in registry.meta.variables if item.id == variable
        )
        return Response(
            cached,
            media_type="image/webp",
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Cache-Status": "HIT",
                "X-Data-Vmin": str(vmin if vmin is not None else vmin_value),
                "X-Data-Vmax": str(vmax if vmax is not None else vmax_value),
            },
        )

    tile_bytes, (actual_vmin, actual_vmax) = generate_tile(
        dataset=registry.dataset,
        meta=registry.meta,
        variable=variable,
        z=z,
        x=x,
        y=y,
        time_index=time_index,
        colormap=colormap,
        vmin=vmin,
        vmax=vmax,
    )
    await request.app.state.cache.set(cache_key, tile_bytes)
    return Response(
        tile_bytes,
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Status": "MISS",
            "X-Data-Vmin": str(actual_vmin),
            "X-Data-Vmax": str(actual_vmax),
        },
    )
