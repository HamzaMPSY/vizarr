import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image
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
from app.core.projected_tile_generator import generate_projected_composite_tile
from app.core.projected_tile_generator import resolve_composite_band_ids
from app.core.tile_observability import TileRequestMetrics
from app.core.tile_observability import TileBudgetExceeded
from app.core.tile_observability import TileComputeBudget
from app.core.tile_observability import activate_tile_metrics
from app.core.tile_observability import build_tile_debug_headers
from app.core.tile_observability import enforce_tile_compute_budget
from app.core.tile_generator import generate_tile
from app.index.spatial_index import tile_intersects_bounds
from app.core.variable_display import resolve_display_range


router = APIRouter(prefix="/tiles", tags=["tiles"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RenderedTile:
    body: bytes
    vmin: float
    vmax: float
    representation: str
    browse_source: str | None = None


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
    cache_version: str | None = None,
) -> Response:
    metrics = TileRequestMetrics()
    with activate_tile_metrics(metrics):
        return await _get_tile_impl(
            dataset_id=dataset_id,
            variable=variable,
            z=z,
            x=x,
            y=y,
            request=request,
            time_index=time_index,
            colormap=colormap,
            vmin=vmin,
            vmax=vmax,
            cache_version=cache_version,
            metrics=metrics,
        )


async def _get_tile_impl(
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
    cache_version: str | None = None,
    metrics: TileRequestMetrics | None = None,
) -> Response:
    if metrics is None:
        metrics = TileRequestMetrics()
    settings = request.app.state.settings
    planner = request.app.state.planner
    effective_cache_version = cache_version or await request.app.state.cache.get_dataset_version(dataset_id)
    direct_tile_max_parallel_chunk_reads = _direct_tile_max_parallel_chunk_reads(settings)
    with metrics.time_block("planner"):
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
        with metrics.time_block("catalog_metadata"):
            catalog = get_or_build_catalog(request.app)
        entry = catalog.get(dataset_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            with metrics.time_block("catalog_metadata"):
                await run_in_threadpool(ensure_catalog_entry_metadata_ready, entry, request.app.state.storage_connector)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        composite_band_ids = resolve_composite_band_ids(entry, variable)
        is_composite = composite_band_ids is not None
        if variable not in entry.band_indices and not is_composite:
            raise HTTPException(status_code=404, detail="Variable not found")
        planned_representation = "serving" if is_composite else tile_plan.chosen_representation
        if not tile_intersects_bounds(entry.meta.bounds, z, x, y):
            return _empty_tile_response(
                settings=settings,
                metrics=metrics,
                dataset_id=dataset_id,
                variable=variable,
                z=z,
                x=x,
                y=y,
                request_class=tile_plan.request_class,
                execution_path=tile_plan.execution_path,
                planned_representation=planned_representation,
            )
        if tile_plan.chosen_representation == "browse" and not is_composite:
            try:
                with metrics.time_block("catalog_metadata"):
                    await run_in_threadpool(ensure_catalog_entry_ready, entry, request.app.state.storage_connector)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        cache_key = _build_tile_cache_key(
            settings,
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
                "representation": planned_representation,
                "render_mode": "composite" if is_composite else "band",
                "composite_bands": composite_band_ids,
                "planner_version": settings.planner_version,
                "cache_version": effective_cache_version,
            },
            vmin=vmin,
            vmax=vmax,
        )
        cached = None
        if cache_key is not None:
            with metrics.time_block("cache_lookup"):
                cached = await request.app.state.cache.get(cache_key)
        selected_variable = next((item for item in entry.meta.variables if item.id == variable), None)
        if cached is not None:
            cached_vmin, cached_vmax = (
                resolve_display_range(selected_variable, vmin, vmax)
                if selected_variable is not None
                else (0.0 if vmin is None else vmin, 255.0 if vmax is None else vmax)
            )
            return _tile_response(
                body=cached,
                settings=settings,
                metrics=metrics,
                dataset_id=dataset_id,
                variable=variable,
                z=z,
                x=x,
                y=y,
                cache_status="HIT",
                representation=planned_representation,
                execution_path=tile_plan.execution_path,
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Cache-Status": "HIT",
                    "X-Data-Vmin": str(cached_vmin),
                    "X-Data-Vmax": str(cached_vmax),
                    "X-Request-Class": tile_plan.request_class,
                    "X-Execution-Path": tile_plan.execution_path,
                    "X-Representation": planned_representation,
                },
            )

        async def _render_uncached_oci_tile() -> _RenderedTile:
            if is_composite:
                tile_generator = generate_projected_composite_tile
                tile_args = (
                    request.app.state.storage_connector,
                    entry,
                    variable,
                    z,
                    x,
                    y,
                    time_index,
                    vmin,
                    vmax,
                    direct_tile_max_parallel_chunk_reads,
                )
            else:
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
                    direct_tile_max_parallel_chunk_reads,
                )
            actual_representation = tile_plan.chosen_representation
            browse_source: str | None = None
            if is_composite:
                actual_representation = "serving"
                with metrics.time_block("representation_generation"):
                    tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)
            elif tile_plan.chosen_representation == "browse":
                try:
                    with metrics.time_block("representation_generation"):
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
                    with metrics.time_block("representation_generation"):
                        tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)
            elif tile_plan.chosen_representation == "pyramid":
                try:
                    with metrics.time_block("representation_generation"):
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
                    with metrics.time_block("representation_generation"):
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
                            direct_tile_max_parallel_chunk_reads,
                        )
                except ValueError:
                    actual_representation = "serving"
                    with metrics.time_block("representation_generation"):
                        tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)
            else:
                with metrics.time_block("representation_generation"):
                    tile_bytes, (actual_vmin, actual_vmax) = await run_in_threadpool(tile_generator, *tile_args)

            if actual_representation == "serving":
                _enforce_direct_tile_budget_or_raise(
                    settings=settings,
                    metrics=metrics,
                    dataset_id=dataset_id,
                    variable=variable,
                    z=z,
                    x=x,
                    y=y,
                    execution_path=tile_plan.execution_path,
                )
            else:
                metrics.record_budget_decision(
                    status="not_applicable",
                    reason=f"{actual_representation} representation did not use direct source serving",
                )

            if cache_key is not None and (is_composite or actual_representation == tile_plan.chosen_representation):
                await request.app.state.cache.set(cache_key, tile_bytes, dataset_id=dataset_id)
            return _RenderedTile(
                body=tile_bytes,
                vmin=actual_vmin,
                vmax=actual_vmax,
                representation=actual_representation,
                browse_source=browse_source,
            )

        coalescing_status = "bypass"
        if cache_key is not None:
            with metrics.time_block("request_coalescing"):
                rendered, coalescing_status = await request.app.state.tile_request_coalescer.run(
                    cache_key,
                    _render_uncached_oci_tile,
                )
        else:
            rendered = await _render_uncached_oci_tile()

        cache_status = "COALESCED" if coalescing_status == "follower" else "MISS"
        headers = {
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Status": cache_status,
            "X-Data-Vmin": str(rendered.vmin),
            "X-Data-Vmax": str(rendered.vmax),
            "X-Request-Class": tile_plan.request_class,
            "X-Execution-Path": tile_plan.execution_path,
            "X-Representation": rendered.representation,
            "X-Request-Coalescing": coalescing_status,
        }
        if rendered.browse_source is not None:
            headers["X-Browse-Source"] = rendered.browse_source
        return _tile_response(
            body=rendered.body,
            headers=headers,
            settings=settings,
            metrics=metrics,
            dataset_id=dataset_id,
            variable=variable,
            z=z,
            x=x,
            y=y,
            cache_status=cache_status,
            representation=rendered.representation,
            execution_path=tile_plan.execution_path,
            coalescing_status=coalescing_status,
        )

    registry = request.app.state.registry
    if dataset_id != registry.meta.id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    variable_ids = {item.id for item in registry.meta.variables}
    if variable not in variable_ids:
        raise HTTPException(status_code=404, detail="Variable not found")
    if not tile_intersects_bounds(registry.meta.bounds, z, x, y):
        return _empty_tile_response(
            settings=settings,
            metrics=metrics,
            dataset_id=dataset_id,
            variable=variable,
            z=z,
            x=x,
            y=y,
            request_class=tile_plan.request_class,
            execution_path=tile_plan.execution_path,
            planned_representation=tile_plan.chosen_representation,
        )

    cache_key = _build_tile_cache_key(
        settings,
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
            "cache_version": effective_cache_version,
        },
        vmin=vmin,
        vmax=vmax,
    )

    cached = None
    if cache_key is not None:
        with metrics.time_block("cache_lookup"):
            cached = await request.app.state.cache.get(cache_key)
    if cached is not None:
        vmin_value, vmax_value = next(
            (item.stats.p02, item.stats.p98) for item in registry.meta.variables if item.id == variable
        )
        return _tile_response(
            body=cached,
            settings=settings,
            metrics=metrics,
            dataset_id=dataset_id,
            variable=variable,
            z=z,
            x=x,
            y=y,
            cache_status="HIT",
            representation=tile_plan.chosen_representation,
            execution_path=tile_plan.execution_path,
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

    async def _render_uncached_synthetic_tile() -> _RenderedTile:
        with metrics.time_block("representation_generation"):
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
        if cache_key is not None:
            await request.app.state.cache.set(cache_key, tile_bytes, dataset_id=dataset_id)
        return _RenderedTile(
            body=tile_bytes,
            vmin=actual_vmin,
            vmax=actual_vmax,
            representation=tile_plan.chosen_representation,
        )

    coalescing_status = "bypass"
    if cache_key is not None:
        with metrics.time_block("request_coalescing"):
            rendered, coalescing_status = await request.app.state.tile_request_coalescer.run(
                cache_key,
                _render_uncached_synthetic_tile,
            )
    else:
        rendered = await _render_uncached_synthetic_tile()

    cache_status = "COALESCED" if coalescing_status == "follower" else "MISS"
    return _tile_response(
        body=rendered.body,
        settings=settings,
        metrics=metrics,
        dataset_id=dataset_id,
        variable=variable,
        z=z,
        x=x,
        y=y,
        cache_status=cache_status,
        representation=rendered.representation,
        execution_path=tile_plan.execution_path,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Status": cache_status,
            "X-Data-Vmin": str(rendered.vmin),
            "X-Data-Vmax": str(rendered.vmax),
            "X-Request-Class": tile_plan.request_class,
            "X-Execution-Path": tile_plan.execution_path,
            "X-Representation": rendered.representation,
            "X-Request-Coalescing": coalescing_status,
        },
        coalescing_status=coalescing_status,
    )


