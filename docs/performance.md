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

Display range values are normalized before key hashing. By default,
`TILE_CACHE_DISPLAY_RANGE_DECIMALS=3`, so tiny slider jitter does not create a
new Redis key for every pointer movement. Set the value higher when exact visual
range separation matters more than cache hit rate.

Set `TILE_CACHE_CUSTOM_RANGE_ENABLED=false` to skip backend tile caching for
requests with explicit `vmin` or `vmax`. This is useful for deployments where
interactive range exploration creates too many one-off cache entries. Default
dataset/display ranges still cache normally.

Production Redis should be configured with a bounded `maxmemory` and an
eviction policy such as `allkeys-lru` or `allkeys-lfu`. Nginx remains a
coarser HTTP response cache; Redis remains the backend byte cache for exact or
normalized render keys.

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

When `TILE_DEBUG_HEADERS_ENABLED=true`, tile responses also include sanitized
per-request diagnostics:

- `X-Tile-Time-Ms`;
- `X-Tile-Planner-Ms`;
- `X-Tile-Cache-Lookup-Ms`;
- `X-Tile-Catalog-Ms`;
- `X-Tile-Render-Ms`;
- `X-Tile-Encode-Ms`;
- `X-Object-Get-Count`;
- `X-Object-Byte-Range-Get-Count`;
- `X-Object-Bytes-Read`;
- `X-Zarr-Shard-Index-Reads`;
- `X-Zarr-Chunk-Count`;
- `X-Tile-Budget-Status`;
- optional `X-Tile-Budget-Reason`, `X-Tile-Budget-Metric`,
  `X-Tile-Budget-Limit`, and `X-Tile-Budget-Actual` when a direct tile budget
  is evaluated or exceeded.

The backend always emits the same aggregate metrics to structured
`tile_request_metrics` logs. The payload is limited to dataset/variable ids,
XYZ coordinates, representation/cache state, timings, and aggregate counts; it
does not include OCI signed URLs, tokens, namespace credentials, or object
contents.

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
missing tiles. The frontend helper for browser-side multiscale reads can render
eligible uncompressed Zarr v2 levels directly, but MapLibre raster TileJSON
remains the fallback for unsupported stores, oversized windows, and failed
chunks.

Browser-native multiscale reads have explicit frontend budgets:

- `1,048,576` output pixels;
- `64` chunks;
- `16 MiB` estimated chunk bytes;
- `4` concurrent chunk fetches.

Small selected levels load as one image source. Larger selected levels are
clipped to the current viewport and only intersecting chunks are fetched. If the
viewport window still exceeds budget, the viewer stays on server-rendered tiles.
Stale metadata and chunk requests consume TanStack Query's `AbortSignal`, so
new map moves or dataset/control changes cancel superseded browser reads. The
active rendering mode and budget counters are exposed on `.map-shell` data
attributes for browser automation.

The browser-GPU path keeps the same safety boundary. The current implemented
slice renders browser-native prepared planes through deck.gl layers when the
serving profile reports `browser_gpu_ready`. Single-band rendering uses
`ZarrColormapBitmapLayer` with one raw `r32float` texture plus a palette
texture. Composite rendering uses `ZarrCompositeBitmapLayer` with three raw
`r32float` band textures. Both paths perform range normalization and nodata
alpha in fragment shaders while preserving the server tile fallback. The GPU
slice refuses texture uploads above the advertised debug guardrail
(`data-browser-gpu-max-texture-dimension`, currently `4096`) and falls back to
server tiles instead of leaving an empty overlay. Runtime deck.gl render errors
are counted per active raster attempt; once
`data-browser-gpu-failure-fallback-threshold` is reached, the overlay is cleared
and the viewer fails closed to server tiles while exposing
`data-browser-gpu-failure-count` and `data-browser-gpu-last-error`.

- use only generated multiscale sidecars exposed through the dataset-scoped
  proxy;
