# Frontend

The frontend is a React + TypeScript + Vite app. The active viewer renders
backend TileJSON as a MapLibre raster source/layer when browser paths are not
eligible. Compatible generated multiscale sidecars can render through a
browser-native MapLibre image source or a deck.gl shader-colormap overlay.

## Current folder structure

```
frontend/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── components/
│   │   ├── DeckRasterOverlay.tsx
│   │   ├── MapView.tsx
│   │   ├── Sidebar.tsx
│   │   └── TileDebugOverlay.tsx
│   ├── api/
│   │   └── endpoints.ts
│   ├── hooks/
│   │   ├── useBrowserMultiscale.ts
│   │   ├── useDeckZarrRaster.ts
│   │   ├── useDatasetInvalidation.ts
│   │   ├── useDatasets.ts
│   │   ├── useDebouncedValue.ts
│   │   ├── useShareableUrlState.ts
│   │   └── useTilePrefetch.ts
│   ├── lib/
│   │   ├── displayRange.ts
│   │   ├── gpuRaster.ts
│   │   ├── multiscale.ts
│   │   └── temporal.ts
│   ├── store/
│   │   └── mapStore.ts
│   ├── styles.css
│   └── types.ts
├── index.html
├── package.json
├── vite.config.ts
└── Dockerfile
```

There is no Deck.gl `TileLayer` in the current code. The MapLibre path now has
a debounced prefetch hook rather than a dedicated Web Worker.
`DeckRasterOverlay` mounts the interleaved deck.gl overlay without removing the
current MapLibre raster fallback.

## Application flow

`src/App.tsx` loads datasets and variables through TanStack Query, selects the
first available dataset/variable, and syncs default display range/colormap from
the selected variable metadata. It also subscribes to `/ws/datasets` and shows a
compact dataset-sync status chip over the map. `useShareableUrlState` hydrates
valid deep-link parameters before normal defaults win, keeps the URL synchronized
with the active view through `history.replaceState`, and reports stale shared
parameters as non-blocking sidebar warnings. API failures caused by expired or
unavailable OCI auth surface as a compact, dismissible storage-session issue so
operators do not have to infer the problem from blank map state alone.

`src/components/Sidebar.tsx` exposes dataset, render mode, variable/composite,
time, playback, colormap, and display-range controls. Display-range edits stay
in local draft state until the user applies a valid `min < max` pair, resets to
auto, or chooses a metadata/viewport percentile preset. It can also narrow the dataset dropdown to
records intersecting the current MapLibre viewport by calling
`GET /api/datasets?bbox=...`. Composite mode is shown only when the selected
dataset advertises `composite_styles`; scalar colormap/range controls are
disabled in composite mode because RGB composites use their band stretches.

`src/components/MapView.tsx`:

- requests TileJSON for the active dataset and selected band or composite style;
- fits the map to TileJSON bounds when the active layer changes;
- adds a MapLibre raster source using the TileJSON tile template;
- attempts browser-native multiscale rendering when the serving profile and
  multiscale metadata are explicitly compatible;
- prefers the deck.gl browser-GPU overlay for compatible generated multiscale
  sidecars;
- falls back to browser-native or server-rendered tiles when GPU rendering is
  not ready, unsupported, too large, or fails;
- publishes debounced viewport bounds for browser-native reads and optional
  dataset-list filtering;
- debounces viewport-driven prefetch work so it does not run on every move
  frame;
- shows guards when the current zoom is below the data/detail zoom;
- renders a single-band colorbar legend from `/api/colormaps/{name}/palette`
  with the active display min/max labels;
- samples sanitized tile response headers through the existing prefetch path and
  shows compact tile issue banners for budget, auth/session, repeated failure,
  browser-GPU fallback, and missing optimized-artifact states;
- renders an opt-in `?debug=1` diagnostics overlay with recent server tile
  samples and browser-native/GPU fallback status;
- tracks tile loading through MapLibre `dataloading`, `idle`, and `error`
  events.

## State and data

`src/store/mapStore.ts` holds tile-affecting state:

- active dataset id;
- active variable id;
- render mode, either `band` or `composite`;
- active composite style id;
- time index;
- temporal playback state, speed, and loop preference;
- colormap;
- vmin/vmax;
- display range mode, either `auto`, `seeded`, or `manual`;
- current map view state.
- whether the first map camera was restored from a shared URL.

The frontend distinguishes manual display ranges from tile-header seeded ranges.
`setRangeFromTileHeaders` can populate a seeded range only while the range mode
is not manual. Resetting the sidebar range returns to auto mode and removes
explicit `vmin`/`vmax` from TileJSON/tile URL construction until a later seed or
manual apply.

Temporal controls use dataset `time_values` as timestamp labels when available.
The user can scrub, step backward/forward, play/pause, choose a conservative
speed, and toggle loop behavior. Playback is disabled when only one time step is
available or when an OCI dataset is renderable only through direct dynamic
tiles; manual scrubbing remains available so users can inspect a specific step.

