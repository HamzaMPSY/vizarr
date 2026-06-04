# GPT 5.5 review
I reviewed the public docs and key implementation paths. Overall: **you have a solid architectural direction**, but the project is not yet “smooth/no-lag” by default. It is closer to a capable OCI-first prototype with good fallback paths than a production-grade, broadly compatible Zarr viewer.

## My blunt critique

### What is good

The core idea is strong: Vizarr is positioned as a satellite Zarr viewer with two main serving paths: backend-rendered WebP map tiles and read-only Zarr proxy endpoints for browser/native multiscale experiments. ([GitHub][1])

The architecture has the right building blocks: FastAPI, OCI discovery, a planner, Redis tile cache, browse artifacts, multiscale sidecars, MapLibre raster fallback, optional browser-native rendering, and a WebSocket invalidation path. ([GitHub][2])

You also already have useful observability. Tile responses can expose timing, cache state, planner path, representation, object-read count, byte-range reads, Zarr chunk count, and budget status via headers/logs. That is exactly the kind of instrumentation you need before optimizing. ([GitHub][3])

## Biggest things you are missing

### 1. You need real performance gates, not just benchmark scripts

Your docs say live OCI benchmarks should publish metadata p95, cold tile p95, warm tile p95, cache hit rate, render mode, GPU readiness/fallback reason, object GET count, and bytes read. But the baseline doc only defines very light CI checks: backend tests, frontend type-check, frontend build, synthetic tile WebP, and repeated synthetic cache hit. ([GitHub][4])

That means regressions like “first pan freezes for 4 seconds on real OCI data” will not fail CI.

What I would add:

```text
Required perf gates:
- synthetic cold tile p95 < 150 ms
- synthetic warm tile p95 < 30 ms
- OCI browse tile p95 < 250 ms
- OCI direct high-zoom tile p95 < 1000 ms, or fail fast with budget 503
- frontend pan interaction: no long task > 100 ms during 10-second pan test
- browser-native fallback reason must be visible in Playwright output
- cache hit rate after repeated viewport >= 80%
```

Without these, “fast enough” is based on hope.

### 2. Direct source serving is still a lag trap

Your own performance doc says direct source serving is correct but can be much slower because a single tile may touch multiple source chunks or require reprojection. It also says the current compute strategy is in-process, with no Dask scheduler, bounded source chunk reads, and browse/prebuilt pyramid expected to carry low/mid zoom performance. ([GitHub][3])

The most dangerous line is this: the default direct-read budgets are `0`, which disables each limit for local development and compatibility, while production should set them from live benchmark data. ([GitHub][3])

That means the app can silently fall into expensive direct reads unless you actively configure budgets.

I would make production fail closed:

```env
DIRECT_TILE_MAX_OBJECT_GETS=40
DIRECT_TILE_MAX_BYTE_RANGE_GETS=80
DIRECT_TILE_MAX_OBJECT_BYTES=16777216
DIRECT_TILE_MAX_ZARR_CHUNKS=64
DIRECT_TILE_MAX_SHARD_INDEX_READS=8
DIRECT_TILE_MAX_PARALLEL_CHUNK_READS=8
```

Tune those from actual OCI benchmark output, but do not ship with unbounded direct serving.

### 3. Browse and multiscale artifacts should be mandatory for smooth UX

The docs are honest here: browse overviews are the current first-view performance lever, and a valid dataset can still be slow if browse artifacts are missing. ([GitHub][3])

So the viewer should not treat missing browse/multiscale artifacts as a normal state. It should surface a warning before the user starts panning:

```text
This dataset is renderable but not optimized:
- missing_browse_overviews
- missing_multiscale_pyramid
Expected first-view latency: slow
Suggested fix: generate browse + multiscale artifacts
```

You already expose serving-profile gaps such as `missing_multiscale_pyramid`, `missing_browse_overviews`, and `incomplete_browse_overview_coverage`; the missing piece is making those gaps obvious in the UI and in CI. ([GitHub][5])

### 4. Your compatibility surface is still narrow

Right now the project is OCI-first, not a general Zarr viewer. The architecture doc explicitly says generic S3, GCS, Azure, Dask clusters, and predictive prefetch are still architectural ideas unless implemented by ticket. ([GitHub][2])

Important gaps from the compatibility contract:

| Missing / limited area             | Current status                                      |               |
| ---------------------------------- | --------------------------------------------------- | ------------- |
| Zarr v2 source stores              | Planned, while generated multiscale may be v2       |               |
| Xarray/Zarr v2 `_ARRAY_DIMENSIONS` | Planned                                             |               |
| Unsupported dimension orders       | Out of scope if spatial dims are not trailing `y/x` |               |
| Rotated/skewed affine transforms   | Out of scope                                        |               |
| Zarr v3 multiscale browser read    | Planned                                             |               |
| STAC item/collection discovery     | Planned                                             |               |
| STAC datacube/raster metadata      | Planned                                             |               |
| Full CF validation                 | Partial/planned                                     | ([GitHub][5]) |

For satellite data, the biggest missing features are probably **STAC ingestion**, **Zarr v2 source support**, **COG support**, **proper temporal UI**, **dataset diagnostics**, and **artifact-generation status**.

### 5. Browser-native/GPU path is too strict to rely on yet

The browser-GPU/browser-native path requires generated sidecars with Zarr v2 consolidated metadata, `time/band/y/x`, `<f4`, C order, no compressor/filters, `[1,1,256,256]` chunks, bounds, browse zoom mapping, data array name, and CRS/transform metadata. If any of that is missing, the frontend falls back to server tiles. ([GitHub][5])

That is reasonable for a controlled OCI pipeline, but it means this path is not a general performance solution. It is a fast path only for datasets you preprocess exactly right.

Also, the frontend budgets are conservative: 1,048,576 pixels, 64 chunks, 16 MiB estimated chunk bytes, and 4 concurrent chunk loads. ([GitHub][6]) This protects the browser, but large screens/high zooms will fall back frequently.

### 6. Docs and implementation are already drifting

Your frontend doc says there is no dedicated Web Worker and the MapLibre path uses a debounced prefetch hook instead. ([GitHub][7])

But the repo has a `frontend/src/workers/tilePrefetchPlanner.worker.ts`, and `useTilePrefetch` tries to instantiate that worker before falling back to in-thread planning. ([GitHub][8]) ([GitHub][9])

This is not a huge bug, but it is a warning sign: your docs are ambitious and detailed, so they need to stay mechanically tied to implementation or they will become misleading.

## Specific performance risks I see

### Prefetch can help, but it can also steal bandwidth

The prefetch hook defaults to radius `2`, queues up to 32 tiles, and allows 3 in-flight requests, dropping to 8 queued / 1 in-flight on slow connections or low-memory devices. ([GitHub][9])

That is sensible, but for expensive direct-serving tiles it can make lag worse: after a pan, the browser might request visible tiles and then prefetch many neighboring expensive tiles. You should disable prefetch when the serving profile says direct path is likely, browse coverage is missing, or tile debug headers show high object/chunk counts.

### Composite rendering loads bands sequentially in browser-native mode

In `useBrowserMultiscale`, composite rendering loops through the three bands and awaits each `loadLevelPlaneWindow` one after another. ([GitHub][6])

That is safer, but not fastest. For GPU/browser-native composites, you probably want parallel band reads with a shared total budget:

```ts
const planes = await Promise.all(
  compositeBands.map((band) => loadLevelPlaneWindow(...))
)
```

…but only after enforcing a combined chunk/byte budget across all three bands.

### Cache correctness is good, but cache effectiveness may suffer

The docs say Redis tile keys include dataset, variable/style, tile coordinate, time index, colormap, display range, representation, render mode, composite bands, and planner version. ([GitHub][3])