- load only the chunks or tile windows intersecting the visible map area;
- upload single-band or RGB/false-color values to GPU textures;
- apply display range, palette lookup, and RGB/false-color band mapping in
  shaders;
- fall back to server tiles when metadata, layout, request, or budget checks
  fail.

The initial sidecar profile for GPU rendering is the same as the strict
browser-native read profile: consolidated Zarr v2, `float32`, C-order, no
compressor/filters, dimensions `time/band/y/x`, and `[1, 1, 256, 256]` chunks.
This intentionally favors predictable browser reads and debuggable performance
over broad Zarr compatibility.

## Direct source serving

Direct source serving is the fallback path for high zooms or missing artifacts.
For OCI projected imagery, the backend reads source metadata/chunks through the
OCI connector and Zarr helpers, renders a selected variable/band, applies the
colormap, and encodes WebP.

This path is correct but can be much slower than browse/pyramid serving because a
single visual tile may touch multiple source chunks or require reprojection.

Current compute strategy is intentionally in-process:

- FastAPI does not start a Dask scheduler or worker cluster.
- Source Zarr chunk reads can run concurrently inside the request path, bounded
  by `DIRECT_TILE_MAX_PARALLEL_CHUNK_READS` (`8` by default).
- OCI full-object, byte-range, and tail reads use a bounded in-process LRU cache
  controlled by `OCI_BYTES_CACHE_MAX_ENTRIES` and
  `OCI_BYTES_CACHE_MAX_BYTES`.
- Decoded Zarr v3 shard indexes use a separate bounded LRU cache controlled by
  `ZARR_SHARD_INDEX_CACHE_ENTRIES` and `ZARR_SHARD_INDEX_CACHE_BYTES`, so
  multiple chunks in the same shard do not repeatedly read and decode the same
  index.
- Browse and prebuilt pyramid responses should be the normal low/mid zoom path.
- Direct source serving must not run unbounded. Set positive
  `DIRECT_TILE_MAX_OBJECT_GETS`, `DIRECT_TILE_MAX_BYTE_RANGE_GETS`,
  `DIRECT_TILE_MAX_OBJECT_BYTES`, `DIRECT_TILE_MAX_ZARR_CHUNKS`, or
  `DIRECT_TILE_MAX_SHARD_INDEX_READS` values to make oversized direct renders
  fail fast with HTTP `503`.

The default read budgets are `0`, which disables each limit for local
development and compatibility. Production OCI deployments should set these from
live benchmark data and revisit them per dataset family.

## Zarr layout guidance

Chunking remains critical. For lat/lon scalar datasets, aim for one time step and
one tile-sized spatial chunk, such as `[1, 256, 256]`.

For current projected multiband imagery:

- source stores may use larger outer shard layouts;
- inner chunks close to `256x256` are preferred;
- shard indexes should stay small enough to keep hot decoded indexes in the
  configured `ZARR_SHARD_INDEX_CACHE_BYTES` budget;
- sharded stores should support efficient byte-range reads because direct source
  serving reads inner chunks by range instead of fetching whole shard objects;
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
- `304 Not Modified` for matching `If-None-Match` validators on non-range
  requests;
- `Cache-Control: public, max-age=3600`.

The proxy rejects unsafe object paths and only serves objects through known
dataset catalog entries.

## Workerized prefetch and range seeding

The frontend debounces viewport state before doing any speculative work. After
the map settles, it schedules prefetch through an idle callback and plans tile
URLs in `tilePrefetchPlanner.worker.ts` when Web Workers are available. If a
browser cannot start the worker, the hook uses the same planner on the main
thread as an idle-task fallback.

The planner computes the current tile, scores the surrounding radius-2 ring by
recent pan direction, adds zoom-in children or a zoom-out parent when zoom
intent is detected, and then caps the queue. Defaults are `32` queued tiles and
`3` in-flight fetches. Save-data, 2G/slow-2G, and low-memory devices use the
reduced budget of `8` queued tiles and `1` in-flight fetch. Stale work is
aborted whenever the viewport, dataset, variable, time index, colormap, display
range, or TileJSON template changes.

