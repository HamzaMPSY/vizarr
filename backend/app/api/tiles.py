from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.core.browse_tiles import BrowseTileResult
from app.core.browse_tiles import generate_browse_tile
from app.core.cache import build_tile_cache_key
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.dataset_catalog import ensure_catalog_entry_metadata_ready
from app.core.dataset_catalog import get_or_build_catalog
from app.core.multiscale_tiles import generate_and_cache_pyramid_tile
from app.core.multiscale_tiles import generate_pyramid_tile
from app.core.projected_tile_generator import generate_projected_band_tile
from app.core.tile_generator import generate_tile
from app.core.variable_display import resolve_display_range


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
    planner = request.app.state.planner
    tile_plan = planner.plan_tile_request(
        collection_id=dataset_id,
        style=colormap,
        z=z,
        x=x,
        y=y,
        time_index=time_index,
        params={
            "variable": variable,
            "colormap": colormap,
            "vmin": vmin,
            "vmax": vmax,
            "band_count": 1,
        },
    )
    if settings.storage_backend == "oci_zarr":
        catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            ensure_entry = ensure_catalog_entry_ready if tile_plan.chosen_representation == "browse" else ensure_catalog_entry_metadata_ready
            await run_in_threadpool(ensure_entry, entry, request.app.state.storage_connector)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
                "representation": tile_plan.chosen_representation,
                "planner_version": settings.planner_version,
            }
        )
        cached = await request.app.state.cache.get(cache_key)
        selected_variable = next(item for item in entry.meta.variables if item.id == variable)
        if cached is not None:
            cached_vmin, cached_vmax = resolve_display_range(selected_variable, vmin, vmax)
            return Response(
                cached,
                media_type="image/webp",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Cache-Status": "HIT",
                    "X-Data-Vmin": str(cached_vmin),
                    "X-Data-Vmax": str(cached_vmax),
                    "X-Request-Class": tile_plan.request_class,
                    "X-Execution-Path": tile_plan.execution_path,
                    "X-Representation": tile_plan.chosen_representation,
                },
            )

        tile_generator = generate_projected_band_tile
        tile_args = (
            request.app.state.storage_connector,
            entry,
            variable,
            z,
            x,
            y,
            time_index,
            colormap,
            vmin,
            vmax,
        )
        actual_representation = tile_plan.chosen_representation
        browse_source: str | None = None
        if tile_plan.chosen_representation == "browse":
            try:
                browse_result = await run_in_threadpool(
                    generate_browse_tile,
                    settings,
                    request.app.state.storage_connector,
                    entry,
                    variable,
                    z,
                    x,
                    y,
                    time_index,
                    colormap,
                    vmin,
                    vmax,
                )
                if not isinstance(browse_result, BrowseTileResult):
                    raise TypeError("Browse tile generation returned an unexpected result")
                tile_bytes = browse_result.tile_bytes
                actual_vmin, actual_vmax = browse_result.display_range
                browse_source = browse_result.source
            except FileNotFoundError:
                actual_representation = "serving"
                tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)
        elif tile_plan.chosen_representation == "pyramid":
            try:
                tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(
                    generate_pyramid_tile,
                    request.app.state.storage_connector,
                    entry,
                    variable,
                    z,
                    x,
                    y,
                    time_index,
                    colormap,
                    vmin,
                    vmax,
                )
            except FileNotFoundError:
                actual_representation = "serving"
                tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(
                    generate_and_cache_pyramid_tile,
                    request.app.state.storage_connector,
                    entry,
                    variable,
                    z,
                    x,
                    y,
                    time_index,
                    colormap,
                    vmin,
                    vmax,
                )
            except ValueError:
                actual_representation = "serving"
                tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)
        else:
            tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)

        if actual_representation == tile_plan.chosen_representation:
            await request.app.state.cache.set(cache_key, tile_bytes)
        headers = {
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Status": "MISS",
            "X-Data-Vmin": str(actual_vmin),
            "X-Data-Vmax": str(actual_vmax),
            "X-Request-Class": tile_plan.request_class,
            "X-Execution-Path": tile_plan.execution_path,
            "X-Representation": actual_representation,
        }
        if browse_source is not None:
            headers["X-Browse-Source"] = browse_source
        return Response(
            tile_bytes,
            media_type="image/webp",
            headers=headers,
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
            "representation": tile_plan.chosen_representation,
            "planner_version": settings.planner_version,
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
                "X-Request-Class": tile_plan.request_class,
                "X-Execution-Path": tile_plan.execution_path,
                "X-Representation": tile_plan.chosen_representation,
            },
        )

    tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(
        generate_tile,
        registry.dataset,
        registry.meta,
        variable,
        z,
        x,
        y,
        time_index,
        colormap,
        vmin,
        vmax,
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
            "X-Request-Class": tile_plan.request_class,
            "X-Execution-Path": tile_plan.execution_path,
            "X-Representation": tile_plan.chosen_representation,
        },
    )
