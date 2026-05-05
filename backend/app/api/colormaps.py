from fastapi import APIRouter, HTTPException, Query

from app.core.colormap import list_colormaps
from app.core.colormap import sample_colormap_palette


router = APIRouter(prefix="/colormaps", tags=["colormaps"])


@router.get("")
async def get_colormaps() -> list[str]:
    return list_colormaps()


@router.get("/{name}/palette")
async def get_colormap_palette(
    name: str,
    samples: int = Query(default=256, ge=2, le=1024),
) -> list[list[int]]:
    if name not in list_colormaps():
        raise HTTPException(status_code=404, detail="Colormap not found")
    return sample_colormap_palette(name, samples=samples)
