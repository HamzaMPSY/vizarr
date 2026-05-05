import json
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.models.dataset import DatasetMeta


router = APIRouter()


def build_dataset_invalidation_event(app_state: Any) -> dict[str, Any]:
    manifest = getattr(app_state, "dataset_manifest", None)
    if manifest is None:
        registry = getattr(app_state, "registry", None)
        manifest = [registry.meta] if registry is not None else []

    datasets: list[dict[str, str]] = []
    for item in manifest:
        dataset = item if isinstance(item, DatasetMeta) else DatasetMeta.model_validate(item)
        datasets.append({"id": dataset.id, "name": dataset.name})

    return {
        "type": "datasets.invalidate",
        "version": datetime.now(UTC).isoformat(),
        "datasets": datasets,
    }


@router.websocket("/ws/datasets")
async def dataset_events(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json(build_dataset_invalidation_event(websocket.app.state))
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
                await websocket.send_json(build_dataset_invalidation_event(websocket.app.state))
    except WebSocketDisconnect:
        return