The center tile response is also used to seed `vmin` and `vmax` from
`X-Data-Vmin` and `X-Data-Vmax` when the user has not already selected an
explicit range. This keeps MapLibre as the visible tile loader while still
warming likely-next tiles at lower priority and avoiding a separate metadata
request for the first display range.

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

## Benchmarking live OCI performance

`scripts/oci_performance_benchmark.py` is the repeatable harness for real
OCI-backed cube timing. It measures metadata requests, TileJSON, a cold
viewport-sized tile pass, a repeated warm tile pass, and optional frontend or
Playwright readiness checks.

For each tile, the report records:

- response latency;
- response byte size;
- `X-Cache-Status`;
- `X-Representation`;
- `X-Execution-Path`;
- `X-Browse-Source` when present;
- optional debug timing and object I/O headers when
  `TILE_DEBUG_HEADERS_ENABLED=true`, including tile time, render/encode time,
  object GET counts, byte-range GET counts, bytes read, shard index reads, and
  Zarr chunk reads;
- direct tile budget status and any exceeded budget metric.

The report also includes `frontend_rendering`. With a Playwright probe, the
harness records the actual `.map-shell` rendering mode (`browser-gpu`,
`browser-native`, or `server-tiles`) plus GPU status and fallback reason.
Without a probe, it records the serving-profile eligibility so operators can
distinguish backend tile timing from browser-native or browser-GPU readiness.
The machine-readable report also includes `rendering_modes`, which breaks out
server tiles, browser-native, and browser-GPU support/readiness/active state.

For a local mocked GPU-path probe, start the frontend and run:

```bash
VIZARR_BROWSER_PROBE_SCENARIO=browser-gpu \
VIZARR_EXPECTED_FRONTEND_RENDER_MODE=browser-gpu \
node scripts/browser_multiscale_probe.cjs http://localhost:5173
```

The probe JSON includes `active_rendering_mode`, `gpu_status`, `gpu_ready`,
`gpu_reason`, `gpu_renderer`, selected dataset/variable/time/zoom, best
available page/render timings, and failed request count. The same probe can be
attached to the live benchmark:

```bash
PLAYWRIGHT_MODULE=playwright \
VIZARR_PLAYWRIGHT_COMMAND='node scripts/browser_multiscale_probe.cjs {frontend_url}' \
python3 scripts/oci_performance_benchmark.py \
  --output .cache/benchmarks/oci-benchmark.json
```

Use benchmark budgets to turn performance expectations into failures:

- metadata p95 budget;
- cold tile p95 budget;
- warm tile p95 budget;
- expected representation;
- forbidden direct `serving` for zoom bands that should be covered by browse or
  pyramid artifacts.

The command exits with `SKIP:` when no OCI-backed dataset is available or OCI
auth is missing. This keeps synthetic local development and CI shells from
failing when they cannot reach private Object Storage.

If a tile returns `direct_tile_compute_budget_exceeded`, the benchmark records it
as a direct budget hit in `budget_status_counts` and
`direct_budget_exceeded_count`, then fails with a budget-specific message. That
keeps budget failures separate from render failures, missing artifacts, and OCI
auth problems.

When invoked with `--matrix docs/oci-cube-matrix.example.json --matrix-entry
<id>`, the benchmark also validates live dataset metadata against the matrix:
Zarr format, consolidation state, CRS authority, expected variables,
expected composites, chunk-layout sharding, and optional supported rendering
modes. The matrix can also supply representation policy for the measured zoom
band.

## Planned work

### External Dask

Dask is not started by the current FastAPI lifespan. Do not assume a scheduler or
worker pool exists in production until code and deployment configuration are
added. If distributed mode is added later, direct request paths still need
bounded work submission, timeout handling, and per-tile I/O budgets so one
viewer interaction cannot enqueue unbounded source reads.
