# Architecture

## Overview

The system is divided into three layers: object storage, a Python backend that acts as a tile server, and a React frontend that renders tiles on a WebGL map. The key insight is that satellite data in Zarr is already chunked on disk — the backend's job is to map XYZ map tile coordinates to the right Zarr chunks, slice them, apply a colormap, and return a WebP image. The frontend treats that image exactly like any other map tile.

```
┌─────────────────────┐     fsspec      ┌────────────────────────────────────────┐     REST/WS     ┌──────────────────────────────┐
│                     │ ──────────────► │                                        │ ──────────────► │                              │
│    Object storage   │                 │          Python backend                │                 │       React frontend          │
│                     │                 │                                        │ ◄────────────── │                              │
│  s3://bucket/       │                 │  FastAPI router                        │    requests     │  Deck.gl + MapLibre           │
│    data.zarr/       │                 │  Tile engine                           │                 │  TanStack Query               │
│      temperature/   │                 │  Zarr + Dask reader                    │                 │  Zustand store                │
│      precipitation/ │                 │  Redis cache                           │                 │  Prefetch worker              │
│      wind/          │                 │  Colormap engine                       │                 │                              │
│      .zattrs        │                 │                                        │                 │                              │
└─────────────────────┘                 └────────────────────────────────────────┘                 └──────────────────────────────┘
```

---

## Components

### Object storage