That prevents stale visual output, but user-controlled `vmin/vmax` sliders can destroy cache hit rate. You already round range values and can disable custom-range backend caching. ([GitHub][3]) I would go further: debounce range changes in the UI and only request tiles on pointer-up, while showing a client-side preview during dragging.

## Feature checklist I would add next

**High priority**

1. **Dataset readiness panel**: show CRS status, chunk shape, browse coverage, multiscale coverage, browser-native eligibility, GPU eligibility, fallback reason.
2. **Mandatory artifact generator workflow**: “generate browse + multiscale for this dataset” with progress/status.
3. **Live OCI perf dashboard**: store benchmark JSON history and compare p50/p95/cold/warm/cache-hit/object-reads per commit.
4. **Playwright pan/zoom smoke test**: fail if map errors, blank tiles, fallback loops, or long tasks appear.
5. **Production direct-read budgets**: fail fast instead of lagging indefinitely.
6. **STAC support**: discover datasets from STAC items/collections instead of only object prefixes.
7. **Zarr v2 source support**: this will unlock many existing stores.
8. **COG support**: for satellite use cases, COG is still very common and would make the viewer more broadly useful.

**Medium priority**

1. Time slider with actual timestamps, not just index.
2. Dataset search/filtering.
3. Coordinate readout and pixel probe.
4. Histogram endpoint for range selection.
5. CRS/layer diagnostics overlay.
6. Shareable URL state.
7. User-facing error messages for unsupported layouts.
8. Optional service worker tile cache with bounded eviction.
9. Frontend performance marks: tile wait, render mode, fallback reason, long tasks.
10. Better composite controls: RGB band mapping, per-band ranges, presets.

## My main recommendation

Make this rule central:

> **Direct source serving is a fallback, not the normal interactive path.**

For “no lag,” every production dataset should pass a readiness contract before it is considered smooth:

```text
Required for smooth mode:
- CRS present and valid
- north-up transform or supported coordinate axes
- chunk layout compatible with tile reads
- browse overview coverage for low/mid zooms
- multiscale sidecar for browser-native/GPU path
- Redis reachable
- direct-read budgets configured
- cold/warm p95 benchmark recorded
- Playwright pan/zoom smoke test passing
```

Right now you have many of the pieces, but the project still needs **enforced readiness**, **real p95 performance gates**, and **clear UI diagnostics** to avoid lag surprises.

[1]: https://github.com/HamzaMPSY/vizarr "GitHub - HamzaMPSY/vizarr: A vizarr project · GitHub"
[2]: https://github.com/HamzaMPSY/vizarr/blob/main/docs/architecture.md "vizarr/docs/architecture.md at main · HamzaMPSY/vizarr · GitHub"
[3]: https://github.com/HamzaMPSY/vizarr/blob/main/docs/performance.md "vizarr/docs/performance.md at main · HamzaMPSY/vizarr · GitHub"
[4]: https://github.com/HamzaMPSY/vizarr/blob/main/docs/performance-baselines.md "vizarr/docs/performance-baselines.md at main · HamzaMPSY/vizarr · GitHub"
[5]: https://github.com/HamzaMPSY/vizarr/blob/main/docs/compatibility.md "vizarr/docs/compatibility.md at main · HamzaMPSY/vizarr · GitHub"
[6]: https://github.com/HamzaMPSY/vizarr/blob/main/frontend/src/hooks/useBrowserMultiscale.ts "vizarr/frontend/src/hooks/useBrowserMultiscale.ts at main · HamzaMPSY/vizarr · GitHub"
[7]: https://github.com/HamzaMPSY/vizarr/blob/main/docs/frontend.md "vizarr/docs/frontend.md at main · HamzaMPSY/vizarr · GitHub"
[8]: https://github.com/HamzaMPSY/vizarr/tree/main/frontend/src/workers "vizarr/frontend/src/workers at main · HamzaMPSY/vizarr · GitHub"
[9]: https://github.com/HamzaMPSY/vizarr/blob/main/frontend/src/hooks/useTilePrefetch.ts "vizarr/frontend/src/hooks/useTilePrefetch.ts at main · HamzaMPSY/vizarr · GitHub"




# Claude Sonnet 4.6 Review

# Vizarr — Code Review & Implementation Gap Analysis

> Generated from reading all docs in `HamzaMPSY/vizarr`: README, architecture.md, backend.md, frontend.md, performance.md, IMPLEMENTATION_STATUS.md, DEV_WORKFLOW.md.
> Use this as a task list for Claude Code. Each issue includes a severity, description, and a concrete fix.

---

## Summary

| Severity | Count |
|---|---|
| 🔴 Critical | 5 |
| 🟠 High | 8 |
| 🟡 Medium | 9 |
| 🟢 Low | 6 |

---

## 🔴 Critical — Performance

### 1. Live bucket scan on every `/api/datasets` call

**Problem:** Every call to `/api/datasets` triggers live OCI object listing and reads `zarr.json` per store. With even 10–20 stores this takes multiple seconds on a cold cache. If Redis is flushed or TTL expires, every user hits this wall simultaneously. The `IMPLEMENTATION_STATUS.md` acknowledges this: "Reading per-store `zarr.json` and coordinate arrays during `/api/datasets` is too expensive."

**Fix:**
- Pre-generate a lightweight `datasets.json` manifest in OCI during startup (or via a background task) and refresh it on WebSocket dataset invalidation.
- On startup: if the manifest exists in OCI, load it immediately and serve it; scan the bucket async in the background.
- Store the manifest at a known key like `OCI_PREFIX + "_manifest/datasets.json"`.
- This is already described as step 3 of the Active Optimization Plan in `IMPLEMENTATION_STATUS.md` — it just hasn't been built.

---

### 2. No browse/pyramid artifacts → every tile is a cold OCI read

**Problem:** Browse and multiscale artifacts must be pre-generated offline using `generate_browse.py` and `generate_multiscale.py`. There is no lazy generation at request time. Any dataset without these artifacts falls through to direct source serving, which pays for: OCI GET → chunk decode → window extraction → colorize → WebP encode, on every single tile request. The Landsat data uses chunk shape `[1,1,512,512]`, meaning each tile read may fetch a 512×512 chunk to render a 256×256 output tile.

**Fix:**
- Add a lazy browse-tile generation path triggered on first cache miss: generate the tile, write it as a browse artifact to OCI, serve it, and cache it in Redis.
- Add a startup hook that generates browse artifacts for all catalogued datasets that are missing them (with bounded concurrency — e.g., 4 concurrent generations).
- Track artifact generation state (pending/done/failed) per dataset so parallel requests don't trigger duplicate generation jobs.

---

### 3. No Dask — CPU-bound tile rendering blocks the asyncio event loop

**Problem:** Dask is a dependency but the FastAPI lifespan never starts a scheduler or worker pool. All tile rendering is in-process. FastAPI runs on Uvicorn's asyncio event loop; a slow render (OCI round trip + NumPy slice + Pillow encode) blocks the loop for all concurrent users. There is no evidence of `asyncio.run_in_executor` or a `ThreadPoolExecutor` wrapping the blocking OCI SDK calls in `oci_object_storage.py` or `projected_tile_generator.py`.

**Fix (short-term):**
```python
# In projected_tile_generator.py and any OCI read path:
import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=8)

async def render_tile_async(...):
    return await asyncio.get_event_loop().run_in_executor(
        _executor, render_tile_sync, ...
    )
```

**Fix (medium-term):** Move tile generation to an ARQ or Celery worker pool. The FastAPI route enqueues a job and returns immediately; the frontend polls or receives the tile URL via WebSocket when it's ready.

---

### 4. Map starts centered nowhere near the real data

**Problem:** `IMPLEMENTATION_STATUS.md` explicitly says dataset bounds extraction is not done for projected imagery. The Landsat data is in `EPSG:32629` (UTM Zone 29N, northwest Africa). If the map loads at a default world view, users see nothing but ocean tiles rendering correctly while the actual scene is invisible. There is no auto-fit on dataset selection.

