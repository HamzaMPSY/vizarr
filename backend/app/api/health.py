import time
from typing import Any

from fastapi import APIRouter
from fastapi import Request

from app.core.oci_auth import OCIAuthExpiredError


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthcheck(request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "ok"}
    diagnostics = getattr(request.app.state, "dataset_manifest_diagnostics", None)
    if diagnostics:
        payload["dataset_manifest"] = diagnostics
    oci_auth = _oci_auth_health(request)
    if oci_auth is not None:
        payload["oci_auth"] = oci_auth
    return payload


def _oci_auth_health(request: Request) -> dict[str, Any] | None:
    settings = request.app.state.settings
    if settings.storage_backend != "oci_zarr":
        return None
    connector = getattr(request.app.state, "storage_connector", None)
    if connector is None:
        return {
            "status": "not_initialized",
            "mode": settings.oci_auth_mode,
        }
    try:
        auth = connector.auth
    except OCIAuthExpiredError as exc:
        return {
            "status": "expired",
            "mode": settings.oci_auth_mode,
            "detail": str(exc),
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "mode": settings.oci_auth_mode,
            "detail": str(exc),
        }

    expires_at = getattr(auth, "token_expires_at_epoch", None)
    if expires_at is None:
        return {
            "status": "ok",
            "mode": auth.auth_mode,
        }
    seconds_remaining = int(expires_at - time.time())
    return {
        "status": "expired" if seconds_remaining <= 0 else "expiring_soon" if seconds_remaining <= 900 else "ok",
        "mode": auth.auth_mode,
        "token_seconds_remaining": max(seconds_remaining, 0),
    }
