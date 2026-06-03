import json
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.auth import authenticate_websocket
from app.core.auth import filter_dataset_ids
from app.models.dataset import DatasetMeta


router = APIRouter()


def build_dataset_invalidation_event(app_state: Any, *, allowed_dataset_ids: set[str] | None = None) -> dict[str, Any]:
    manifest = getattr(app_state, "dataset_manifest", None)
    if manifest is None:
        registry = getattr(app_state, "registry", None)
        manifest = [registry.meta] if registry is not None else []

    datasets: list[dict[str, str]] = []
    for item in manifest:
        dataset = item if isinstance(item, DatasetMeta) else DatasetMeta.model_validate(item)
        if allowed_dataset_ids is not None and dataset.id not in allowed_dataset_ids:
            continue
        datasets.append({"id": dataset.id, "name": dataset.name})

    return {
        "type": "datasets.invalidate",
        "version": datetime.now(UTC).isoformat(),
        "datasets": datasets,
    }


@router.websocket("/ws/datasets")
async def dataset_events(websocket: WebSocket) -> None:
    try:
        auth_context = authenticate_websocket(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return
    manifest = getattr(websocket.app.state, "dataset_manifest", None)
    if manifest is None:
        registry = getattr(websocket.app.state, "registry", None)
        manifest = [registry.meta] if registry is not None else []
    allowed_dataset_ids = filter_dataset_ids(auth_context, [DatasetMeta.model_validate(item).id for item in manifest])
    await websocket.accept()
    await websocket.send_json(
        build_dataset_invalidation_event(websocket.app.state, allowed_dataset_ids=allowed_dataset_ids)
    )
    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                message = {"type": raw_message}

            message_type = message.get("type")
            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif message_type == "refresh":
                await websocket.send_json(
                    build_dataset_invalidation_event(websocket.app.state, allowed_dataset_ids=allowed_dataset_ids)
                )
    except WebSocketDisconnect:
        return
