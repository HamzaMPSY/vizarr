# Deck.gl Zarr GPU Rendering Tickets

These tickets translate the deck.gl-raster/CDL study into an incremental Vizarr
implementation plan. They are written to be copied into Linear as separate
issues.

## Epic: Browser GPU Rendering For OCI Zarr Cubes

### Goal

Add a browser GPU rendering path for compatible generated multiscale Zarr
artifacts while preserving the current server-rendered WebP tile path as the
fallback.

### Non-goals

- Do not expose raw OCI credentials or writable object-store paths to the
  browser.
- Do not remove the existing `/api/tiles/...` MapLibre raster path.
- Do not make the browser read arbitrary source Zarr v3/sharded cubes directly.
- Do not port CDL-specific STAC/SAS-token/category code.

### Target Architecture

```text
OCI source Zarr v3/sharded cube
  -> backend-generated browser-friendly multiscale sidecar
  -> read-only /api/zarr/multiscale/{dataset_id}/...
  -> deck.gl interleaved overlay in MapLibre
  -> GPU texture upload + shader colormap/composite
  -> server WebP tiles if unsupported or failed
```

---

## Ticket 1: Document Browser-GPU Rendering Contract

### Problem

Vizarr has server tiles and a strict browser-native multiscale path, but there
is no explicit contract for a deck.gl/GPU path. Without a contract, the backend
artifact generator and frontend renderer can drift.

### Scope

- Add a docs section describing the browser-GPU rendering contract.
- Define the required multiscale artifact shape and metadata.
- Define fallback behavior and observability attributes.

### Proposed Files

- `docs/architecture.md`
- `docs/frontend.md`
- `docs/performance.md`
- `docs/compatibility.md`

### Requirements

- Browser GPU path uses generated multiscale sidecar stores, not raw source
  cubes.
- Initial compatible artifact profile:
  - Zarr v2
  - consolidated metadata
  - C-order arrays
  - `float32`
  - no compressor
  - no filters
  - dimensions `time, band, y, x`
  - chunks `[1, 1, 256, 256]`
  - WGS84 or Web Mercator bounds per level
  - stable `multiscales` metadata with level paths
- Existing server tile path remains the default fallback.
- Rendering mode should be observable from DOM attributes or equivalent debug
  state:
  - `server-tiles`
  - `browser-native-canvas`
  - `browser-gpu`

### Acceptance Criteria

- Docs explain when the GPU path is eligible and when it must fall back.
- Docs explicitly say raw OCI source stores remain protected behind read-only
  backend routes.
- Docs identify which backend metadata fields drive frontend eligibility.
- Docs identify validation commands for backend and frontend changes.

### Validation

- Documentation-only review.

---

## Ticket 2: Normalize Multiscale Metadata For Deck.gl Compatibility

### Problem

The current multiscale sidecar is designed for Vizarr's custom
`useBrowserMultiscale` loader. Deck.gl Zarr rendering will need predictable
metadata for level bounds, dimensions, chunk layout, dtype, CRS, and array name.

### Scope

- Audit generated multiscale metadata from `backend/app/core/multiscale_builder.py`.
- Ensure generated Zarr v2 sidecars expose stable metadata needed by a
  browser-side tile/chunk renderer.
- Extend serving profiles with any missing fields needed by the frontend GPU
  renderer.

### Proposed Files

- `backend/app/core/multiscale_builder.py`
- `backend/app/core/multiscale_store.py`
- `backend/app/core/serving_profile.py`
- `backend/app/models/dataset.py`
- `backend/tests/test_serving_profile.py`
- `backend/tests/test_zarr_v3.py` or new focused metadata tests

### Requirements

- Generated sidecar levels must expose:
  - level path
  - data array name
  - shape
  - chunk shape
  - dtype
  - compressor/filter status
  - bounds
  - browse zoom mapping
  - CRS/transform metadata where available
