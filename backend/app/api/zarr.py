import mimetypes
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.core.dataset_catalog import CatalogEntry
from app.core.dataset_catalog import get_or_build_catalog


router = APIRouter(prefix="/zarr", tags=["zarr"])

_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_JSON_OBJECT_NAMES = ("zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata")


def _guess_content_type(object_path: str, fallback: str | None) -> str:
    if fallback:
        return fallback
    if object_path.endswith(_JSON_OBJECT_NAMES):
        return "application/json"
    guessed, _ = mimetypes.guess_type(object_path)
    return guessed or "application/octet-stream"


def _resolve_object_path(store_path: str, object_path: str) -> str:
    cleaned = object_path.strip("/")
    if not cleaned or cleaned in {".", ".."} or "/./" in f"/{cleaned}/" or "/../" in f"/{cleaned}/":
        raise HTTPException(status_code=400, detail="Invalid Zarr object path")
    return f"{store_path.rstrip('/')}/{cleaned}"


def _parse_range_header(
    header_value: str | None,
    *,
    object_size: int | None,
) -> tuple[int | None, int | None, int | None]:
    if header_value is None:
        return None, None, None

    match = _RANGE_PATTERN.match(header_value.strip())
    if match is None:
        raise HTTPException(status_code=416, detail="Unsupported Range header")

    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise HTTPException(status_code=416, detail="Invalid Range header")

    if start_raw and end_raw:
        start = int(start_raw)
        end_inclusive = int(end_raw)
        if end_inclusive < start:
            raise HTTPException(status_code=416, detail="Invalid Range header")
        return start, end_inclusive + 1, end_inclusive - start + 1

    if start_raw:
        start = int(start_raw)
        if object_size is None:
            return start, None, None
        if start >= object_size:
            raise HTTPException(status_code=416, detail="Requested range is outside the object")
        return start, None, object_size - start

    suffix_length = int(end_raw)
    if suffix_length <= 0:
        raise HTTPException(status_code=416, detail="Invalid Range header")
    if object_size is None:
        return None, None, suffix_length
    return max(object_size - suffix_length, 0), None, min(suffix_length, object_size)


def _etag_matches(header_value: str | None, etag: str | None) -> bool:
    if not header_value or not etag:
        return False
    candidates = [item.strip() for item in header_value.split(",")]
    if "*" in candidates:
        return True
    normalized_etag = _normalize_etag(etag)
    return any(_normalize_etag(candidate) == normalized_etag for candidate in candidates)