Share URLs preserve dataset, variable or composite style, render mode, time
index, colormap, display range, and map camera (`lon`, `lat`, `zoom`, `pitch`,
and `bearing`). The copy command builds a clean same-origin URL and omits
private `api_key` query values by default. Set `VITE_SHARE_API_KEY_IN_URL=true`
only for environments that explicitly want the frontend API key embedded in
shared URLs.

Dataset metadata types include optional `native_resolution_m`, `crs_wkt`, and
`crs_authority` fields. The current UI does not render CRS details directly, but
the fields are available to future inspectors and diagnostics.

`src/hooks/useDatasets.ts` wraps these API calls:

- datasets;
- variables;
- colormaps;
- colormap palettes;
- TileJSON;
- serving profiles.

`src/hooks/useDatasetInvalidation.ts` opens `/ws/datasets`, listens for
`datasets.invalidate` events, and invalidates dataset, variable,
serving-profile, and TileJSON query roots in TanStack Query.

`src/api/endpoints.ts` centralizes URL construction for:

- `/api/datasets`
- `/api/datasets/{dataset_id}`
- `/api/datasets/{dataset_id}/variables`
- `/api/datasets/{dataset_id}/serving-profile`
- `/api/tilejson/{dataset_id}/{variable}`
- `/api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`
- `/api/query/point`
- `/api/query/bbox`
- `/api/query/range`
- `/api/colormaps`
- `/api/colormaps/{name}/palette`
- `/ws/datasets`

`GET /api/query/range` is requested only on explicit user action from the range
panel. With the current viewport bbox it samples a bounded source window and
returns min/max, p02/p98, and histogram counts; without a bbox it can return
metadata-level stats. The sidebar applies sampled p02/p98 only after the request
completes successfully.

The sidebar tile preview labels rendered WebP tiles as visual map products and
points scientific inspection/export workflows at the source readback APIs. This
keeps the UI from implying that tile pixels are exact source values.

## Tile error and debug UI

`src/hooks/useTilePrefetch.ts` reports sanitized tile diagnostics while it warms
nearby URLs. The records include HTTP status, XYZ indices, cache status,
representation, execution path, timing headers, object-read counts, byte-range
counts, chunk counts, and direct-budget headers. Query strings, API keys, OCI
object paths, and credentials are not stored or rendered by the frontend debug
surface.

`MapView` combines those sampled server-tile diagnostics with MapLibre errors
and browser-GPU failure state. Normal users see at most one compact,
dismissible banner when a likely actionable issue occurs: direct tile compute
budget exceeded, storage auth/session failure, repeated tile failures,
browser-GPU fallback, or missing browse/multiscale artifacts that force server
tiles. Developers can append `?debug=1` to show the map overlay with recent
tile samples plus browser-native and browser-GPU status. The overlay remains
hidden by default and is preserved when the shareable URL sync rewrites map
state.

## Browser-native multiscale status

`src/lib/multiscale.ts` can load consolidated multiscale metadata, select a
level, read uncompressed float32 chunks through the dataset-scoped multiscale
proxy, compose a plane or viewport window, and render it to a data URL.

`src/hooks/useBrowserMultiscale.ts` gates the path with the serving profile,
Zarr format/consolidation flags, chunk-layout availability, level compressor and
filter metadata, pixel/chunk/byte limits, and profile gaps. When the selected
level fits the browser-native budgets, the hook loads the full level. When the
level is larger, it clips reads to the current MapLibre viewport bounds and
fetches only intersecting chunks through a bounded async queue. Otherwise, it
keeps the server TileJSON raster layer active.

The metadata and chunk fetches consume TanStack Query's `AbortSignal`, so stale
browser-side multiscale reads are canceled when the viewport, dataset, variable,
time index, colormap, or display range changes.

Current browser-native support is intentionally strict:

- supported: consolidated Zarr v2 metadata, uncompressed float32 (`<f4`),
  C-order arrays, no filters, one time step and one band per chunk, and
  `256x256` spatial chunks;
- rejected with explicit fallback reasons: compressed chunks such as Blosc,
  Zstd, gzip/zlib, non-empty filters, non-float32 dtypes, Fortran-order arrays,
  multi-time or multi-band chunks, and non-`256x256` spatial chunks.

`MapView` exposes the active rendering decision on `.map-shell` data attributes
for debugging and Playwright checks:

- `data-render-mode`: `browser-gpu`, `browser-native`, or `server-tiles`;
- `data-browser-native-status`: `native`, `native-loading`, or `fallback`;
- `data-browser-native-mode`: `full-level`, `viewport-window`, or `none`;
- selected dataset, variable/style, time index, and current map zoom
  attributes;
- pixel, chunk, byte, and concurrency budget attributes.

## Browser-GPU rendering

