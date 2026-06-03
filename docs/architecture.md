# Architecture

## Current system

Vizarr is a tile-based satellite Zarr viewer. The checked-in implementation now
has two backend storage modes:

- `synthetic`: in-memory demo data for local UI and backend regression checks.
- `oci_zarr`: OCI Object Storage discovery, catalog construction, Zarr metadata
  inspection, server-rendered tiles, and dataset-scoped Zarr proxying.

The OCI path is the primary implementation target. Generic S3, GCS, Azure, Dask
clusters, and predictive prefetch remain architectural ideas unless a ticket
explicitly implements them.

```
OCI Object Storage
  |
  | OCI SDK listing, ocifs/fsspec reads, direct byte ranges
  v
FastAPI backend
  - catalog and manifest builder
  - planner-selected tile representation
  - browse/pyramid/serving tile paths
  - Redis tile cache
  - read-only Zarr proxy
  - browser-facing multiscale sidecars
  - dataset invalidation WebSocket
  |
  | REST / TileJSON / WebP tiles / read-only Zarr proxy / WebSocket invalidation
  v
React frontend
  - dataset and variable selection
  - MapLibre raster source/layer
  - optional browser-native multiscale image source
  - optional deck.gl browser-GPU raster overlay
  - TanStack Query metadata cache
  - Zustand map/UI state
```

## Data representations

### Source Zarr stores

OCI datasets are discovered under `OCI_BUCKET` and `OCI_PREFIX`. A prefix can
contain many stores; the prefix itself must not be treated as a Zarr root unless
it contains store metadata.

The current adapter is tuned toward projected multiband Zarr v3 stores with a
main `bands` array and dimensions like `time`, `band`, `y`, and `x`. Generic
projected layouts are not complete yet.

### Browse artifacts

Browse overviews are durable, low/mid zoom artifacts used for fast initial map
navigation. When a browse artifact exists for the requested dataset, variable,
time, and zoom, the planner can route tile requests through the `browse` path.

### Multiscale artifacts

Generated multiscale stores live separately from source stores, usually under
`OCI_MULTISCALE_PREFIX_ROOT`. They are exposed at:

- `/api/zarr/multiscale/{dataset_id}`
- `/api/zarr/multiscale/{dataset_id}/{object_path}`

The backend can serve prebuilt pyramid tiles and can lazily generate/cache some
pyramid tiles. The frontend contains helper code for browser-native multiscale
reading and a first deck.gl overlay path for compatible generated sidecars.
MapLibre raster tiles from the backend TileJSON endpoint remain the fallback.

The browser-GPU path also uses these generated sidecars. It must not read
arbitrary source Zarr v3/sharded cubes directly from the browser. The initial
GPU-compatible sidecar contract is:

- Zarr v2 with consolidated metadata;
- one data array per level with dimensions `time`, `band`, `y`, and `x`;
- `float32`, C-order chunks with no compressor and no filters;
- chunks `[1, 1, 256, 256]`;
- level metadata containing stable level paths, WGS84 or Web Mercator bounds,
  browse zoom mapping, data array name, CRS/transform metadata where available,
  and enough shape/chunk/dtype fields for a frontend eligibility decision.

When any part of that contract is missing, the frontend must stay on the
server-rendered TileJSON path.

### Direct serving

When no browse or pyramid artifact is available, the backend reads source Zarr
metadata/chunks and renders the requested band tile directly. This is the
fallback for high zooms and unsupported artifact states.

Direct serving is in-process today. It can use bounded thread-level source chunk
parallelism, but the FastAPI lifespan does not start an external Dask scheduler
or worker cluster. Per-request direct tile object/chunk/byte budgets are the
runtime guardrail for missing artifacts or unexpectedly expensive source
windows. OCI object bytes and decoded Zarr shard indexes are cached with bounded
LRUs so repeated chunks in the same shard avoid repeated index reads and decode
work.

## Backend components

### FastAPI app and lifespan

`backend/app/main.py` creates the app, installs CORS, registers `/api` routes,
and initializes runtime state:

- settings from `backend/app/config.py`;
- planner and planner index;
- export job store;
- storage connector for `oci_zarr`;
- dataset registry or OCI catalog;
- Redis cache client.

In `oci_zarr` mode, startup can warm the catalog and browse overviews when the
settings allow it. In synthetic mode, it builds a local demo registry.

### Catalog and discovery

`backend/app/core/dataset_catalog.py` and
`backend/app/core/oci_object_storage.py` list OCI objects, identify Zarr stores,
hydrate metadata, and build dataset records. Unreadable stores are skipped during
catalog construction so one bad store does not take down the API.

### Planner

`backend/app/services/planner.py` classifies tile and query requests. Tile
responses expose planner decisions through headers:

- `X-Request-Class`
- `X-Execution-Path`
- `X-Representation`

The tile route currently chooses among `browse`, `pyramid`, and `serving`.

### Tile generation

`backend/app/api/tiles.py` is the hot path:

1. validate dataset, variable, tile coordinate, and display parameters;
2. plan the request;
3. check Redis by a key containing every tile-affecting parameter;
4. render through browse, pyramid, or direct source serving;
5. return WebP bytes with cache and display-range headers.

Synthetic datasets use `core/tile_generator.py`. OCI projected imagery uses
`core/projected_tile_generator.py`, browse helpers, and multiscale helpers.

### Zarr proxy

`backend/app/api/zarr.py` provides read-only, dataset-scoped proxy routes for
source and multiscale stores. It supports `GET`, `HEAD`, `Range`, ETag, content
type detection, and path traversal protection.

These routes are OCI-only and do not expose raw credentials or raw OCI URLs to
the frontend.

### Cache

Redis stores rendered tile bytes with TTL. Browser HTTP caching is enabled by
`Cache-Control: public, max-age=3600` on tile and Zarr object responses.

Metadata caching exists in process through app state and TanStack Query on the
frontend. `/ws/datasets` sends dataset invalidation snapshots so the frontend
can refresh dataset, variable, serving-profile, and TileJSON queries when the
catalog changes.

## Frontend components

The current frontend is a React/Vite app using:

- MapLibre raster `Source`/`Layer` for TileJSON-backed map rendering;
- TanStack Query for datasets, variables, colormaps, TileJSON, and serving
  profiles;
- Zustand for active dataset, variable, time index, colormap, and map state.

Tile URLs are centralized in `frontend/src/api/endpoints.ts`. The active viewer
requests:

- `/api/datasets`
- `/api/datasets/{dataset_id}/variables`
- `/api/datasets/{dataset_id}/serving-profile`
- `/api/tilejson/{dataset_id}/{variable}`
- `/api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`

Browser-native multiscale code in `frontend/src/lib/multiscale.ts` is wired as
an opportunistic MapLibre image-source path when the serving profile and level
metadata are compatible. Server-rendered TileJSON remains the fallback and the
normal path for unsupported datasets.

The deck.gl browser-GPU path sits beside that image-source path. The current
slice reuses the dataset-scoped multiscale proxy, reads a bounded browser
window, uploads raw float values and a palette texture through
`ZarrColormapBitmapLayer`, and applies range normalization plus colormap lookup
in a fragment shader. The CPU-colored data URL is kept for the MapLibre
image-source fallback. It is an optimization path only: `browser-gpu` may be
selected for compatible sidecars, while `browser-native` and `server-tiles`
remain fallback states for existing behavior and unsupported data.

## Request flow: active tile path

```
User selects dataset/variable or pans map
  |
  v
Frontend requests TileJSON for dataset + variable
  |
  v
MapLibre requests /api/tiles/{dataset}/{variable}/{z}/{x}/{y}
  |
  v
FastAPI planner selects browse, pyramid, or serving
  |
  v
Redis HIT -> return cached WebP
  |
  v
Redis MISS -> render tile from browse artifact, multiscale artifact, or source Zarr
  |
  v
FastAPI returns WebP + cache/planner/range headers
```

## Deployment topology

Production-style compose:

```
Browser
  |
  v
Nginx on host port ${APP_PORT:-8000}
  - /api/tiles/ -> backend:8000 with Nginx disk cache
  - /api/ -> backend:8000
  - /ws/  -> backend:8000 with WebSocket upgrade headers
  - /     -> frontend:80

Backend
  - internal-only in production-style compose
  - Redis on the internal compose network
  - OCI access through configured profile/resource auth
```

Development compose:

```
Browser -> Vite dev server on ${APP_PORT:-5173}
Vite /api proxy -> backend:8000
Vite /ws proxy -> backend:8000
Host direct API -> http://localhost:${API_PORT:-8001}/api
```

Nginx reports tile disk-cache state with `X-Cache-Status`. Direct backend tile
responses still use `X-Cache-Status` for the backend Redis cache.

## Implemented vs planned

| Capability | Status |
|---|---|
| Synthetic demo dataset | Implemented |
| OCI session-profile auth and object listing | Implemented |
| Zarr v3 metadata/chunk/shard handling | Implemented for current target layouts |
| Server-rendered WebP tiles | Implemented |
| Direct tile compute/read budgets | Implemented |
| Browse overview serving | Implemented |
| Separate multiscale store discovery/proxying | Implemented |
| MapLibre raster TileJSON viewer | Implemented |
| Deck.gl MapLibre overlay shell | Implemented |
| Redis tile cache | Implemented |
| Browser-native multiscale attempt with server-tile fallback | Implemented |
| Deck.gl browser-GPU Zarr rendering | Partly implemented with raw-float single-band and composite deck.gl layers |
| RGB and false-color composites | Implemented for recognized band aliases |
| Generic projected Zarr layout adapter | Partly implemented for direct `time/y/x` and banded `time/*/y/x` layouts |
| Debounced frontend tile prefetch | Implemented without a worker |
| WebSocket dataset invalidation | Implemented |
| External Dask scheduler | Not implemented |
| Nginx disk tile cache | Implemented |