def _normalize_etag(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    return normalized


def _require_oci_proxy(request: Request):
    settings = request.app.state.settings
    connector = getattr(request.app.state, "storage_connector", None)
    if settings.storage_backend != "oci_zarr" or connector is None:
        raise HTTPException(status_code=400, detail="Zarr proxy is available only when STORAGE_BACKEND=oci_zarr")
    return connector


def _catalog_entry(request: Request, dataset_id: str) -> CatalogEntry:
    catalog = get_or_build_catalog(request.app)
    entry = catalog.get(dataset_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return entry


def _store_variant(entry: CatalogEntry, *, variant: str) -> tuple[str, str, int | None, bool | None]:
    if variant == "source":
        if entry.meta.zarr_proxy_root is None:
            raise HTTPException(status_code=404, detail="Source Zarr proxy is not available")
        return entry.path, entry.meta.zarr_proxy_root, entry.zarr_format, entry.consolidated
    if variant == "multiscale":
        if entry.meta.multiscale_store_path is None or entry.meta.multiscale_proxy_root is None:
            raise HTTPException(status_code=404, detail="Multiscale Zarr proxy is not available")
        return (
            entry.meta.multiscale_store_path,
            entry.meta.multiscale_proxy_root,
            entry.meta.multiscale_zarr_format,
            entry.meta.multiscale_zarr_consolidated,
        )
    raise HTTPException(status_code=400, detail="Unsupported Zarr proxy variant")


async def _proxy_info(dataset_id: str, request: Request, *, variant: str) -> JSONResponse:
    _require_oci_proxy(request)
    entry = _catalog_entry(request, dataset_id)
    store_path, proxy_root, zarr_format, consolidated = _store_variant(entry, variant=variant)
    return JSONResponse(
        {
            "dataset_id": dataset_id,
            "variant": variant,
            "store_path": store_path,
            "zarr_proxy_root": proxy_root,
            "zarr_json_url": f"{proxy_root}/zarr.json",
            "zarr_format": zarr_format,
            "consolidated": consolidated,
            "source_zarr_proxy_root": entry.meta.zarr_proxy_root,
            "multiscale_zarr_proxy_root": entry.meta.multiscale_proxy_root,
        }
    )


async def _proxy_object(dataset_id: str, object_path: str, request: Request, *, variant: str) -> Response:
    connector = _require_oci_proxy(request)
    entry = _catalog_entry(request, dataset_id)
    store_path, _proxy_root, _zarr_format, _consolidated = _store_variant(entry, variant=variant)

    resolved_object_path = _resolve_object_path(store_path, object_path)
    try:
        object_info = await run_in_threadpool(connector.head_object, resolved_object_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Zarr object not found") from exc

    range_start, range_end, expected_length = _parse_range_header(
        request.headers.get("range"),
        object_size=object_info.size,
    )
    status_code = 206 if range_start is not None or request.headers.get("range") is not None else 200
    content_type = _guess_content_type(object_path, object_info.content_type)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "X-Zarr-Variant": variant,
    }
    if object_info.etag:
        headers["ETag"] = object_info.etag

    if request.headers.get("range") is None and _etag_matches(request.headers.get("if-none-match"), object_info.etag):
        return Response(status_code=304, media_type=content_type, headers=headers)

    if range_start is None and expected_length is None and range_end is None:
        try:
            payload = b"" if request.method == "HEAD" else await run_in_threadpool(connector.read_bytes, resolved_object_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Zarr object not found") from exc
    elif range_start is not None and range_end is None and object_info.size is not None and expected_length is not None:
        if request.method == "HEAD":
            payload = b""
        else:
            payload = await run_in_threadpool(
                connector.read_byte_range,
                resolved_object_path,
                start=range_start,
                end=None,
            )
    elif range_start is None and expected_length is not None:
        if request.method == "HEAD":
            payload = b""
        else:
            payload = await run_in_threadpool(
                connector.read_byte_tail,
                resolved_object_path,
                length=expected_length,
            )
        if object_info.size is not None:
            range_start = max(object_info.size - expected_length, 0)
    else:
        if request.method == "HEAD":
            payload = b""
        else:
            payload = await run_in_threadpool(
                connector.read_byte_range,
                resolved_object_path,
                start=range_start,
                end=range_end,
            )

    if expected_length is not None:
        payload_length = expected_length
    elif request.method == "HEAD" and object_info.size is not None:
        payload_length = object_info.size
    else:
        payload_length = len(payload)
    if payload_length >= 0:
        headers["Content-Length"] = str(payload_length)

    if status_code == 206 and object_info.size is not None:
        content_range_start = range_start if range_start is not None else max(object_info.size - payload_length, 0)
        content_range_end = content_range_start + max(payload_length - 1, 0)
        headers["Content-Range"] = f"bytes {content_range_start}-{content_range_end}/{object_info.size}"

    return Response(
        content=b"" if request.method == "HEAD" else payload,
        status_code=status_code,
        media_type=content_type,
        headers=headers,
    )


@router.get("/multiscale/{dataset_id}")
async def get_multiscale_zarr_proxy_info(dataset_id: str, request: Request) -> JSONResponse:
    return await _proxy_info(dataset_id, request, variant="multiscale")


@router.api_route("/multiscale/{dataset_id}/{object_path:path}", methods=["GET", "HEAD"])
async def get_multiscale_zarr_object(dataset_id: str, object_path: str, request: Request) -> Response:
    return await _proxy_object(dataset_id, object_path, request, variant="multiscale")


@router.get("/{dataset_id}")
async def get_zarr_proxy_info(dataset_id: str, request: Request) -> JSONResponse:
    return await _proxy_info(dataset_id, request, variant="source")


@router.api_route("/{dataset_id}/{object_path:path}", methods=["GET", "HEAD"])
async def get_zarr_object(dataset_id: str, object_path: str, request: Request) -> Response:
    return await _proxy_object(dataset_id, object_path, request, variant="source")
