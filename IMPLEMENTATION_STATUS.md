# Vizarr Implementation Status

## Goal

Deliver a runnable proof of concept for macOS that starts with either Podman or Docker and demonstrates the core Vizarr flow:
- backend tile API
- dataset discovery
- frontend map rendering
- Redis-backed tile caching
- OCI Object Storage connectivity
- reverse proxy entrypoint for production-style runs

## Done

- [x] Reviewed the architecture, backend, frontend, performance, and build docs.
- [x] Defined the first POC scope around synthetic data so the stack runs without cloud credentials.
- [x] Scaffolded a FastAPI backend with dataset, variable, colormap, health, and tile endpoints.
- [x] Added synthetic Xarray dataset generation for the demo flow.
- [x] Added Redis-backed tile caching with graceful fallback when Redis is unavailable.
- [x] Added backend tests for health, dataset discovery, and tile responses.
- [x] Scaffolded a React + TypeScript + Vite frontend.
- [x] Added a Deck.gl + MapLibre map with sidebar-driven dataset and variable selection.
- [x] Added container files for backend, frontend, nginx, and Compose.
- [x] Added run instructions for Docker Compose and Podman Compose.
- [x] Added a development compose workflow with hot reload for backend and frontend.
- [x] Added the first OCI Object Storage connector path using OCI SDK listing plus `fsspec`/`ocifs` access for Zarr stores.
- [x] Wired Oracle-friendly container build settings for internal npm registry and proxy/TLS overrides.
- [x] Added dev-container support for mounting the local OCI session profile from `~/.oci`.
- [x] Proved OCI authentication from the backend container using local profile `prof`.
- [x] Added object listing, prefix listing, Zarr store discovery, Zarr inspection, and raw `zarr.json` inspection endpoints.
- [x] Proved that `cubes/landsat/LC08_L1TP_202037_20260117_20260122_02_T1.zarr` is a Zarr v3 store with inline consolidated metadata.
- [x] Extracted dataset metadata and band-level variable choices from discovered Zarr v3 stores under `cubes`.
- [x] Added a projected raster tile path for per-band visualization from discovered OCI stores.
- [x] Replaced the unsupported `zarr.open_group()` path with explicit Zarr v3 metadata plus chunk decoding for projected OCI imagery.
- [x] Hardened OCI catalog discovery so unreadable or partially written stores are skipped instead of breaking `/api/datasets` or `/api/tiles`.
- [x] Verified a live OCI Landsat band tile request returns `200 image/webp` from the backend container.

## Not Done Yet

- [ ] Performance pass 1: split fast dataset index from lazy per-dataset detail loads.
- [ ] Performance pass 2: expose dataset bounds and auto-fit the map to the selected scene.
- [ ] Performance pass 3: add a generated manifest for `cubes/` so interactive requests never depend on live bucket scans.
- [ ] Performance pass 4: add stronger OCI/object-level caching for metadata and chunk reads.
- [ ] Full browser-level verification that discovered OCI datasets appear in the frontend picker and render tiles on the map.
- [ ] Robust handling for Zarr v3 stores beyond the Landsat-style `bands/time/y/x` shape.
- [ ] RGB composite rendering for multiband imagery.
- [ ] Better discovery recursion and pagination across large bucket prefixes.
- [ ] Dataset bounds extraction and map auto-fit for projected imagery.
- [ ] Dask-backed lazy chunk execution.
- [ ] WebSocket dataset invalidation flow.
- [ ] Predictive prefetch worker.
- [ ] Advanced sidebar controls like time slider, legend, and range overrides.
- [ ] Nginx disk tile cache and production hardening.
- [ ] End-to-end browser verification in a fully installed dependency environment.

## Proven Facts

- The synthetic POC is runnable in dev mode and serves visible tile previews.
- The frontend dev workflow uses Vite on `5173`; the backend dev workflow uses FastAPI on `8001`.
- OCI local-development auth works by mounting the host `~/.oci` directory into the backend container and using profile `prof`.
- The `STAY` bucket in namespace `lrdwfp6kyp5x` is reachable from the backend.
- `cubes/` is a prefix, not a Zarr store root.
- At least one discovered real store exists:
  - `cubes/landsat/LC08_L1TP_202037_20260117_20260122_02_T1.zarr`
- That store is Zarr v3 and contains:
  - array `bands`
  - coordinates `band`, `time`, `x`, `y`
  - CRS `EPSG:32629`
  - chunk shape `[1, 1, 512, 512]`

## Current Notes

- The backend defaults to synthetic data so the app is immediately runnable.
- OCI mode now supports discovery and metadata inspection without requiring a single preconfigured Zarr path.
- Projected OCI tiles now read Zarr v3 chunks directly through `ocifs` because the current backend image uses `zarr==2.18.3`, which cannot open these stores as groups.
- The current slowness is dominated by interactive OCI request amplification, not only by raw object-store throughput.
- The first optimization track is now:
  - fast dataset index from object listing only
  - lazy variable and coordinate materialization per selected dataset
  - bounds-aware map positioning
  - manifest-first discovery for `cubes/`
- Creative solutions likely needed after the first fixes:
  - precomputed dataset manifest
  - low-zoom overview tiles or a derived raster pyramid
  - Zarr v3 sharding or another reduced-object-count visualization layout
- The implementation is intentionally narrower than the docs: it proves the end-to-end shape before the generalized cloud-backed viewer is added.
- The production compose stack is standard and should work with `docker compose` and `podman compose`.
- The development stack is the preferred workflow while iterating on backend/frontend logic.

## Performance Bottlenecks

- Live bucket traversal on interactive requests is too expensive.
- Reading per-store `zarr.json` and coordinate arrays during `/api/datasets` is too expensive.
- The frontend can request or wait for tiles while centered far away from the real projected scene.
- Dynamic tile generation still pays for remote chunk fetches, window extraction, resize, and colorization on demand.

## Active Optimization Plan

1. Make `/api/datasets` return a lightweight dataset index quickly.
2. Load variables and coordinates only for the selected dataset.
3. Expose and use dataset bounds so the map lands on the scene immediately.
4. Add a generated manifest and stronger caching once the interactive path is correct.
