import json

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.zarr_reader import open_dataset_from_path, read_store_json


router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/objects")
async def list_storage_objects(
    request: Request,
    prefix: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, object]:
    connector = getattr(request.app.state, "storage_connector", None)
    settings = request.app.state.settings
    if connector is None or settings.storage_backend != "oci_zarr":
        raise HTTPException(
            status_code=400,
            detail="Object storage listing is available only when STORAGE_BACKEND=oci_zarr",
        )

    objects = connector.list_objects(prefix=prefix, limit=limit)
    return {
        "namespace": connector.namespace,
        "bucket": settings.oci_bucket,
        "prefix": prefix if prefix is not None else settings.oci_prefix,
        "objects": [
            {"name": item.name, "size": item.size, "etag": item.etag}
            for item in objects
        ],
    }


@router.get("/prefixes")
async def list_storage_prefixes(
    request: Request,
    prefix: str | None = Query(default=None),
) -> dict[str, object]:
    connector = getattr(request.app.state, "storage_connector", None)
    settings = request.app.state.settings
    if connector is None or settings.storage_backend != "oci_zarr":
        raise HTTPException(
            status_code=400,
            detail="Object storage listing is available only when STORAGE_BACKEND=oci_zarr",
        )

    prefixes = connector.list_prefixes(prefix=prefix)
    return {
        "namespace": connector.namespace,
        "bucket": settings.oci_bucket,
        "prefix": prefix if prefix is not None else settings.oci_prefix,
        "prefixes": prefixes,
    }


@router.get("/zarr-stores")
async def list_zarr_stores(
    request: Request,
    prefix: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> dict[str, object]:
    connector = getattr(request.app.state, "storage_connector", None)
    settings = request.app.state.settings
    if connector is None or settings.storage_backend != "oci_zarr":
        raise HTTPException(
            status_code=400,
            detail="Object storage listing is available only when STORAGE_BACKEND=oci_zarr",
        )

    stores = connector.list_zarr_stores(prefix=prefix, limit=limit)
    return {
        "namespace": connector.namespace,
        "bucket": settings.oci_bucket,
        "prefix": prefix if prefix is not None else settings.oci_prefix,
        "stores": [
            {
                "path": item.path,
                "consolidated": item.consolidated,
                "zarr_format": item.zarr_format,
            }
            for item in stores
        ],
    }


@router.get("/inspect-zarr")
async def inspect_zarr_store(
    request: Request,
    zarr_path: str = Query(...),
    consolidated: bool = Query(default=True),
) -> dict[str, object]:
    connector = getattr(request.app.state, "storage_connector", None)
    settings = request.app.state.settings
    if connector is None or settings.storage_backend != "oci_zarr":
        raise HTTPException(
            status_code=400,
            detail="Object storage listing is available only when STORAGE_BACKEND=oci_zarr",
        )

    if zarr_path.startswith("oci://"):
        resolved_path = zarr_path
    else:
        resolved_path = connector.build_oci_uri(zarr_path)

    try:
        dataset = open_dataset_from_path(
            connector=connector,
            zarr_path=resolved_path,
            consolidated=consolidated,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to open Zarr store: {exc}") from exc

    variables = []
    for name, data_array in dataset.data_vars.items():
        variables.append(
            {
                "name": name,
                "dims": list(data_array.dims),
                "shape": [int(data_array.sizes[dim]) for dim in data_array.dims],
                "dtype": str(data_array.dtype),
                "attrs": {key: str(value) for key, value in data_array.attrs.items()},
            }
        )

    coords = []
    for name, coord in dataset.coords.items():
        coords.append(
            {
                "name": name,
                "dims": list(coord.dims),
                "shape": [int(coord.sizes[dim]) for dim in coord.dims],
                "dtype": str(coord.dtype),
            }
        )

    return {
        "zarr_path": zarr_path,
        "resolved_path": resolved_path,
        "consolidated": consolidated,
        "attrs": {key: str(value) for key, value in dataset.attrs.items()},
        "variables": variables,
        "coords": coords,
    }


@router.get("/zarr-json")
async def get_zarr_json(
    request: Request,
    zarr_path: str = Query(...),
) -> dict[str, object]:
    connector = getattr(request.app.state, "storage_connector", None)
    settings = request.app.state.settings
    if connector is None or settings.storage_backend != "oci_zarr":
        raise HTTPException(
            status_code=400,
            detail="Object storage listing is available only when STORAGE_BACKEND=oci_zarr",
        )

    object_path = zarr_path.rstrip("/") + "/zarr.json"
    try:
        raw = read_store_json(connector=connector, object_path=connector.build_oci_uri(object_path))
        return {
            "zarr_path": zarr_path,
            "object_path": object_path,
            "metadata": json.loads(raw),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read zarr.json: {exc}") from exc
