from fastapi import APIRouter

from app.core.colormap import list_colormaps


router = APIRouter(prefix="/colormaps", tags=["colormaps"])


@router.get("")
async def get_colormaps() -> list[str]:
    return list_colormaps()