The Zarr store lives directly in object storage. No data is copied or pre-processed beyond the initial write. The backend reads chunks on demand via `fsspec`, which abstracts S3, GCS, and Azure behind a common interface. The only requirement is that the Zarr arrays use a chunk layout that aligns well with tile boundaries — see [performance.md](performance.md#zarr-chunking) for the recommended encoding.

### FastAPI router

The entry point for all client requests. Handles three categories of traffic:

- **REST endpoints** — dataset discovery, variable metadata, colormap listing. These are served from Redis or computed once and cached.
- **Tile endpoints** — `GET /tiles/{dataset}/{variable}/{z}/{x}/{y}` — the hot path. Every parameter is validated via Pydantic, the cache is checked, and the tile engine is called only on a miss.
- **WebSocket** — `WS /ws/datasets` — pushes dataset availability events to connected clients so they can invalidate their caches without polling.

### Tile engine

The core of the backend. For a given `(z, x, y)` tile coordinate, it:

1. Converts the tile to a geographic bounding box (EPSG:4326).
2. Translates that bounding box to Zarr array index slices using the coordinate arrays.
3. Calls the Zarr + Dask reader to fetch only those chunks.
4. Passes the resulting NumPy array to the colormap engine.
5. Returns a WebP-encoded image.

The tile engine is stateless — all state (open Zarr stores, colormap definitions) is held in the application lifespan context.

### Zarr + Dask reader

Opens the Zarr store lazily via `xarray.open_zarr()` with `chunks="auto"` so Dask takes over chunk scheduling. When the tile engine calls `.isel()` or `.sel()` to slice a spatial region, Dask computes only the required chunks in parallel. On a 4-core machine this typically means 2–4 simultaneous HTTP range requests to the object store per tile.

The reader maintains a warm connection pool through `fsspec`'s built-in caching (`BlockCache`), which keeps recently read byte ranges in memory. This is particularly valuable for tiles at the same zoom level in the same region, where adjacent tiles share chunk boundaries.

### Redis cache

A three-tier cache keyed by a hash of all parameters that affect the tile output:

```
tile:{dataset}:{variable}:{z}:{x}:{y}:{time_index}:{colormap}:{vmin}:{vmax}
```

- **L1** — tile bytes (`SETEX`, default TTL 3600s)
- **L2** — dataset metadata (`SET` with no expiry, invalidated on WebSocket push)
- **L3** — variable statistics (p2/p98 percentile range, computed once per variable per time step)

A Redis hit short-circuits the entire tile engine — the response is served in under 1ms from memory.

### Colormap engine

Takes a 2D NumPy float32 array and returns a uint8 RGBA image. The pipeline:

1. Clip values to `[vmin, vmax]` (defaults to p2/p98 percentile range).
2. Normalize to `[0, 1]`.
3. Apply a named colormap (matplotlib-compatible, stored as 256×4 RGBA LUT).
4. Encode to WebP with quality 85 via Pillow.

Colormaps are loaded from disk at startup and cached in memory. The client can override the colormap and range per request via query parameters.

### Deck.gl + MapLibre (frontend)

`DeckGL` hosts a `TileLayer` whose `getTileData` function constructs the tile URL from the current dataset, variable, time step, and colormap from the Zustand store, then fetches it as a bitmap. MapLibre GL renders the base map underneath. The two are composed via Deck.gl's `MapboxMap` interop — they share the same viewport and event loop.

Tile transitions use Deck.gl's built-in fade (`transitions: { opacity: 300 }`), so stale tiles linger at reduced opacity while the fresh tile loads, eliminating the blank-flash that plagues most tile viewers.

### TanStack Query

Manages all server state. Key query configurations:

- `staleTime: 30_000` for dataset metadata — considered fresh for 30 seconds, then refetched silently in the background.
- `staleTime: Infinity` for colormap definitions — never changes without a deploy.
- `gcTime: 300_000` for tile prefetch queries — keeps tile data in the in-memory cache for 5 minutes after it leaves the viewport.

Variable statistics queries are seeded on first tile load (the backend embeds them in the tile response headers) so there is never a separate round trip to discover the data range.

### Zustand store

Two stores:

- `mapStore` — viewport bounding box, active dataset ID, active variable, active time index, colormap name, vmin/vmax override. Anything that changes what tiles are fetched.
- `uiStore` — sidebar open/closed, filter panel state, loading indicators. UI concerns that should not trigger tile refetches.

### Prefetch worker

A Web Worker (`prefetch.worker.ts`) that receives the current viewport and zoom level via `postMessage` whenever the map moves. It computes the surrounding 5×5 tile grid (2 tiles in each direction from the visible tiles), filters out tiles already in the browser cache via the Cache API, and fires low-priority `fetch()` requests for the missing ones. By the time the user's pan animation ends, the next viewport is already cached.

---

## Data flow: a single tile request

```
User pans map
  │
  ▼
TileLayer computes new (z, x, y) coordinates
  │
  ▼
getTileData() builds URL: /tiles/{dataset}/{var}/{z}/{x}/{y}?time=...&colormap=...
  │
  ▼
Browser checks HTTP cache (Cache-Control: max-age=3600)
  ├── HIT → render immediately
  └── MISS ──►
              FastAPI validates request (Pydantic)
                │
                ▼
              Redis HGET tile:{hash}
                ├── HIT → return WebP bytes (< 1ms)
                └── MISS ──►
                            Tile engine: XYZ → bbox → array slices
                              │
                              ▼
                            Zarr + Dask: fetch chunks from object store
                              │
                              ▼
                            Colormap engine: normalize → WebP
                              │
                              ▼
                            Redis SETEX tile:{hash} (cache for next request)
                              │
                              ▼
                            Return WebP + headers (Cache-Control, ETag)
                              │
                              ▼
                            Deck.gl fades in the new tile
```

---

## Deployment topology

```
Internet
  │
  ▼
Nginx (port 80/443)
  ├── /api/*  → FastAPI (Gunicorn + Uvicorn workers, port 8000)
  ├── /ws     → FastAPI WebSocket
  └── /*      → Vite build (static files)

FastAPI
  └── Redis (port 6379, internal network only)
  └── Object store (via fsspec over HTTPS)

Optional: Dask scheduler + workers (for heavy parallel loads)
```

For production, run at least 4 Uvicorn workers (`--workers 4`) so concurrent tile requests are handled in parallel. The tile engine is CPU-bound during colormap encoding, so worker count should match available cores.
