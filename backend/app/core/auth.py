from __future__ import annotations

import hashlib
from dataclasses import dataclass
from secrets import compare_digest
from urllib.parse import unquote

from fastapi import HTTPException, Request, WebSocket, status


_PRODUCTION_ENVIRONMENTS = {"prod", "production"}
_PUBLIC_API_PATHS = {"/api/healthz"}
_GLOBAL_ACCESS_PREFIXES = ("/api/storage", "/api/query", "/api/exports")


@dataclass(frozen=True)
class AuthContext:
    token_hint: str
    token_digest: str
    allowed_dataset_ids: frozenset[str] | None

    @property
    def has_global_access(self) -> bool:
        return self.allowed_dataset_ids is None


@dataclass(frozen=True)
class _AuthKey:
    token: str
    allowed_dataset_ids: frozenset[str] | None


def is_auth_enabled(settings) -> bool:
    environment = str(getattr(settings, "app_environment", "development")).strip().lower()
    return bool(getattr(settings, "auth_enabled", False)) or environment in _PRODUCTION_ENVIRONMENTS


def is_public_api_path(path: str) -> bool:
    return path in _PUBLIC_API_PATHS


def authenticate_http_request(request: Request) -> AuthContext | None:
    settings = request.app.state.settings
    if not is_auth_enabled(settings):
        return None
    if is_public_api_path(request.url.path):
        return None

    context = authenticate_token(settings, _extract_http_token(request))
    request.state.auth_context = context
    enforce_path_access(context, request.url.path)
    return context


def authenticate_websocket(websocket: WebSocket) -> AuthContext | None:
    settings = websocket.app.state.settings
    if not is_auth_enabled(settings):
        return None

    context = authenticate_token(settings, _extract_websocket_token(websocket))
    enforce_path_access(context, websocket.url.path)
    websocket.state.auth_context = context
    return context


def authenticate_token(settings, token: str | None) -> AuthContext:
    keys = _parse_auth_keys(getattr(settings, "auth_api_keys", ""))
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is enabled but AUTH_API_KEYS is not configured",
        )
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    for key in keys:
        if compare_digest(token, key.token):
            return AuthContext(
                token_hint=_token_hint(key.token),
                token_digest=_token_digest(key.token),
                allowed_dataset_ids=key.allowed_dataset_ids,
            )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def enforce_path_access(context: AuthContext | None, path: str) -> None:
    if context is None:
        return
    if any(path.startswith(prefix) for prefix in _GLOBAL_ACCESS_PREFIXES) and not context.has_global_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key is not allowed to access global or debug routes",
        )
    dataset_id = dataset_id_from_path(path)
    if dataset_id is not None:
        enforce_dataset_access(context, dataset_id)


def enforce_dataset_access(context: AuthContext | None, dataset_id: str) -> None:
    if context is None or context.allowed_dataset_ids is None:
        return
    if dataset_id not in context.allowed_dataset_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Dataset access denied")


def filter_dataset_ids(context: AuthContext | None, dataset_ids: list[str]) -> set[str] | None:
    if context is None or context.allowed_dataset_ids is None:
        return None
    return set(dataset_ids).intersection(context.allowed_dataset_ids)


def dataset_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3:
        return None
    if parts[0] != "api":
        return None

    route = parts[1]
    if route in {"datasets", "tilejson", "tiles"}:
        return unquote(parts[2])
    if route == "zarr":
        if len(parts) >= 4 and parts[2] == "multiscale":
            return unquote(parts[3])
        return unquote(parts[2])
    return None


def _extract_http_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    query_key = request.query_params.get("api_key")
    return query_key.strip() if query_key else None


def _extract_websocket_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_key = websocket.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    query_key = websocket.query_params.get("api_key")
    return query_key.strip() if query_key else None


def _parse_auth_keys(raw_value: str) -> list[_AuthKey]:
    keys: list[_AuthKey] = []
    for item in raw_value.replace("\n", ",").split(","):
        entry = item.strip()
        if not entry:
            continue
        token, separator, datasets_raw = entry.partition("=")
        token = token.strip()
        if not token:
            continue
        if not separator or datasets_raw.strip() in {"", "*"}:
            keys.append(_AuthKey(token=token, allowed_dataset_ids=None))
            continue
        datasets = frozenset(
            dataset.strip()
            for dataset in datasets_raw.replace("|", ";").split(";")
            if dataset.strip()
        )
        keys.append(_AuthKey(token=token, allowed_dataset_ids=datasets or None))
    return keys


def _token_hint(token: str) -> str:
    return f"...{token[-6:]}" if len(token) > 6 else "configured-key"


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