- Serving profile should expose enough normalized information for the frontend
  to decide between server tiles and GPU rendering without fetching large data.
- Existing `browser_multiscale_ready` behavior must not regress.
- Synthetic mode must keep working.

### Acceptance Criteria

- Backend tests cover a compatible sidecar profile.
- Backend tests cover at least one incompatible profile and report a useful
  gap reason.
- `/api/datasets/{dataset_id}/serving-profile` includes all fields needed by
  the GPU renderer.
- Existing browser-native canvas fallback remains compatible with the metadata.

### Validation

```bash
cd backend && pytest tests/test_serving_profile.py -q
cd backend && pytest tests -q
```

---

## Ticket 3: Add Deck.gl MapLibre Overlay Shell

### Problem

The current viewer renders backend TileJSON through MapLibre `Source`/`Layer`.
To render chunks with GPU shaders, Vizarr needs an interleaved deck.gl overlay
that can coexist with MapLibre labels and the existing fallback raster layer.

### Scope

- Add a reusable deck.gl overlay component for MapLibre.
- Insert deck.gl below the first symbol layer when possible.
- Keep the current server tile layer available and active when GPU rendering is
  unavailable.

### Proposed Files

- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/src/components/MapView.tsx`
- `frontend/src/components/DeckRasterOverlay.tsx` or similar
- `frontend/src/types.ts`

### Requirements

- Add `@deck.gl/mapbox` if it is not already installed.
- Keep existing MapLibre basemap and country borders behavior.
- Do not render both GPU raster and server tile raster at the same time for the
  same dataset/variable unless explicitly in debug mode.
- Add stable debug attributes on `.map-shell`, for example:
  - `data-render-mode="browser-gpu"`
  - `data-browser-gpu-status`
  - `data-browser-gpu-reason`
- Overlay must clean up on unmount and dataset/variable changes.

### Acceptance Criteria

- Frontend can mount a deck.gl overlay without changing visible behavior when
  no GPU layer is provided.
- Existing TileJSON server tiles still render.
- Existing browser-native image-source path still falls back cleanly.
- No TypeScript errors.

### Validation

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

---

## Ticket 4: Prototype GPU Single-Band Zarr Renderer

### Problem

Vizarr's browser-native path currently reads compatible multiscale Zarr chunks,
CPU-colormaps them into a canvas PNG data URL, and gives that image to
MapLibre. That avoids backend tile rendering but still does substantial CPU
work and creates large image URLs.

### Scope

- Implement an experimental GPU renderer for one compatible single-band
  variable.
- Reuse the existing multiscale proxy and metadata loader where practical.
- Upload visible chunk/window data as GPU textures and apply colormap/range in
  shader modules.

### Proposed Files

- `frontend/src/lib/multiscale.ts`
- `frontend/src/lib/gpuRaster.ts` or similar
- `frontend/src/hooks/useDeckZarrRaster.ts`
- `frontend/src/components/MapView.tsx`
- `frontend/src/api/endpoints.ts`
- `frontend/src/types.ts`

### Requirements

- Renderer must only enable when serving profile declares compatible
  multiscale metadata.
- Renderer must read through `/api/zarr/multiscale/{dataset_id}/...`.
- Renderer must honor:
  - selected variable/band
  - time index
  - colormap
  - `vmin`
  - `vmax`
  - nodata/NaN transparency
- Renderer must use bounded concurrency and abort stale requests on map move or
  dataset changes.
- Renderer must fall back to server tiles on unsupported dtype, compression,
  filters, layout, missing metadata, request failure, or budget exceedance.

### Acceptance Criteria

- First slice: one compatible generated multiscale dataset renders through the
  deck.gl overlay path with `BitmapLayer` fed by the existing browser-native
  prepared image.
- Browser-side metadata and chunk requests abort when viewport, dataset,
  variable, time, colormap, or display range changes supersede them.
- Completed follow-up slice: upload a scalar texture and palette texture to
  deck.gl, apply colormap in a fragment shader, and keep the data URL for the
  MapLibre fallback path.
- Completed follow-up slice: upload raw numeric Zarr windows as float textures
  and apply range normalization in shaders.
- Unsupported datasets continue to use server WebP tiles.
- User-facing controls for dataset, variable, time, colormap, and range still
  affect rendering.
- Debug attributes explain the active path and fallback reason.
- No regression in server tile rendering.

### Validation

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

Manual smoke:

```text
1. Start backend + frontend.
2. Select a compatible OCI dataset with a generated multiscale sidecar.
3. Confirm .map-shell reports browser-gpu.
4. Pan and zoom; confirm stale requests cancel and no blank map persists.
5. Select an incompatible/synthetic dataset; confirm server-tiles fallback.
```

---

## Ticket 5: Add Optional GPU Composites And Shader Utilities

### Problem

Server tiles currently support RGB/false-color composites when dataset metadata
advertises required bands. A browser GPU path must support equivalent composite
rendering before it can replace server tiles for common imagery workflows.

### Scope

- Add shader utilities for single-band colormap and RGB/false-color composite
  rendering.
- Keep single-band GPU rendering from Ticket 4 as the baseline.
- Add composite support only for datasets whose multiscale sidecar includes the
  required bands and layout.

### Proposed Files

- `frontend/src/lib/gpuRaster.ts`
- `frontend/src/hooks/useDeckZarrRaster.ts`
- `frontend/src/components/Sidebar.tsx`
- `frontend/src/components/MapView.tsx`

### Requirements

- Single-band shader:
  - sample float texture
  - normalize by `vmin/vmax`
  - sample palette LUT
  - transparent nodata/NaN
- Composite shader:
  - sample R/G/B bands from compatible chunks
  - apply per-band or shared stretch based on existing metadata/control model
  - output RGB with alpha
- Do not apply single-band colormap controls to composite rendering unless the
  UI explicitly says so.
- Keep all texture sampling nearest or otherwise appropriate for the data type.

### Acceptance Criteria

- Completed first slice: advertised composite styles can render through GPU path when their
  required bands are present in the sidecar.
- Single-band rendering remains unchanged.
- Composite path falls back to server tiles with a clear reason if a required
  band is missing or chunk layout is incompatible.

### Validation

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

Manual smoke:

```text
1. Select RGB or false-color composite on a compatible dataset.
2. Confirm GPU path renders expected color ordering.
3. Toggle back to single-band and confirm colormap/range controls still work.
4. Confirm fallback on a dataset without composite metadata.
```

---

## Ticket 6: Add Browser-GPU Performance Benchmark And Smoke Probe

### Problem

Vizarr already has benchmark scripts for OCI/server tile behavior. The new GPU
path needs its own validation so we can prove it is faster and detect fallback
regressions.

### Scope

- Extend browser smoke/probe scripts to detect `browser-gpu`.
- Add benchmark output fields for GPU path readiness and fallback reason.
- Compare server tiles, current browser-native canvas path, and new browser GPU
  path where possible.

### Proposed Files

- `scripts/browser_multiscale_probe.cjs`
- `scripts/oci_performance_benchmark.py`
- `docs/performance-baselines.md`
- `docs/performance.md`
- `.github/workflows/ci.yml` only if a lightweight non-OCI check is possible

### Requirements

- Probe must record:
  - active render mode
  - GPU status/reason
  - first visible paint or best available proxy timing
  - failed request count if observable
  - selected dataset/variable/time/zoom
- Benchmark must skip cleanly when OCI credentials or compatible sidecars are
  unavailable.
- No benchmark should require raw OCI credentials in the browser.

### Acceptance Criteria

- Completed first slice: browser probe can assert a mocked `browser-gpu` path and
  records GPU status, readiness, renderer, selected dataset/variable/time/zoom,
  best available render timing, and failed request count.
- Completed first slice: OCI benchmark report summarizes `server-tiles`,
  `browser-native`, and `browser-gpu` readiness/active state and preserves
  explicit skip behavior when OCI datasets or credentials are unavailable.
- Benchmark report distinguishes:
  - `server-tiles`
  - `browser-native`
  - `browser-gpu`
- A failed GPU attempt records the fallback reason.
- Docs describe how to run the benchmark locally.
- Existing OCI benchmark behavior does not regress.

### Validation

```bash
cd frontend && npm run type-check
cd frontend && npm run build
python3 scripts/oci_performance_benchmark.py --output .cache/benchmarks/oci-benchmark.json
```

The benchmark may skip when OCI auth or compatible artifacts are unavailable;
that skip must be explicit and non-failing.

---

## Ticket 7: Production Hardening For GPU Zarr Rendering

### Problem

Once the prototype works, the GPU path needs operational guardrails: cache
behavior, request budgets, memory limits, compatibility reporting, and clean
fallbacks.

### Scope

- Add runtime budgets for browser GPU reads.
- Add clear compatibility and fallback reporting in serving profile and UI.
- Ensure backend proxy responses support browser caching and byte/range access
  needed by the renderer.

### Proposed Files

- `frontend/src/hooks/useDeckZarrRaster.ts`
- `frontend/src/lib/gpuRaster.ts`
- `frontend/src/components/MapView.tsx`
- `backend/app/api/zarr.py`
- `backend/app/core/serving_profile.py`
- `docs/performance.md`
- `docs/build.md`

### Requirements

- Frontend budgets should cover:
  - max visible chunks
  - max loaded bytes
  - max concurrent chunk requests
  - max texture dimensions
  - fallback threshold after repeated failures
- Backend proxy must preserve:
  - `GET` and `HEAD`
  - `Range`
  - ETag/cache headers
  - path traversal protection
  - auth scoping
- UI/debug state must make it obvious why a dataset is not using GPU rendering.

### Acceptance Criteria

- Completed first slice: serving profile now exposes `browser_gpu_reason` and
  `browser_gpu_gaps`; frontend debug attributes include GPU texture and fallback
  guardrails; Zarr proxy tests cover HEAD, cache validators, cache headers, and
  path traversal behavior.
- Completed second slice: deck.gl runtime errors are counted per active raster
  attempt, surfaced on `.map-shell`, and trip the configured fallback threshold
  so server tiles remain visible after repeated GPU failures.
- Completed third slice: dataset-scoped API keys now have regression coverage
  for source and multiscale Zarr proxy info/object routes, including `HEAD` and
  unauthorized/forbidden cases.
- GPU path fails closed to server tiles, not blank screen.
- No raw object-store URL or credential is exposed to the browser.
- Cache and range behavior is covered by backend tests where practical.
- Docs include operational guidance and known limitations.

### Validation

```bash
cd backend && pytest tests -q
cd frontend && npm run type-check
cd frontend && npm run build
```

---

## Suggested Implementation Order

1. Ticket 1: lock the contract.
2. Ticket 2: make sidecar metadata reliable.
3. Ticket 3: add the Deck.gl overlay shell with no behavior change.
4. Ticket 4: render one single-band compatible sidecar on GPU.
5. Ticket 6: benchmark early so performance claims are measured.
6. Ticket 5: add composites after the single-band path is stable.
7. Ticket 7: harden for production.

## Open Questions

- Should Vizarr use DevelopmentSeed `ZarrLayer` directly, or a smaller
  Vizarr-specific `RasterTileLayer` adapter over the existing multiscale proxy?
- Do we want compressed browser sidecars later, or keep the first production
  profile uncompressed for predictable browser reads?
- Should generated sidecars eventually become GeoZarr-compatible enough for
  non-Vizarr clients?
- What is the minimum acceptable benchmark delta before enabling GPU rendering
  by default for compatible datasets?
