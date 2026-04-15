from fastapi import APIRouter

from app.api.colormaps import router as colormaps_router
from app.api.datasets import router as datasets_router
from app.api.exports import router as exports_router
from app.api.health import router as health_router
from app.api.query import router as query_router
from app.api.storage import router as storage_router
from app.api.tiles import router as tiles_router


router = APIRouter()
router.include_router(health_router)
router.include_router(datasets_router)
router.include_router(colormaps_router)
router.include_router(query_router)
router.include_router(exports_router)
router.include_router(storage_router)
router.include_router(tiles_router)