def _empty_tile_response(
    *,
    settings,
    metrics: TileRequestMetrics,
    dataset_id: str,
    variable: str,
    z: int,
    x: int,
    y: int,
    request_class: str,
    execution_path: str,
    planned_representation: str,
) -> Response:
    return _tile_response(
        body=_transparent_webp_tile(),
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Status": "BYPASS",
            "X-Data-Vmin": "0.0",
            "X-Data-Vmax": "0.0",
            "X-Request-Class": request_class,
            "X-Execution-Path": execution_path,
            "X-Representation": "empty",
            "X-Planned-Representation": planned_representation,
            "X-Tile-Empty": "bounds",
            "X-Request-Coalescing": "none",
        },
        settings=settings,
        metrics=metrics,
        dataset_id=dataset_id,
        variable=variable,
        z=z,
        x=x,
        y=y,
        cache_status="BYPASS",
        representation="empty",
        execution_path=execution_path,
        coalescing_status="none",
    )


@lru_cache(maxsize=1)
def _transparent_webp_tile() -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def _tile_response(
    *,
    body: bytes,
    headers: dict[str, str],
    settings,
    metrics: TileRequestMetrics,
    dataset_id: str,
    variable: str,
    z: int,
    x: int,
    y: int,
    cache_status: str,
    representation: str,
    execution_path: str,
    coalescing_status: str = "none",
) -> Response:
    metrics.finish()
    response_headers = dict(headers)
    response_headers.setdefault("X-Request-Coalescing", coalescing_status)
    if settings.tile_debug_headers_enabled:
        response_headers.update(build_tile_debug_headers(metrics))
    _log_tile_metrics(
        metrics=metrics,
        dataset_id=dataset_id,
        variable=variable,
        z=z,
        x=x,
        y=y,
        cache_status=cache_status,
        representation=representation,
        execution_path=execution_path,
        coalescing_status=coalescing_status,
    )
    return Response(body, media_type="image/webp", headers=response_headers)


