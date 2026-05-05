# Performance

This document distinguishes implemented performance behavior from planned work.

## Implemented caching layers

| Layer | Location | Status | Hit condition |
|---|---|---|---|
| Map renderer state | Browser/GPU | Implemented | MapLibre has already loaded and retained the raster tile |
| TanStack Query | Browser memory | Implemented | Dataset, variable, colormap, TileJSON, or serving-profile query key is still cached |
| Browser HTTP cache | Browser | Implemented | Tile or Zarr proxy response has `Cache-Control: public, max-age=3600` |
| Redis tile cache | Backend/Redis | Implemented | Tile cache key exists for the exact dataset, variable/style, coordinate, display range, render mode, composite bands, planner representation, and planner version |
| Browse artifacts | OCI/local cache path | Implemented | Planner chooses `browse` and the overview artifact exists |
| Multiscale artifacts | OCI | Partly implemented | Planner chooses `pyramid` and the generated tile exists or can be generated/cache-filled |
| Direct source serving | OCI source Zarr | Implemented | Cache/artifact miss falls back to source Zarr reads |
| Nginx disk tile cache | Reverse proxy | Implemented | Request reaches `/api/tiles/` through production-style Nginx and the disk cache has a matching successful tile response |
| Debounced adjacent-tile prefetch | Browser | Implemented | Debounced viewport state computes current XYZ plus a radius-2 surrounding ring |

## Tile cache keys

Rendered WebP tiles are cached in Redis through `app.core.cache`. The key
includes every parameter that changes tile output:

- dataset id;
- variable/band;
- z/x/y;
- time index;
- colormap;
- vmin/vmax;
- selected representation;
- render mode;
- composite band ids when rendering RGB styles;
- planner version.

This prevents stale bytes from one representation or style from being reused for
another.

## Response headers

Tile responses include:

- `Cache-Control: public, max-age=3600`;
- `X-Cache-Status`;
- `X-Data-Vmin`;
- `X-Data-Vmax`;
- `X-Request-Class`;
- `X-Execution-Path`;
- `X-Representation`;
- optional `X-Browse-Source`.

These headers let the frontend and operators distinguish browser/Redis cache
behavior from browse, pyramid, or direct serving paths.

## Browse overviews

Browse overviews are the current first-view performance lever. They avoid
expensive direct reads for low and mid zooms by serving prebuilt overview data.

Current behavior:

- startup can prewarm catalog/browse state when enabled;
- browse artifacts can be generated ahead of time with backend tools;
- the tile route attempts browse serving when the planner selects `browse`;
- missing browse files fall back to direct `serving`.

Browse coverage is dataset-specific. A dataset can be valid and still have slow
first-view performance if its browse artifacts are missing.

## Multiscale artifacts

The multiscale path treats the browser-facing pyramid as a separate store from
the source Zarr. This avoids mutating source datasets and lets the service expose
a dedicated proxy root:

- `/api/zarr/multiscale/{dataset_id}`
- `/api/zarr/multiscale/{dataset_id}/{object_path}`

The backend can serve generated pyramid tiles and can lazily populate some
missing tiles. The frontend helper for browser-side multiscale reads is present,
but MapLibre raster TileJSON is still the active viewer path.

## Direct source serving

Direct source serving is the fallback path for high zooms or missing artifacts.
For OCI projected imagery, the backend reads source metadata/chunks through the
OCI connector and Zarr helpers, renders a selected variable/band, applies the
colormap, and encodes WebP.

This path is correct but can be much slower than browse/pyramid serving because a
single visual tile may touch multiple source chunks or require reprojection.

## Zarr layout guidance

Chunking remains critical. For lat/lon scalar datasets, aim for one time step and
one tile-sized spatial chunk, such as `[1, 256, 256]`.

For current projected multiband imagery:

- source stores may use larger outer shard layouts;
- inner chunks close to `256x256` are preferred;
- browse overviews or generated multiscale stores should carry low/mid zoom
  interaction performance;
- direct serving should be treated as a fallback or high-zoom path.

Consolidated metadata is preferred because it reduces store-open request count.
The implementation supports current Zarr v3 inline consolidated metadata and
Zarr v2 consolidated multiscale stores where generated.

## WebP encoding

Tiles are returned as WebP. For continuous visual data, WebP generally keeps tile
payloads smaller than PNG while preserving enough visual quality for map
navigation.

If exact pixel readback becomes a product requirement, add a separate lossless
path rather than changing the default map tile response globally.

## Zarr proxy byte ranges

The Zarr proxy supports byte-range requests for source and multiscale objects.
This is required for browser/native readers and efficient access to chunks or
shards. Proxy responses include:

- `Accept-Ranges: bytes`;
- `Content-Range` for partial responses;
- `ETag` when available;
- `Cache-Control: public, max-age=3600`.

The proxy rejects unsafe object paths and only serves objects through known
dataset catalog entries.

## Debounced prefetch and range seeding

The frontend debounces viewport state before doing any speculative work. After
the map settles, it computes the current tile coordinate and a radius-2
surrounding ring, checks the browser Cache API, and fetches missing URLs using
the same TileJSON template that MapLibre uses for visible tiles.

The center tile response is also used to seed `vmin` and `vmax` from
`X-Data-Vmin` and `X-Data-Vmax` when the user has not already selected an
explicit range. This keeps MapLibre as the visible tile loader while still
warming nearby tiles and avoiding a separate metadata request for the first
display range.

## Nginx tile cache

`nginx/nginx.conf` defines a `tile_cache` zone under `/var/cache/nginx/tiles`
and a dedicated `/api/tiles/` location. It caches successful tile responses for
one hour, uses cache locking to reduce duplicate backend renders on cold misses,
and can serve stale tile bytes during backend timeout/error/update states.

Through Nginx, `X-Cache-Status` reports the Nginx disk cache status. Direct
backend tile requests still use `X-Cache-Status` for the backend Redis cache.

## WebSocket invalidation

`/ws/datasets` sends dataset invalidation snapshots. The frontend subscribes to
that route, invalidates TanStack Query dataset-related roots, and reconnects
after disconnects. Vite and Nginx both proxy `/ws` with WebSocket upgrade
support.

## Planned work

### External Dask

Dask is not started by the current FastAPI lifespan. Do not assume a scheduler or
worker pool exists in production until code and deployment configuration are
added.
