# Backend

Python / FastAPI tile server. Reads Zarr arrays from object storage, generates XYZ map tiles on demand, and caches everything aggressively in Redis.

---

## Folder structure

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                   # FastAPI app + lifespan context
│   ├── config.py                 # Pydantic settings, loaded from .env
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py             # Aggregates all sub-routers
│   │   ├── tiles.py              # GET /tiles/{dataset}/{variable}/{z}/{x}/{y}
│   │   ├── datasets.py           # GET /datasets, GET /datasets/{id}
│   │   ├── variables.py          # GET /datasets/{id}/variables
│   │   └── colormaps.py          # GET /colormaps
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── zarr_reader.py        # Zarr store open + spatial slicing
│   │   ├── tile_generator.py     # XYZ → bbox → slice → image
│   │   ├── cache.py              # Redis wrapper (get/set/invalidate)
│   │   ├── colormap.py           # Normalization + LUT application + WebP encode
│   │   └── prefetch.py           # Background tile warming (asyncio tasks)
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── dataset.py            # DatasetMeta, DatasetList Pydantic models
│   │   ├── tile.py               # TileRequest query params
│   │   └── variable.py           # VariableMeta, stats
│   │
│   └── workers/
│       ├── __init__.py
│       └── dask_client.py        # Dask LocalCluster or remote scheduler setup
│
├── tests/
│   ├── test_tiles.py
│   ├── test_zarr_reader.py
│   └── conftest.py               # Fixtures: mock Zarr store, test client
│
├── Dockerfile
├── requirements.txt
├── pyproject.toml
└── .env.example
```

---

## Key modules

### `app/main.py`

Initialises the FastAPI app and manages the application lifespan. On startup it opens the Zarr store once (so the metadata is warm), starts the Dask client, and connects to Redis. On shutdown it closes all connections cleanly.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.workers.dask_client import start_dask
from app.core.zarr_reader import open_store
from app.core.cache import connect_redis
from app.api.router import router

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.zarr = await open_store()
    app.state.dask = await start_dask()
    app.state.redis = await connect_redis()
    yield
    await app.state.redis.aclose()
    app.state.dask.close()

app = FastAPI(lifespan=lifespan)
app.include_router(router, prefix="/api")
```

### `app/config.py`

All settings are read from environment variables (or `.env`) via Pydantic's `BaseSettings`. Nothing is hardcoded.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    zarr_store_url: str           # e.g. s3://bucket/data.zarr
    redis_url: str = "redis://localhost:6379"
    tile_cache_ttl: int = 3600    # seconds
    dask_threads: int = 4
    colormap_default: str = "viridis"
    vmin_percentile: float = 2.0
    vmax_percentile: float = 98.0
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-west-1"

    class Config:
        env_file = ".env"

settings = Settings()
```

### `app/core/zarr_reader.py`

Opens the Zarr store lazily with Xarray and exposes a `slice_region()` function that takes array index bounds and returns a NumPy array. This is the only place that touches the object store.

```python
import xarray as xr
import fsspec
import numpy as np
from app.config import settings

_store: xr.Dataset | None = None

async def open_store() -> xr.Dataset:
    global _store
    fs = fsspec.filesystem(
        settings.zarr_store_url.split("://")[0],
        key=settings.aws_access_key_id,
        secret=settings.aws_secret_access_key,
    )
    mapper = fs.get_mapper(settings.zarr_store_url)
    _store = xr.open_zarr(mapper, chunks="auto", consolidated=True)
    return _store

def slice_region(
    variable: str,
    lat_slice: slice,
    lon_slice: slice,
    time_index: int = 0,
    target_shape: tuple[int, int] = (256, 256),
) -> np.ndarray:
    arr = _store[variable].isel(time=time_index).sel(
        lat=lat_slice, lon=lon_slice
    )
    data = arr.values  # triggers Dask compute
    # resize to tile shape if needed
    from PIL import Image
    img = Image.fromarray(data).resize(target_shape, Image.BILINEAR)
    return np.array(img)
```

### `app/core/tile_generator.py`

The hot path. Converts `(z, x, y)` to a geographic bounding box, maps that to Zarr index slices, and coordinates the read + encode pipeline.

```python
import math
import numpy as np
from app.core.zarr_reader import slice_region
from app.core.colormap import encode_tile