def _build_tile_cache_key(settings, parts: dict, *, vmin: float | None, vmax: float | None) -> str | None:
    if not settings.tile_cache_custom_range_enabled and (vmin is not None or vmax is not None):
        return None
    return build_tile_cache_key(
        parts,
        display_range_decimals=settings.tile_cache_display_range_decimals,
    )


def _direct_tile_max_parallel_chunk_reads(settings) -> int | None:
    value = int(settings.direct_tile_max_parallel_chunk_reads)
    return value if value > 0 else None


def _direct_tile_compute_budget(settings) -> TileComputeBudget:
    return TileComputeBudget(
        max_object_gets=int(settings.direct_tile_max_object_gets),
        max_byte_range_gets=int(settings.direct_tile_max_byte_range_gets),
        max_object_bytes=int(settings.direct_tile_max_object_bytes),
        max_zarr_chunks=int(settings.direct_tile_max_zarr_chunks),
        max_shard_index_reads=int(settings.direct_tile_max_shard_index_reads),
    )


def _enforce_direct_tile_budget_or_raise(
    *,
    settings,
    metrics: TileRequestMetrics,
    dataset_id: str,
    variable: str,
    z: int,
    x: int,
    y: int,
    execution_path: str,
) -> None:
    try:
        enforce_tile_compute_budget(metrics, _direct_tile_compute_budget(settings))
    except TileBudgetExceeded as exc:
        metrics.finish()
        headers = {
            "X-Cache-Status": "BYPASS",
            "X-Representation": "serving",
            "X-Execution-Path": execution_path,
            "X-Tile-Budget-Status": "exceeded",
            "X-Tile-Budget-Reason": str(exc),
            "X-Tile-Budget-Metric": exc.metric,
            "X-Tile-Budget-Limit": str(exc.limit),
            "X-Tile-Budget-Actual": str(exc.actual),
        }
        if settings.tile_debug_headers_enabled:
            headers.update(build_tile_debug_headers(metrics))
        _log_tile_metrics(
            metrics=metrics,
            dataset_id=dataset_id,
            variable=variable,
            z=z,
            x=x,
            y=y,
            cache_status="BYPASS",
            representation="serving",
            execution_path=execution_path,
        )
        raise HTTPException(status_code=503, detail=exc.detail(), headers=headers) from exc


def _log_tile_metrics(
    *,
    metrics: TileRequestMetrics,
    dataset_id: str,
    variable: str,
    z: int,
    x: int,
    y: int,
    cache_status: str,
    representation: str,
    execution_path: str,
    coalescing_status: str = "none",
) -> None:
    payload = {
        "event": "tile_request_metrics",
        "dataset_id": dataset_id,
        "variable": variable,
        "z": z,
        "x": x,
        "y": y,
        "cache_status": cache_status,
        "coalescing_status": coalescing_status,
        "representation": representation,
        "execution_path": execution_path,
        **metrics.snapshot(),
    }
    logger.info("tile_request_metrics %s", json.dumps(payload, sort_keys=True))