**Fix:**
1. During catalog build in `dataset_catalog.py`, extract the spatial extent from the Zarr store's `x`/`y` coordinate arrays and the `spatial_ref` metadata.
2. Reproject the bounding box from `EPSG:32629` (or whatever the native CRS is) to WGS84 using `pyproj`.
3. Store the WGS84 bbox `[west, south, east, north]` in the dataset record and manifest.
4. Expose it in the TileJSON `bounds` field (MapLibre reads this natively).
5. In `MapView.tsx`, call `map.fitBounds(tilejson.bounds, { padding: 40 })` when the active layer changes.

This is step 2 of the Active Optimization Plan in `IMPLEMENTATION_STATUS.md`.

---

### 5. Single Uvicorn worker + no HTTP/2

**Problem:** The Dockerfile and docker-compose files don't configure multiple Uvicorn workers. A browser fires 6–8 concurrent tile requests per pan event; a single worker can serve exactly one CPU-bound render at a time, queuing the rest. HTTP/1.1 via Nginx means 6 max parallel connections per origin — modern tile viewers need 12–16 concurrent tile fetches for smooth panning.

**Fix:**
```dockerfile
# In backend/Dockerfile, change:
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# To:
CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
```

For HTTP/2, add to `nginx/nginx.conf`:
```nginx
listen 443 ssl http2;
```
(Requires TLS; add a self-signed cert path for development. HTTP/2 requires HTTPS.)

---

## 🟠 High — Performance

### 6. Prefetch planner worker: docs contradict each other

**Problem:** `architecture.md` says "debounced adjacent-tile prefetch: implemented without a worker." `frontend.md` references `src/workers/tilePrefetchPlanner.worker.ts` as the off-main-thread planner. If the worker file doesn't exist, the prefetch computation runs on the main thread — a source of jank during fast panning, and a signal that docs and code are diverging.

**Fix:**
- Verify whether `src/workers/tilePrefetchPlanner.worker.ts` exists.
- If it doesn't, create it. Move the radius-2 ring + direction-aware tile-plan computation into the worker. The main hook sends a `postMessage` with the current viewport, receives back a tile URL list, then fetches them.
- Update `architecture.md` to match the actual code state.

---

### 7. Redis TTL-based invalidation is too coarse

**Problem:** When new Landsat data arrives, the WebSocket sends a dataset invalidation event that clears TanStack Query on the frontend — but the old rendered tiles remain in Redis for up to 3600 seconds and will be served to new requests during that window.

**Fix:**
```python
# In app/core/cache.py, add:
async def invalidate_dataset_tiles(redis, dataset_id: str):
    """Flush all Redis tile keys for a dataset using SCAN + DEL."""
    pattern = f"tile:{dataset_id}:*"
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break
```

Call this from the WebSocket dataset invalidation handler whenever a dataset's store changes.

---

### 8. Browser-native and GPU paths will never activate for real OCI data

**Problem:** `useBrowserMultiscale.ts` and `useDeckZarrRaster.ts` require: uncompressed float32 Zarr **v2**, consolidated metadata, `[1,1,256,256]` chunks, no filters. The actual OCI Landsat data is Zarr **v3**, Blosc-zstd compressed, `[1,1,512,512]` chunks. Both browser rendering paths will always fall back to server-rendered TileJSON for real data. The entire GPU shader infrastructure (`ZarrColormapBitmapLayer`, `ZarrCompositeBitmapLayer`) is currently inert.

**Fix (pragmatic):** Use `generate_multiscale.py` to produce sidecar stores specifically formatted for browser reading:
- Zarr v2, uncompressed, float32, `[1,1,256,256]` chunks, consolidated metadata.
- Store at `OCI_MULTISCALE_PREFIX_ROOT/{dataset_id}/`.
- The serving profile will then report `browser_gpu_ready: true` and the GPU path will activate.

**Fix (ambitious):** Add WASM-based Blosc decompression to the frontend using `numcodecs-wasm` so the browser can decompress Zarr v3 chunks natively.

---

## 🔴 Critical — Backend

### 9. `zarr==2.18.3` — stuck on the wrong major version

**Problem:** zarr 2.18.3 cannot open Zarr v3 stores as groups. This forced manual implementation of Zarr v3 metadata + chunk decoding in `core/zarr_v3.py`. You're now maintaining a custom chunk decoder that won't support sharding, storage transformers, or future Zarr v3 features. zarr-python 3.x has been stable since early 2025.

**Fix:**
```
# In requirements.txt:
zarr>=3.0.0,<4.0.0
```

The zarr 3.x API changed significantly (sync/async redesign, new `zarr.open_group`/`zarr.open_array` signatures). Key migration points:
- Replace `zarr.open_consolidated(...)` with `zarr.open_group(..., zarr_format=3)`.
- Replace manual chunk-coordinate-to-key logic with `array.get_basic_selection(...)`.
- Remove `core/zarr_v3.py` once the native API covers all use cases.
- Unlocks native sharding support — critical for reducing OCI GET count per tile.

---

## 🟠 High — Backend

### 10. Generic Zarr layout adapter is too narrow

**Problem:** The catalog only handles three array shapes: `y/x`, `time/y/x`, and `time/*/y/x` (banded). Any store with different dimension names (`latitude/longitude`, `lat/lon`, `row/col`) is silently skipped during catalog build with an unsupported-layout diagnostic. This limits the viewer to Landsat-style data only.

**Fix:**
```python
# In dataset_catalog.py, add a dimension alias resolver:
SPATIAL_X_ALIASES = {"x", "lon", "longitude", "col", "column", "easting"}
SPATIAL_Y_ALIASES = {"y", "lat", "latitude", "row", "northing"}
TIME_ALIASES = {"time", "t", "date", "datetime", "step"}

def resolve_spatial_dims(dims: list[str]) -> tuple[str, str] | None:
    x = next((d for d in dims if d.lower() in SPATIAL_X_ALIASES), None)
    y = next((d for d in dims if d.lower() in SPATIAL_Y_ALIASES), None)
    return (y, x) if x and y else None
```

Also add a config-level override (`OCI_DIM_OVERRIDE_JSON`) so operators can specify dimension mappings per dataset prefix without code changes.

---

### 11. Export jobs are in-memory only

**Problem:** `GET /exports/{job_id}` returns in-memory job state. A server restart or any deployment loses all export state. Users who submitted long clip/export jobs get a 404 after any crash or redeploy.

**Fix:**
```python
# In app/services/export_jobs.py, persist to Redis:
JOB_TTL = 86400  # 24 hours

async def create_job(redis, job: ExportJob) -> str:
    await redis.setex(f"export:{job.id}", JOB_TTL, job.model_dump_json())
    return job.id

async def get_job(redis, job_id: str) -> ExportJob | None:
    data = await redis.get(f"export:{job_id}")
    return ExportJob.model_validate_json(data) if data else None
```

---

### 12. No spatial index for dataset discovery by viewport

**Problem:** As the catalog grows, there's no way to answer "which datasets cover this bounding box?" without iterating all records. The tile endpoint also can't short-circuit requests that fall entirely outside a dataset's spatial extent — it renders and returns a blank tile, wasting backend compute and OCI reads.