def tile_to_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Returns (west, south, east, north) in EPSG:4326."""
    n = 2 ** z
    west  = x / n * 360 - 180
    east  = (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north

def generate_tile(
    variable: str,
    z: int, x: int, y: int,
    time_index: int,
    colormap: str,
    vmin: float,
    vmax: float,
) -> bytes:
    west, south, east, north = tile_to_bbox(z, x, y)
    lat_slice = slice(south, north)
    lon_slice = slice(west, east)
    data = slice_region(variable, lat_slice, lon_slice, time_index)
    return encode_tile(data, colormap, vmin, vmax)
```

### `app/core/colormap.py`

Normalizes float data and encodes to WebP. Colormaps are loaded once at import time from matplotlib and stored as 256×4 uint8 LUTs for zero-overhead lookup.

```python
import numpy as np
from PIL import Image
import io
import matplotlib.cm as mcm

_luts: dict[str, np.ndarray] = {}

def _load_lut(name: str) -> np.ndarray:
    if name not in _luts:
        cmap = mcm.get_cmap(name)
        _luts[name] = (cmap(np.linspace(0, 1, 256)) * 255).astype(np.uint8)
    return _luts[name]

def encode_tile(
    data: np.ndarray,
    colormap: str,
    vmin: float,
    vmax: float,
    quality: int = 85,
) -> bytes:
    lut = _load_lut(colormap)
    clipped = np.clip(data, vmin, vmax)
    normalized = ((clipped - vmin) / (vmax - vmin) * 255).astype(np.uint8)
    rgba = lut[normalized]  # shape (H, W, 4)
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()
```

### `app/core/cache.py`

A thin async wrapper around `redis.asyncio`. All keys are namespaced and all writes use `SETEX` so nothing accumulates indefinitely.

```python
import hashlib, json
import redis.asyncio as aioredis
from app.config import settings

_redis: aioredis.Redis | None = None

async def connect_redis() -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(settings.redis_url, decode_responses=False)
    return _redis

def _tile_key(dataset, variable, z, x, y, time_index, colormap, vmin, vmax) -> str:
    h = hashlib.sha1(
        json.dumps([dataset, variable, z, x, y, time_index, colormap, vmin, vmax])
        .encode()
    ).hexdigest()[:16]
    return f"tile:{h}"

async def get_tile(key: str) -> bytes | None:
    return await _redis.get(key)

async def set_tile(key: str, data: bytes) -> None:
    await _redis.setex(key, settings.tile_cache_ttl, data)
```

### `app/api/tiles.py`

The tile endpoint. Checks Redis first, generates on miss, writes back to Redis, returns WebP with appropriate cache headers.

```python
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from app.core.cache import _tile_key, get_tile, set_tile
from app.core.tile_generator import generate_tile
from app.models.tile import TileParams

router = APIRouter()

@router.get("/tiles/{dataset}/{variable}/{z}/{x}/{y}")
async def serve_tile(
    request: Request,
    dataset: str,
    variable: str,
    z: int, x: int, y: int,
    params: TileParams = Depends(),
) -> Response:
    key = _tile_key(dataset, variable, z, x, y,
                    params.time_index, params.colormap,
                    params.vmin, params.vmax)
    cached = await get_tile(key)
    if cached:
        return Response(cached, media_type="image/webp",
                        headers={"Cache-Control": "max-age=3600", "X-Cache": "HIT"})

    tile = generate_tile(variable, z, x, y,
                         params.time_index, params.colormap,
                         params.vmin, params.vmax)
    await set_tile(key, tile)
    return Response(tile, media_type="image/webp",
                    headers={"Cache-Control": "max-age=3600", "X-Cache": "MISS"})
```

---

## API reference

| Method | Path | Description |
|---|---|---|
| GET | `/api/tiles/{dataset}/{variable}/{z}/{x}/{y}` | WebP tile image |
| GET | `/api/datasets` | List all available datasets |
| GET | `/api/datasets/{id}` | Dataset metadata (variables, dimensions, CRS) |
| GET | `/api/datasets/{id}/variables` | Variable list with dtype, shape, units |
| GET | `/api/colormaps` | Available colormap names |
| WS | `/ws/datasets` | Push channel for dataset availability events |

### Tile query parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `time_index` | int | 0 | Time step index |
| `colormap` | string | `viridis` | Matplotlib-compatible colormap name |
| `vmin` | float | p2 percentile | Data minimum for colormap normalization |
| `vmax` | float | p98 percentile | Data maximum for colormap normalization |

---

## Dependencies (`requirements.txt`)

```
fastapi>=0.111
uvicorn[standard]>=0.29
gunicorn>=22
pydantic-settings>=2
xarray>=2024.1
zarr>=2.18
dask[distributed]>=2024.1
fsspec>=2024.1
s3fs>=2024.1          # S3 support
gcsfs>=2024.1         # GCS support (optional)
adlfs>=2024.1         # Azure support (optional)
redis[hiredis]>=5
Pillow>=10
matplotlib>=3.8
numpy>=1.26
httpx>=0.27           # For internal test client
pytest>=8
pytest-asyncio>=0.23
```

---

## Environment variables (`.env.example`)

```bash
# Object storage
ZARR_STORE_URL=s3://your-bucket/path/to/data.zarr

# AWS credentials (or use IAM role / workload identity)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=eu-west-1

# GCS (alternative)
# ZARR_STORE_URL=gcs://your-bucket/data.zarr
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json

# Redis
REDIS_URL=redis://localhost:6379
TILE_CACHE_TTL=3600

# Dask
DASK_THREADS=4
# DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786

# Colormap defaults
COLORMAP_DEFAULT=viridis
VMIN_PERCENTILE=2.0
VMAX_PERCENTILE=98.0
```