The deck.gl renderer is a third visible rendering mode, separate from the
MapLibre TileJSON path and the browser-native image-source path.
`src/components/DeckRasterOverlay.tsx` provides the interleaved
MapLibre/deck.gl shell. `src/hooks/useDeckZarrRaster.ts` enables the first
deck.gl slice only when the dataset serving profile and multiscale metadata
prove that the generated sidecar can be read safely by the browser.

The first GPU-compatible sidecar profile is intentionally narrow:

- generated multiscale Zarr v2, not a raw source store;
- consolidated metadata;
- dimensions `time`, `band`, `y`, and `x`;
- `float32`, C-order arrays;
- no compressor and no filters;
- chunks `[1, 1, 256, 256]`;
- level bounds, browse zoom mapping, CRS/transform metadata where available,
  and the data array name exposed through the serving profile or consolidated
  metadata.

The current GPU slice renders prepared browser multiscale planes through
deck.gl layers. `ZarrColormapBitmapLayer` uploads one raw `r32float` value
texture plus a palette texture, applies `vmin`/`vmax` normalization and palette
lookup in the fragment shader, and preserves nodata transparency.
`ZarrCompositeBitmapLayer` uploads RGB/false-color composite bands as three raw
`r32float` textures and applies per-band metadata stretches in shader code. The
MapLibre image-source fallback still uses generated CPU data URLs for
single-band rendering.

Fallbacks are part of the contract. If a dataset is synthetic-only, lacks a
multiscale sidecar, has compressed or filtered chunks, exceeds browser budgets,
fails a request, or is missing required metadata, the viewer must use
server-rendered TileJSON tiles instead of leaving a blank map. The debug surface
should grow to include:

- `data-render-mode`: `browser-gpu`, `browser-native`, or `server-tiles`;
- `data-browser-gpu-status`: `native`, `native-loading`, or `fallback`;
- `data-browser-gpu-ready`: whether the serving profile reports a compatible
  sidecar;
- `data-browser-gpu-renderer`: `raw-float-shader-colormap` or
  `raw-float-composite`;
- `data-browser-gpu-reason`: the eligibility or fallback reason;
- `data-browser-gpu-max-texture-dimension` and
  `data-browser-gpu-failure-fallback-threshold`: browser-GPU guardrails used by
  probes and debugging;
- `data-browser-gpu-failure-count` and `data-browser-gpu-last-error`: runtime
  deck.gl failures for the active raster attempt.

`Sidebar` includes an optional `Country borders` layer toggle. When enabled,
`MapView` adds a MapLibre GeoJSON source for Natural Earth
`ne_110m_admin_0_boundary_lines_land.geojson` and renders two line layers above
the raster data: a dark casing line plus a light border line. The source is not
requested until the toggle is enabled.

## Prefetch and range seeding

`src/hooks/useTilePrefetch.ts` runs from debounced viewport state. It schedules
speculative work through `requestIdleCallback` when available, asks
`src/workers/tilePrefetchPlanner.worker.ts` to build the tile plan off the main
thread, and falls back to the same planner on the main thread only when workers
are unavailable.

The planner prioritizes:

- the current center tile;
- tiles ahead of the recent pan direction;
- zoom-in child tiles or a zoom-out parent tile when zoom intent is detected;
- the remaining radius-2 ring.

The hook cancels stale plans and fetches whenever the viewport, tile template,
zoom range, colormap, display range, dataset, variable, or time-derived URL
changes. Prefetch uses a bounded queue: `32` queued tiles and `3` in-flight
requests by default, reduced to `8` queued tiles and `1` in-flight request for
save-data, 2G/slow-2G, or low-memory devices.

While temporal playback is active, the hook may also prefetch the next time
step with a reduced radius-1 queue and one in-flight request. That adjacent-time
prefetch is enabled only for synthetic data, browser multiscale/GPU-ready
datasets, or browse-covered OCI datasets. Direct-only OCI serving does not
start playback-driven speculative tile work.

The first center-tile response seeds `vmin`/`vmax` from `X-Data-Vmin` and
`X-Data-Vmax` when no manual range is already set. MapLibre still owns visible
tile loading; the prefetch hook only warms nearby URLs after the viewport
settles and never blocks visible raster requests. Manual text edits do not
change TileJSON or tile URLs until Apply, Reset auto, or a range preset commits
state.

## Vite proxy

`frontend/vite.config.ts` proxies `/api` and `/ws` to:

```text
process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000"
```

The `/ws` proxy enables WebSocket upgrades for local Vite development. In
production-style compose, Nginx handles the same upgrade proxying.

## Dependencies

The active frontend depends on:

- React;
- TanStack Query;
- Zustand;
- MapLibre GL;
- `react-map-gl/maplibre`;
- `@deck.gl/mapbox` and `@deck.gl/layers` for the interleaved browser-GPU
  overlay slice;
- Vite.