**Fix:**
1. Once bounding boxes are available (fix #4), build an in-memory R-tree index at startup using `rtree` or `shapely.STRtree`.
2. Add `?bbox=west,south,east,north` filter to `GET /api/datasets`.
3. In the tile endpoint, check if the requested Web Mercator tile intersects the dataset's WGS84 bbox. If not, return a `204 No Content` (or a 1×1 transparent WebP) immediately without any OCI reads.

---

## 🟡 Medium — Backend

### 13. S3/GCS/Azure advertised in README but not implemented

**Problem:** The README headline reads "S3 / GCS / Azure via fsspec." `architecture.md` says: "Generic S3, GCS, Azure, Dask clusters, and predictive prefetch remain architectural ideas." This is a misleading claim for anyone outside your OCI environment.

**Fix (honest):** Update the README to state only OCI is currently supported.

**Fix (proper):** Add a generic fsspec storage backend. The OCI connector uses `ocifs` which is an fsspec implementation. An S3 backend is a near-drop-in:
```python
# In app/core/storage_backends.py:
import s3fs

class S3StorageConnector:
    def __init__(self, settings):
        self.fs = s3fs.S3FileSystem(
            key=settings.aws_access_key_id,
            secret=settings.aws_secret_access_key,
        )
```

---

### 14. OCI session token expires silently in production

**Problem:** Docs warn: "that profile is intentionally temporary and requires browser authentication when it cannot be refreshed." After token expiry, the backend returns 503 but from the user's perspective tiles just stop loading — no clear error message.

**Fix:**
- Add a proactive OCI auth check to `/api/healthz` that performs a lightweight OCI API call (e.g., list one object) and reports auth status.
- Add a frontend toast that fires when tile requests return a high rate of 503s: "API connection issue — tiles may not load."
- For production deployments, document and default to `OCI_AUTH_MODE=instance_principal` on OCI compute, which never expires.

---

### 15. Auth has no rate limiting or token rotation

**Problem:** API keys are static strings with no expiry, no rotation mechanism, and no per-key rate limiting. A leaked key grants permanent unlimited access until a manual redeploy.

**Fix (minimal):**
```python
# Per-key rate limiting via Redis sliding window:
async def check_rate_limit(redis, api_key: str, limit=1000, window=3600):
    key = f"ratelimit:{api_key}:{int(time.time() // window)}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    if count > limit:
        raise HTTPException(429, "Rate limit exceeded")
```

Add a `POST /api/admin/keys/rotate` endpoint that accepts the old key, generates a new one, and gives a grace period (e.g., 10 minutes) where both are valid.

---

## 🔴 Critical — Frontend

### 16. Time slider is missing — time series is unusable

**Problem:** `IMPLEMENTATION_STATUS.md` explicitly lists "time slider" as not done. Zustand holds `time_index` and it's wired to tile URLs, but there's no scrubber, animation control, or date label in the `Sidebar` component. For satellite data the core use case is often temporal change detection — without a time slider you're locked to time step 0.

**Fix:**
```tsx
// In src/components/Sidebar.tsx:
const { timeIndex, setTimeIndex } = useMapStore();
const { data: variables } = useVariables(activeDatasetId);
const times = variables?.find(v => v.id === activeVariableId)?.times ?? [];

// Render:
<label>Time: {times[timeIndex] ?? timeIndex}</label>
<input
  type="range"
  min={0}
  max={Math.max(0, times.length - 1)}
  value={timeIndex}
  onChange={e => setTimeIndex(Number(e.target.value))}
/>
// Play/pause button that calls setInterval(() => setTimeIndex(i => (i + 1) % times.length), 500)
```

---

## 🟠 High — Frontend

### 17. No colormap legend on the map

**Problem:** There is no visual legend showing what colormap colors represent in data units. For any quantitative satellite dataset (reflectance, temperature, NDVI) the viewer is scientifically unusable without this.

**Fix:**
```tsx
// New component: src/components/ColormapLegend.tsx
// Uses /api/colormaps/{name}/palette (already called by useDatasets)
// Renders a CSS gradient bar + vmin/vmax labels, overlaid on the map bottom-left.

const palette = useColormapPalette(colormap); // already exists in useDatasets.ts

const gradient = palette.map((rgba, i) =>
  `rgba(${rgba[0]},${rgba[1]},${rgba[2]},${rgba[3]}) ${(i / palette.length) * 100}%`
).join(', ');

return (
  <div style={{ position: 'absolute', bottom: 32, left: 16, zIndex: 10 }}>
    <div style={{ width: 160, height: 12, background: `linear-gradient(to right, ${gradient})` }} />
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
      <span>{vmin?.toFixed(2)}</span>
      <span>{vmax?.toFixed(2)}</span>
    </div>
  </div>
);
```

---

### 18. No data-value readout on map click

**Problem:** Users can't inspect the actual data value at any map location. `X-Data-Vmin`/`X-Data-Vmax` are used to seed the display range but there's no reverse pixel-to-value path and no click-to-query endpoint.

**Fix (backend):**
```python
# In app/api/query.py, add:
@router.get("/query/point")
async def query_point(
    dataset_id: str, variable: str,
    lat: float, lon: float, time_index: int = 0
):
    # Reproject lat/lon to dataset CRS, find nearest chunk, return value + units
    ...
```

**Fix (frontend):**
```tsx
// In MapView.tsx:
map.on('click', async (e) => {
  const { lat, lng } = e.lngLat;
  const result = await fetch(`/api/query/point?dataset_id=${activeDataset}&variable=${activeVariable}&lat=${lat}&lon=${lng}&time_index=${timeIndex}`);
  const { value, units } = await result.json();
  // Show MapLibre popup at e.lngLat with value + units
});
```

---

### 19. No error state surfaced to users

**Problem:** The backend returns HTTP 503 when a direct tile render exceeds its compute budget. MapLibre tracks tile errors via `error` events, but there's no documented error UI component. Users see blank/grey tiles with no explanation.

**Fix:**
```tsx
// In MapView.tsx, add to the MapLibre event subscriptions:
map.on('error', (e) => {
  if (e.error?.status === 503) {
    setTileError('Some tiles could not load — data may be too large for this zoom level.');
  }
});

// Also: for out-of-bounds tile requests, return a cached 1×1 transparent WebP
// from the backend instead of 404, to avoid MapLibre retry storms.
```

---

### 20. No URL-based state — can't share a view

**Problem:** All view state (dataset, variable, time index, colormap, vmin/vmax, map center/zoom) lives in Zustand memory. Close the tab and everything is lost. No way to share a specific scene or bookmark a view.

**Fix:**
```tsx
// In src/hooks/useUrlState.ts:
import { useEffect } from 'react';
import { useMapStore } from '../store/mapStore';

export function useUrlState() {
  const store = useMapStore();

  // Read from URL on mount
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get('d')) store.setActiveDataset(p.get('d')!);
    if (p.get('v')) store.setActiveVariable(p.get('v')!);
    if (p.get('t')) store.setTimeIndex(Number(p.get('t')));
    if (p.get('cm')) store.setColormap(p.get('cm')!);
  }, []);

  // Write to URL on change (replaceState to avoid polluting history)
  useEffect(() => {
    const p = new URLSearchParams({
      d: store.activeDatasetId ?? '',
      v: store.activeVariableId ?? '',
      t: String(store.timeIndex),
      cm: store.colormap,
    });
    window.history.replaceState(null, '', `?${p}`);
  }, [store.activeDatasetId, store.activeVariableId, store.timeIndex, store.colormap]);
}
```

---

### 21. `vmin`/`vmax` override controls not in the UI

**Problem:** `IMPLEMENTATION_STATUS.md` lists "range overrides" as not done. Zustand holds `vmin`/`vmax` and the prefetch hook seeds them from response headers — but there's no manual input in the Sidebar. Contrast stretching is critical for raw satellite imagery.

**Fix:**
```tsx
// In Sidebar.tsx, add:
<label>Display range</label>
<div style={{ display: 'flex', gap: 8 }}>
  <input
    type="number"
    placeholder={`min (${seededVmin?.toFixed(2)})`}
    value={vmin ?? ''}
    onChange={e => setVmin(e.target.value ? Number(e.target.value) : null)}
  />
  <input
    type="number"
    placeholder={`max (${seededVmax?.toFixed(2)})`}
    value={vmax ?? ''}
    onChange={e => setVmax(e.target.value ? Number(e.target.value) : null)}
  />
  <button onClick={() => { setVmin(null); setVmax(null); }}>Reset</button>
</div>
```

---

### 22. WebSocket reconnect has no exponential backoff

**Problem:** `useDatasetInvalidation.ts` reconnects after disconnects but there's no backoff strategy. If the backend restarts, all connected clients simultaneously retry every second, creating a thundering-herd reconnect storm.

**Fix:**
```tsx
// In src/hooks/useDatasetInvalidation.ts:
let retryDelay = 1000;

function connect() {
  const ws = new WebSocket('/ws/datasets');
  ws.onopen = () => { retryDelay = 1000; }; // reset on success
  ws.onclose = () => {
    const jitter = retryDelay * (0.8 + Math.random() * 0.4);
    setTimeout(connect, jitter);
    retryDelay = Math.min(retryDelay * 2, 30000); // cap at 30s
  };
  ws.onmessage = handleInvalidation;
}
```

---

## 🟡 Medium — Frontend

### 23. Deck.gl loads unconditionally even though the GPU path is currently inert

**Problem:** `@deck.gl/mapbox` and `@deck.gl/layers` are ~1.5MB of bundle that loads on every page visit even though the GPU path never activates for real OCI data (see issue #8). This adds to initial load time with no benefit until sidecar stores are generated.

**Fix:**
```tsx
// In App.tsx or MapView.tsx, lazy-load DeckRasterOverlay:
const DeckRasterOverlay = React.lazy(() =>
  import('./components/DeckRasterOverlay')
);

// Only render it when serving profile reports gpu readiness:
{servingProfile?.browser_gpu_ready && (
  <React.Suspense fallback={null}>
    <DeckRasterOverlay ... />
  </React.Suspense>
)}
```

---

### 24. RGB/false-color composites bypass browse and pyramid — 3× the OCI cost

**Problem:** `backend.md` states: "Composite requests bypass browse overviews and pyramid artifacts for now, render from the source bands." Every composite tile fetches all three band chunks from OCI on every request, making composites dramatically slower than single-band tiles at low zoom.

**Fix:** Generate pre-rendered RGB browse tiles using `generate_browse.py` for each composite style. Add composite-style-aware cache keys in the planner and serve composite browse artifacts the same way single-band browse artifacts are served.

---

### 25. No dataset search or filter in the sidebar

**Problem:** With a growing catalog of scenes, the dataset picker is a flat unfiltered list. No search, no date range filter, no "show only datasets in current viewport" option.

**Fix (phase 1):** Client-side text search over the already-fetched dataset list:
```tsx
const [search, setSearch] = useState('');
const filtered = datasets?.filter(d =>
  d.name.toLowerCase().includes(search.toLowerCase())
) ?? [];
```

**Fix (phase 2):** Once bounding boxes are available (fix #4), add a "current viewport only" toggle that calls `GET /api/datasets?bbox=...`.

---

## 🟢 Low / Refinements

### 26. No CI/CD pipeline — OCI path has zero test coverage

**Problem:** Backend tests exist for synthetic mode only. The OCI connector, projected tile generator, catalog builder, and Zarr v3 decoder have no automated tests. There's no GitHub Actions workflow visible.

**Fix:**
```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  backend-tests:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7
        ports: ['6379:6379']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r backend/requirements.txt
      - run: pytest backend/tests/ -v
        env:
          STORAGE_BACKEND: synthetic
          REDIS_URL: redis://localhost:6379

  frontend-checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd frontend && npm ci && npm run type-check && npm run lint
```

---

### 27. Country borders GeoJSON is hardcoded to a local file

**Problem:** `Sidebar.tsx` adds a Natural Earth GeoJSON source at `ne_110m_admin_0_boundary_lines_land.geojson`. This works only if the file is being served by Nginx or Vite, which isn't documented anywhere. A missing file causes a silent MapLibre fetch error.

**Fix:** Serve the file from a documented static path in the Nginx config, or fetch it from a CDN: `https://cdn.jsdelivr.net/npm/natural-earth-vector@5.0.1/geojson/ne_110m_admin_0_boundary_lines_land.geojson`. Document the dependency.

---

### 28. Predictive prefetch is directional heuristics, not truly predictive

**Problem:** The README promises "predictive prefetching." The implementation is a radius-2 ring with pan-direction bias. For fast-panning users, the ring tiles are already visible before speculative tiles land.

**Fix:** Track the last 3–5 viewport positions, compute panning velocity, and extrapolate the map center 300–500ms ahead. Prefetch the tile grid at that predicted position first, before the radius-2 ring.

```ts
// In tilePrefetchPlanner.worker.ts:
function predictNextCenter(history: ViewState[]): {lat: number, lng: number} {
  if (history.length < 2) return history[history.length - 1];
  const last = history[history.length - 1];
  const prev = history[history.length - 2];
  const dt = last.timestamp - prev.timestamp;
  const vLat = (last.lat - prev.lat) / dt;
  const vLng = (last.lng - prev.lng) / dt;
  return { lat: last.lat + vLat * 400, lng: last.lng + vLng * 400 }; // 400ms ahead
}
```

---

### 29. `TILE_DEBUG_HEADERS_ENABLED` has no frontend debug overlay

**Problem:** The backend emits detailed `X-Tile-Time-Ms`, `X-Object-Get-Count`, `X-Zarr-Chunk-Count`, etc. headers when `TILE_DEBUG_HEADERS_ENABLED=true`. These are invaluable for diagnosing slow tiles but there's no frontend UI to display them.

**Fix:** Add a dev-mode debug panel (hidden behind `?debug=1` in the URL) that reads `X-*` response headers from the last tile fetch and displays them in a floating overlay on the map.

---

### 30. No permalink / view sharing

Already covered as issue #20 (URL state). Separating it here as a reminder to also add a "Copy link" button in the UI that copies the current URL with state params.

---

## What's Already Done Well

These are genuinely solid — don't change them.

- **Cache key design** is thorough. Including dataset, variable, z/x/y, time, colormap, vmin/vmax, representation, and planner version in the Redis key prevents any stale-byte collisions across style changes.
- **Response headers** are an excellent debugging surface. `X-Cache-Status`, `X-Request-Class`, `X-Execution-Path`, `X-Representation`, `X-Browse-Source`, and the optional debug headers give complete observability per tile without a separate tracing system.
- **Graceful degradation cascade** (browse → pyramid → direct source) with explicit path headers is the right pattern. The budget system (`DIRECT_TILE_MAX_OBJECT_GETS`, `DIRECT_TILE_MAX_ZARR_CHUNKS`, etc.) correctly prevents runaway OCI costs.
- **Security posture**: Zarr proxy is dataset-scoped, read-only, with path traversal protection. API key scoping to dataset IDs is a thoughtful multi-tenant design.
- **WebSocket invalidation flow** is the right architecture — push, not poll. The TanStack Query invalidation cascade (datasets → variables → serving-profile → TileJSON) is correct.
- **Serving profile API** (`/api/datasets/{id}/serving-profile`) is a smart design. Exposing `seamless_rendering_gaps`, `browser_gpu_ready`, `browser_gpu_reason`, and per-level descriptors lets the frontend make intelligent rendering decisions without hardcoding backend knowledge in the client.
- **vmin/vmax seeding from the center tile** is clever — avoids a separate stats request and adapts the colormap to the actual data range at the current view.
- **Documentation quality** is unusually high for an early-stage project. Five separate docs with explicit "implemented vs planned" tables and honest performance bottleneck acknowledgment.

---

## Priority Order for Claude Code

Work through these in order for maximum impact:

1. **Fix #1** — Dataset manifest (eliminates the worst latency spike)
2. **Fix #4** — Bounds extraction + map auto-fit (makes the app usable with real data)
3. **Fix #3** — Wrap OCI reads in `run_in_executor` (prevents event loop blocking)
4. **Fix #16** — Time slider (core feature for satellite data)
5. **Fix #5** — Multiple Uvicorn workers (immediate throughput improvement)
6. **Fix #2** — Lazy browse artifact generation (eliminates slow cold tiles)
7. **Fix #9** — Upgrade zarr to 3.x (unlocks native Zarr v3 support, removes zarr_v3.py workaround)
8. **Fix #17 + #18** — Legend + click-to-query (makes the viewer scientifically useful)
9. **Fix #8** — Generate browser-compatible multiscale sidecars (activates the GPU path)
10. **Fix #20 + #21** — URL state + range override controls (UX polish)



# Kimi K2.6

## P0: ProcessPoolExecutor for Tile Rendering

### The Problem
Your current tile rendering uses `run_in_threadpool` which is bound by Python's GIL. For CPU-intensive numpy bilinear resampling + PIL WebP encoding, threads don't help.

### Implementation

**1. Create a process pool in `backend/app/core/tile_worker_pool.py`:**

```python
import os
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

def get_tile_process_pool() -> ProcessPoolExecutor:
    # Singleton process pool for tile rendering. Must be initialized once.
    max_workers = int(os.getenv("TILE_RENDER_WORKERS", os.cpu_count() or 4))
    return ProcessPoolExecutor(max_workers=max_workers)

# Shutdown hook for FastAPI
async def shutdown_tile_pool():
    pool = get_tile_process_pool()
    pool.shutdown(wait=True)
```

**2. Modify `backend/app/api/tiles.py` to use the process pool:**

```python
from app.core.tile_worker_pool import get_tile_process_pool
from concurrent.futures import ProcessPoolExecutor

# Make render functions picklable by moving them to a separate module
# (functions defined inside other functions or closures can't be pickled)

@router.get("/tiles/{dataset_id}/{z}/{x}/{y}.webp")
async def get_tile(
    dataset_id: str,
    z: int, x: int, y: int,
    # ... existing params ...
):
    # ... validation and cache lookup ...

    if cached:
        return Response(cached, media_type="image/webp")

    # Use process pool for CPU-intensive rendering
    pool = get_tile_process_pool()

    # Pass only picklable data to the worker
    loop = asyncio.get_event_loop()
    image_bytes = await loop.run_in_executor(
        pool,
        render_tile_worker,
        {
            "dataset_id": dataset_id,
            "z": z, "x": x, "y": y,
            "colormap_name": colormap_name,
            "vmin": vmin,
            "vmax": vmax,
            "band_index": band_index,
            "time_index": time_index,
            # ... other params
        }
    )

    # Cache the result
    await cache.set(cache_key, image_bytes, ttl=3600)
    return Response(image_bytes, media_type="image/webp")
```

**3. Create `backend/app/core/tile_renderer_worker.py` (must be importable, not nested):**

```python
# Picklable tile rendering functions for ProcessPoolExecutor.
import numpy as np
from PIL import Image
from app.core.zarr_v3 import load_4d_window  # re-import inside worker
from app.core.colormap import get_colormap  # re-import inside worker

def render_tile_worker(params: dict) -> bytes:
    # Runs in a separate process. Must re-initialize all connections.
    from app.core.dataset_catalog import get_dataset

    dataset = get_dataset(params["dataset_id"])
    zarr_path = dataset.zarr_path

    # ... your existing render logic ...
    # load_4d_window, apply colormap, bilinear resample, WebP encode

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=85, method=6)
    return buffer.getvalue()
```

**Critical:** ProcessPoolExecutor requires all passed functions to be picklable. Move ALL rendering logic out of closures/nested functions into top-level module functions. Also, any global state (Redis connections, OCI clients) must be re-initialized inside the worker — they cannot be shared.

---

## P0: Background Overview Generation (Never Block Requests)

### The Problem
`_build_overview()` in `browse_tiles.py` runs synchronously during a tile request, blocking the response for 10-30 seconds.

### Implementation

**1. Add a background task queue using Celery or a simple Redis-backed queue:**

```python
# backend/app/core/job_queue.py
import redis
import json
from datetime import datetime

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

def enqueue_overview_generation(dataset_id: str, zarr_path: str, overview_path: str):
    job = {
        "type": "build_overview",
        "dataset_id": dataset_id,
        "zarr_path": zarr_path,
        "overview_path": overview_path,
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending"
    }
    r.lpush("tile_jobs", json.dumps(job))
    return job["id"]

def get_job_status(job_id: str) -> dict:
    # ... lookup in Redis ...
    pass
```

**2. Add a background worker process:**

```python
# backend/worker.py — run as a separate service
import time
import json
from app.core.job_queue import r
from app.core.browse_tiles import _build_overview

def process_jobs():
    while True:
        _, job_json = r.brpop("tile_jobs", timeout=5)
        if job_json:
            job = json.loads(job_json)
            if job["type"] == "build_overview":
                try:
                    _build_overview(
                        job["zarr_path"],
                        job["overview_path"],
                        # ... params
                    )
                    update_job_status(job["id"], "completed")
                except Exception as e:
                    update_job_status(job["id"], "failed", error=str(e))
        time.sleep(0.1)

if __name__ == "__main__":
    process_jobs()
```

**3. Modify the tile endpoint to return 202 Accepted instead of blocking:**

```python
@router.get("/tiles/{dataset_id}/{z}/{x}/{y}.webp")
async def get_tile(...):
    # ... check cache ...

    if not overview_exists and not allow_build:
        raise HTTPException(404, "Overview not found")

    if not overview_exists and allow_build:
        # Check if job is already queued
        job_status = get_overview_job_status(dataset_id)
        if job_status == "pending":
            return Response(
                status_code=202,
                content=b"",  # Or return a placeholder "generating" tile
                media_type="image/webp"
            )
        elif job_status is None:
            enqueue_overview_generation(dataset_id, zarr_path, overview_path)
            return Response(status_code=202, content=b"")

    # ... normal rendering ...
```

**4. Frontend: Show a "generating preview" state:**

```typescript
// In your tile loading logic
const handleTileError = (error: any) => {
    if (error.status === 202) {
        // Show a spinner or gray placeholder tile
        return placeholderTile;
    }
    // ... existing error handling
};
```

---

## P0: URL State Sync (Shareable Links)

### The Problem
Map state (center, zoom, dataset, variable, time, colormap, range) is not in the URL. Users can't share or bookmark views.

### Implementation

**1. Add a URL sync hook in `frontend/src/hooks/useUrlSync.ts`:**

```typescript
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom"; // or use custom parser
import { useMapStore } from "../store/mapStore";

export function useUrlSync() {
    const [searchParams, setSearchParams] = useSearchParams();
    const { dataset, variable, timeIndex, colormap, vmin, vmax, center, zoom } = useMapStore();

    // Read URL to state (on mount)
    useEffect(() => {
        const ds = searchParams.get("dataset");
        const var_ = searchParams.get("variable");
        const time = searchParams.get("time");
        const cmap = searchParams.get("colormap");
        const vmin = searchParams.get("vmin");
        const vmax = searchParams.get("vmax");
        const lat = searchParams.get("lat");
        const lon = searchParams.get("lon");
        const z = searchParams.get("zoom");

        if (ds) useMapStore.getState().setDataset(ds);
        if (var_) useMapStore.getState().setVariable(var_);
        // ... etc ...
    }, []);

    // Write state to URL (debounced)
    useEffect(() => {
        const params = new URLSearchParams();
        if (dataset) params.set("dataset", dataset.id);
        if (variable) params.set("variable", variable.name);
        if (timeIndex !== undefined) params.set("time", String(timeIndex));
        if (colormap) params.set("colormap", colormap);
        if (vmin !== undefined) params.set("vmin", String(vmin));
        if (vmax !== undefined) params.set("vmax", String(vmax));
        if (center) {
            params.set("lat", String(center[1]));
            params.set("lon", String(center[0]));
        }
        if (zoom !== undefined) params.set("zoom", String(zoom));

        // Use replaceState to avoid polluting browser history on every change
        window.history.replaceState(null, "", `?${params.toString()}`);
    }, [dataset?.id, variable?.name, timeIndex, colormap, vmin, vmax, center, zoom]);
}
```

**2. Add `react-router-dom` to your dependencies and wrap App:**

```typescript
// main.tsx
import { BrowserRouter } from "react-router-dom";

<BrowserRouter>
    <App />
</BrowserRouter>
```

**3. Call `useUrlSync()` in `App.tsx`:**

```typescript
function App() {
    useUrlSync(); // Add this
    return (
        <div className="app">
            <MapView />
            <Sidebar />
        </div>
    );
}
```

---

## P0: Pixel Value Inspection (Click-to-Query)

### The Problem
Users can't click on the map to see the exact data value at a pixel.

### Implementation

**1. Backend: Add a pixel query endpoint `backend/app/api/query.py`:**

```python
from fastapi import APIRouter, HTTPException
import numpy as np
from app.core.zarr_v3 import load_4d_window
from app.core.dataset_catalog import get_dataset

router = APIRouter(prefix="/api/query")

@router.get("/pixel/{dataset_id}")
async def query_pixel(
    dataset_id: str,
    lat: float,
    lon: float,
    band_index: int = 0,
    time_index: int = 0,
    api_key: str = None,
):
    dataset = get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")

    # Convert lat/lon to pixel coordinates using dataset CRS/transform
    # This requires your dataset to have geotransform info in metadata
    transform = dataset.geotransform  # [a, b, c, d, e, f]

    # Inverse transform: pixel = inv(transform) * (lon, lat)
    # Use rasterio or affine library
    from affine import Affine
    aff = Affine.from_gdal(*transform)
    col, row = ~aff * (lon, lat)

    col, row = int(col), int(row)

    # Read single pixel (or small window around it)
    data = load_4d_window(
        dataset.zarr_path,
        y_start=row, y_stop=row+1,
        x_start=col, x_stop=col+1,
        band_index=band_index,
        time_index=time_index,
    )

    value = float(data[0, 0, 0, 0]) if data.size > 0 else None

    return {
        "lat": lat,
        "lon": lon,
        "pixel_col": col,
        "pixel_row": row,
        "value": value,
        "band_index": band_index,
        "time_index": time_index,
        "units": dataset.variables[band_index].units if hasattr(dataset, "variables") else None,
    }
```

**2. Frontend: Add click handler in `MapView.tsx`:**

```typescript
import { useState } from "react";
import { useMapStore } from "../store/mapStore";

export function PixelInspector() {
    const [pixelInfo, setPixelInfo] = useState<{value: number; lat: number; lon: number} | null>(null);
    const { dataset, variable, timeIndex } = useMapStore();

    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        const handleClick = async (e: maplibregl.MapMouseEvent) => {
            if (!dataset || !variable) return;

            const { lng, lat } = e.lngLat;

            const res = await fetch(
                `/api/query/pixel/${dataset.id}?lat=${lat}&lon=${lng}&band_index=${variable.bandIndex}&time_index=${timeIndex}`
            );
            const data = await res.json();
            setPixelInfo(data);
        };

        map.on("click", handleClick);
        return () => map.off("click", handleClick);
    }, [dataset, variable, timeIndex]);

    if (!pixelInfo) return null;

    return (
        <div className="pixel-inspector-popup">
            <div>Lat: {pixelInfo.lat.toFixed(4)}</div>
            <div>Lon: {pixelInfo.lon.toFixed(4)}</div>
            <div>Value: {pixelInfo.value !== null ? pixelInfo.value.toFixed(4) : "N/A"}</div>
            <div>Units: {pixelInfo.units || ""}</div>
        </div>
    );
}
```

**3. Add CSS for the popup:**

```css
.pixel-inspector-popup {
    position: absolute;
    bottom: 20px;
    left: 20px;
    background: rgba(0, 0, 0, 0.8);
    color: white;
    padding: 12px;
    border-radius: 8px;
    font-family: monospace;
    font-size: 12px;
    z-index: 1000;
    pointer-events: none;
}
```

---

## P1: Request Deduplication (Request Coalescing)

### The Problem
If 10 users request the same uncached tile simultaneously, your server renders it 10 times.

### Implementation

**1. Add an in-flight request cache in `backend/app/core/dedup.py`:**

```python
import asyncio
from typing import Dict, Any

_in_flight: Dict[str, asyncio.Future] = {}

async def deduped_execute(key: str, coro_factory) -> Any:
    #
    # Ensures only one execution of `coro_factory` runs for a given `key`.
    # All concurrent callers with the same key wait for the same result.
    #
    if key in _in_flight:
        return await _in_flight[key]

    future = asyncio.get_event_loop().create_future()
    _in_flight[key] = future

    try:
        result = await coro_factory()
        future.set_result(result)
        return result
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        del _in_flight[key]
```

**2. Use it in the tile endpoint:**

```python
from app.core.dedup import deduped_execute

@router.get("/tiles/{dataset_id}/{z}/{x}/{y}.webp")
async def get_tile(...):
    cache_key = f"tile:{dataset_id}:{z}:{x}:{y}:{colormap_name}:{vmin}:{vmax}:{band_index}:{time_index}"

    # Check Redis cache first
    cached = await cache.get(cache_key)
    if cached:
        return Response(cached, media_type="image/webp")

    # Deduplicate in-flight requests
    async def render():
        return await render_tile_async(...)  # your existing logic

    image_bytes = await deduped_execute(cache_key, render)

    # Store in Redis cache
    await cache.set(cache_key, image_bytes, ttl=3600)
    return Response(image_bytes, media_type="image/webp")
```

---

## P1: Time Series Animation

### The Problem
No play/pause button for time series. Users must manually click through time steps.

### Implementation

**1. Add animation state to `mapStore.ts`:**

```typescript
interface AnimationState {
    isPlaying: boolean;
    fps: number;
    direction: "forward" | "backward";
    loop: boolean;
}

// Add to store:
animation: AnimationState;
setAnimationPlaying: (playing: boolean) => void;
setAnimationFps: (fps: number) => void;
```

**2. Create `frontend/src/hooks/useAnimation.ts`:**

```typescript
import { useEffect, useRef, useCallback } from "react";
import { useMapStore } from "../store/mapStore";

export function useAnimation() {
    const { isPlaying, fps, timeIndex, maxTimeIndex, setTimeIndex } = useMapStore();
    const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const advance = useCallback(() => {
        setTimeIndex((prev) => {
            if (prev >= maxTimeIndex) return 0; // loop
            return prev + 1;
        });
    }, [maxTimeIndex, setTimeIndex]);

    useEffect(() => {
        if (isPlaying) {
            const ms = 1000 / fps;
            intervalRef.current = setInterval(advance, ms);
        } else {
            if (intervalRef.current) clearInterval(intervalRef.current);
        }

        return () => {
            if (intervalRef.current) clearInterval(intervalRef.current);
        };
    }, [isPlaying, fps, advance]);
}
```

**3. Add animation controls to `Sidebar.tsx`:**

```typescript
<div className="animation-controls">
    <button onClick={() => setAnimationPlaying(!isPlaying)}>
        {isPlaying ? "Pause" : "Play"}
    </button>
    <input
        type="range"
        min={1}
        max={30}
        value={fps}
        onChange={(e) => setAnimationFps(Number(e.target.value))}
    />
    <span>{fps} fps</span>
</div>
```

**4. Pre-buffer adjacent time step tiles:**

```typescript
// In useTilePrefetch.ts, when animation is playing:
useEffect(() => {
    if (!isPlaying) return;

    // Pre-fetch tiles for next time step
    const nextTimeIndex = (timeIndex + 1) % maxTimeIndex;
    prefetchTilesForTimeStep(nextTimeIndex);
}, [timeIndex, isPlaying]);
```

---

## P1: Colorbar / Legend

### The Problem
No visual colorbar showing the mapping from data values to colors.

### Implementation

**1. Create `frontend/src/components/Colorbar.tsx`:**

```typescript
import { useMemo } from "react";
import { useMapStore } from "../store/mapStore";

export function Colorbar() {
    const { colormap, vmin, vmax } = useMapStore();

    const gradient = useMemo(() => {
        if (!colormap) return "linear-gradient(to right, #000, #fff)";
        // Generate gradient stops from the colormap
        const stops = colormap.map((color, i) => {
            const pos = (i / (colormap.length - 1)) * 100;
            return `${color} ${pos}%`;
        });
        return `linear-gradient(to right, ${stops.join(", ")})`;
    }, [colormap]);

    const ticks = useMemo(() => {
        if (vmin === undefined || vmax === undefined) return [];
        const count = 5;
        return Array.from({ length: count }, (_, i) => {
            const value = vmin + (vmax - vmin) * (i / (count - 1));
            return { value, pos: (i / (count - 1)) * 100 };
        });
    }, [vmin, vmax]);

    return (
        <div className="colorbar-container">
            <div className="colorbar-gradient" style={{ background: gradient }} />
            <div className="colorbar-ticks">
                {ticks.map((t) => (
                    <span key={t.pos} style={{ left: `${t.pos}%` }}>
                        {t.value.toFixed(2)}
                    </span>
                ))}
            </div>
        </div>
    );
}
```

**2. CSS:**

```css
.colorbar-container {
    position: absolute;
    bottom: 40px;
    right: 20px;
    width: 200px;
    background: rgba(0, 0, 0, 0.7);
    padding: 10px;
    border-radius: 4px;
    z-index: 1000;
}

.colorbar-gradient {
    height: 20px;
    border-radius: 2px;
}

.colorbar-ticks {
    position: relative;
    height: 20px;
    margin-top: 4px;
}

.colorbar-ticks span {
    position: absolute;
    transform: translateX(-50%);
    font-size: 10px;
    color: white;
    font-family: monospace;
}
```

---

## P1: CORS Restriction

### The Problem
`allow_origins=["*"]` is dangerous in production.

### Implementation

**Modify `backend/app/main.py`:**

```python
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI()

# Production CORS
allowed_origins = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:3000"  # dev defaults
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],  # Restrict methods
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    max_age=600,
)
```

**In your deployment environment (`.env` or k8s config):**

```bash
CORS_ALLOWED_ORIGINS=https://vizarr.yourdomain.com,https://app.yourdomain.com
```

---

## P1: HTTP/2 + Brotli Compression

### Nginx Configuration (`nginx.conf`):

```nginx
server {
    listen 443 ssl http2;  # Enable HTTP/2

    # Brotli compression (requires nginx-brotli module)
    brotli on;
    brotli_comp_level 6;
    brotli_types application/json text/css application/javascript image/svg+xml;

    # Gzip fallback
    gzip on;
    gzip_vary on;
    gzip_types application/json text/css application/javascript;

    location /api/ {
        proxy_pass http://backend;
        proxy_http_version 1.1;  # FastAPI doesn't support HTTP/2 directly
        # But Nginx handles HTTP/2 termination
    }

    location /tiles/ {
        proxy_pass http://backend;
        # Tiles are already compressed (WebP), don't re-compress
        gzip off;
        brotli off;

        # Cache tiles aggressively
        proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=tiles:100m max_size=10g;
        proxy_cache tiles;
        proxy_cache_valid 200 1d;
        proxy_cache_use_stale error timeout updating;
    }
}
```

---

## P2: Service Worker for Offline Caching

### Implementation with Vite PWA

**1. Install:**
```bash
npm install vite-plugin-pwa -D
```

**2. Update `vite.config.ts`:**

```typescript
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
    plugins: [
        react(),
        VitePWA({
            registerType: "autoUpdate",
            workbox: {
                globPatterns: ["**/*.{js,css,html,ico,png,svg,webp}"],
                runtimeCaching: [
                    {
                        urlPattern: /^https:\/\/your-api\.com\/api\/datasets/,
                        handler: "CacheFirst",
                        options: {
                            cacheName: "dataset-metadata",
                            expiration: { maxEntries: 50, maxAgeSeconds: 86400 }
                        }
                    },
                    {
                        urlPattern: /^https:\/\/your-api\.com\/tiles\//,
                        handler: "StaleWhileRevalidate",
                        options: {
                            cacheName: "tile-cache",
                            expiration: { maxEntries: 5000, maxAgeSeconds: 604800 }
                        }
                    }
                ]
            }
        })
    ]
});
```

---

## P2: Frontend Unit Tests (Vitest)

**1. Install:**
```bash
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

**2. `vite.config.ts`:**
```typescript
export default defineConfig({
    test: {
        environment: "jsdom",
        globals: true,
        setupFiles: "./src/test/setup.ts"
    }
});
```

**3. `src/test/setup.ts`:**
```typescript
import "@testing-library/jest-dom";
```

**4. Example test for `multiscale.ts`:**
```typescript
import { describe, it, expect } from "vitest";
import { getMultiscaleTileInfo, renderMultiscaleRaster } from "../lib/multiscale";

describe("multiscale rendering", () => {
    it("should render a simple 2x2 raster", () => {
        const raster = {
            data: new Float32Array([1, 2, 3, 4]),
            shape: [1, 1, 2, 2],
            dtype: "float32",
            metadata: { spatialReference: "EPSG:4326", geoTransform: [0, 1, 0, 0, 0, -1] }
        };

        const result = renderMultiscaleRaster(raster, "viridis", 0, 5, 256, 256);

        expect(result).toBeDefined();
        expect(result.dataUrl).toMatch(/^data:image\/png;base64,/);
    });
});
```

---

## Quick Wins (Low Effort, High Impact)

| Task | File | Change |
|------|------|--------|
| Add scale bar | `MapView.tsx` | `map.addControl(new maplibregl.ScaleControl());` |
| Fullscreen | `MapView.tsx` | `map.addControl(new maplibregl.FullscreenControl());` |
| Coordinate display | `MapView.tsx` | `map.on("mousemove", (e) => setCoords(e.lngLat));` |
| Keyboard shortcuts | `App.tsx` | `useEffect(() => window.addEventListener("keydown", ...), [])` |
| Minimap | New component | Use `maplibregl.Map` in a small div with `sync` to main map |
| Opacity slider | `Sidebar.tsx` | `<input type="range" min="0" max="1" step="0.01" onChange={setOpacity} />` |
| Print/screenshot | `Sidebar.tsx` | `map.getCanvas().toDataURL()` download link |

---

## Implementation Roadmap

**Week 1-2: Performance (Critical)**
1. Move overview generation to background worker
2. Add ProcessPoolExecutor for tile rendering
3. Add request deduplication
4. Restrict CORS in production

**Week 3-4: Core Features (High Impact)**
1. URL state sync
2. Pixel value inspection
3. Colorbar/legend
4. Time series animation

**Week 5-6: Polish & Reliability**
1. Service worker / PWA caching
2. Frontend unit tests
3. Coordinate display, scale bar, fullscreen
4. Mobile responsiveness

**Week 7-8: Advanced Features**
1. AOI drawing tools
2. Split-screen comparison
3. Histogram endpoint
4. Export to GeoTIFF
