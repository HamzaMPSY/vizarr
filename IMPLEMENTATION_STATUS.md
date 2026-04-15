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
- [x] Built a startup dataset manifest/index so `/api/datasets` no longer triggers first-request bucket traversal.
- [x] Split lazy dataset metadata hydration from bounds/coordinate hydration so `/datasets/{id}/variables` stays lighter than full dataset detail loads.
- [x] Wired frontend map auto-fit to backend-provided dataset bounds.
- [x] Added app-lifetime OCI metadata and chunk byte caches plus filesystem reuse to reduce repeated object-store reads.
- [x] Updated the parquet-to-zarr tool to write explicit Zarr v3 output by default.
- [x] Updated the parquet-to-zarr tool to write viewer-compatible 4D `bands(time, band, y, x)` stores by default.
- [x] Hardened projected dataset metadata hydration so band labels can fall back to stored metadata when Zarr v3 string coord encoding differs.

## Not Done Yet

- [x] Performance pass 1: split fast dataset index from lazy per-dataset detail loads.
- [x] Performance pass 2: expose dataset bounds and auto-fit the map to the selected scene.
- [ ] Performance pass 3: add a generated manifest for `cubes/` so interactive requests never depend on live bucket scans.
- [x] Performance pass 4: add stronger OCI/object-level caching for metadata and chunk reads.
- [ ] Performance pass 5: add Zarr v3 `sharding_indexed` support with shard-index parsing and inner-chunk range reads.
- [ ] Performance pass 6: write Vizarr-native sharded Zarr v3 stores instead of plain per-chunk objects.
- [ ] Performance pass 7: add multiscale overview arrays and route low zoom tile requests to them.
- [ ] Performance pass 8: adopt standard spatial metadata conventions (`proj`, `spatial`, `multiscales`) instead of relying only on `spatial_ref`.
- [ ] Full browser-level verification that discovered OCI datasets appear in the frontend picker and render tiles on the map.
- [ ] Robust handling for Zarr v3 stores beyond the Landsat-style `bands/time/y/x` shape.
- [ ] Support direct viewing of 3D `(time, y, x)` projected stores without requiring banded conversion.
- [ ] RGB composite rendering for multiband imagery.
- [ ] Better discovery recursion and pagination across large bucket prefixes.
- [ ] Dask-backed lazy chunk execution.
- [ ] WebSocket dataset invalidation flow.
- [ ] Predictive prefetch worker.
- [ ] Advanced sidebar controls like time slider, legend, and range overrides.
- [ ] Nginx disk tile cache and production hardening.
- [ ] End-to-end browser verification in a fully installed dependency environment.

## Ordered Task Queue

1. [x] Manifest-backed dataset index
   Make `/api/datasets` serve a precomputed manifest from app state instead of triggering live bucket traversal on the request path.
2. [x] Lazy dataset detail hydration
   Keep manifest entries lightweight and only load band metadata, coordinate arrays, and sample stats when a specific dataset is selected.
3. [x] Frontend map auto-fit from dataset bounds
   Use backend-provided bounds so the map lands on the actual projected scene immediately.
4. [x] Stronger OCI metadata and chunk caching
   Cache `zarr.json`, coordinate arrays, and repeated chunk reads to reduce OCI round trips.
5. [ ] Generalize projected imagery support beyond Landsat-style `bands/time/y/x`
   Remove Landsat-only assumptions from catalog hydration and tile generation.
6. [ ] Add Zarr v3 shard-aware reads for fast remote streaming
   Parse `sharding_indexed` metadata, read shard indexes, and issue byte-range reads for only the required inner chunks instead of reading whole logical chunk objects.
7. [ ] Update the writer to emit a Vizarr-native sharded Zarr v3 layout
   Extend `parquet_to_zarr` so output stores use shard containers plus small inner chunks tuned for tile access rather than plain chunk objects.
8. [ ] Add multiscale overview arrays for low zoom rendering
   Generate lower-resolution preview levels and route low zoom requests to those levels instead of reprojecting full-resolution source windows every time.
9. [ ] Adopt shared spatial metadata conventions
   Emit and consume `proj`, `spatial`, and `multiscales`-style metadata so stores are easier to interoperate with external tooling.
10. [ ] Browser verification for discovered OCI datasets
   Prove that the frontend picker, bounds, and tile rendering work end-to-end in OCI mode.
11. [ ] Support direct projected 3D stores in the viewer
   Allow OCI datasets with `(time, y, x)` variables to render without converting to a `bands` cube first.

## In Progress

- [ ] Task 6: Add Zarr v3 shard-aware reads for fast remote streaming
  Current state:
  - Manifest-backed dataset listing and split detail hydration are in place.
  - OCI metadata text reads, chunk-byte reads, and filesystem creation now reuse app-lifetime caches.
  - `parquet_to_zarr` now writes viewer-compatible `bands(time, band, y, x)` output by default, with band-name metadata fallback during catalog hydration.
  - The 1D coordinate/label loader now reassembles multi-chunk arrays instead of assuming the full coordinate vector lives in chunk `0`.
  Target:
  - Support `sharding_indexed` arrays in the manual Zarr v3 reader.
  - Read shard indexes and fetch only the referenced inner chunk payload via byte ranges.
  - Keep Redis/WebP tile caching as an outer cache, but stop depending on full-object reads on the hot path.
- [ ] Task 7: Update the writer to emit a Vizarr-native sharded Zarr v3 layout
  Current state:
  - The converter now treats `chunk-size` as the inner read chunk size and adds an explicit `shard-size` for Zarr v3 output.
  - Dense `to_zarr` writes now prepare Zarr v3 encoding with both `chunks` and `shards`.
  - Sparse writes now group and write by shard-sized windows instead of chunk-sized windows.
  - Output-store consolidation is now part of the write path so metadata stays cheap to open.
  Target:
  - Verify the emitted Zarr v3 metadata and codecs against a real converted store.
  - Confirm the reader hot path can consume the produced sharded stores end to end.
  - Tune the default inner chunk and shard sizes against real tile latency.

## Tessera Comparison

### External reference

- Reviewed:
  - `https://anil.recoil.org/notes/tessera-zarr-v3-layout`
  - follow-up note `https://anil.recoil.org/notes/tessera-embeddings-convention`
  - linked Zarr v3 sharding and consolidated metadata references

### Stable takeaways from Tessera

- Fast streaming depends on Zarr v3 object layout, not only on tile endpoint caching.
- The key storage idea is:
  - large shard objects
  - small inner chunks inside each shard
  - shard index lookup
  - byte-range reads for only the needed inner chunk payload
- Consolidated metadata remains important so opening a store does not require many metadata object fetches.
- Low zoom rendering should come from multiscale overview arrays instead of repeatedly sampling from the full-resolution source.
- Spatial metadata should follow shared conventions rather than tool-specific ad hoc attributes.

### What Vizarr already has

- Explicit Zarr v3 metadata parsing for discovered OCI stores.
- A projected raster tile path that reads data windows and returns WebP image tiles.
- Redis-backed tile caching on the rendered-image side.
- App-lifetime caching for OCI metadata text and repeated chunk byte reads.
- Basic consolidated metadata use when root `zarr.json` includes it.
- A writer path that now targets sharded Zarr v3 layout rather than plain chunked v3 output.

### What Vizarr is missing compared with Tessera

- No support for Zarr v3 `sharding_indexed`.
- No shard index parsing and no inner-chunk offset/length resolution.
- No byte-range reads on the OCI hot path; current reads use full `cat_file()` object fetches.
- No multiscale overview pyramid for low zoom requests.
- No shared `proj` / `spatial` / `multiscales` convention support in catalog hydration or writing.
- No verified end-to-end sharded writer/read pipeline against a real converted store yet.

### Important nuance for Vizarr

- Tessera's exact updated embedding layout should not be copied mechanically.
- The transferable idea is:
  - sharding
  - range-addressable inner chunks
  - multiscale previews
  - standard metadata conventions
- The exact chunk packing for Vizarr should still be optimized for image tile rendering, not embedding-vector retrieval.

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
- The current manual Zarr v3 reader supports per-object chunk loading and basic codec decoding, but not `sharding_indexed`.
- The current OCI connector reads chunk payloads with full-object `cat_file()` fetches and does not issue byte-range reads.
- The current writer code now prepares sharded Zarr v3 layout with explicit inner chunks, explicit shard size, and metadata consolidation.
- The current frontend renders server raster tiles but does not yet implement the predictive prefetch worker described in the docs.

## Current Notes

- The backend defaults to synthetic data so the app is immediately runnable.
- OCI mode now supports discovery and metadata inspection without requiring a single preconfigured Zarr path.
- Projected OCI tiles now read Zarr v3 chunks directly through `ocifs` and the manual reader path rather than relying on `zarr.open_group()` for the discovered imagery stores.
- The current slowness is dominated by interactive OCI request amplification, not only by raw object-store throughput.
- The first optimization track is now:
  - fast dataset index from object listing only
  - lazy variable and coordinate materialization per selected dataset
  - bounds-aware map positioning
  - manifest-first discovery for `cubes/`
- Creative solutions likely needed after the first fixes:
  - shard-aware Zarr v3 reads
  - a sharded writer layout tuned for Vizarr access
  - low-zoom overview tiles or a derived raster pyramid
  - shared spatial metadata conventions for interoperability
- The implementation is intentionally narrower than the docs: it proves the end-to-end shape before the generalized cloud-backed viewer is added.
- The production compose stack is standard and should work with `docker compose` and `podman compose`.
- The development stack is the preferred workflow while iterating on backend/frontend logic.

## Performance Bottlenecks

- Live bucket traversal on interactive requests is too expensive.
- Reading per-store `zarr.json` and coordinate arrays during `/api/datasets` is too expensive.
- The frontend can request or wait for tiles while centered far away from the real projected scene.
- Dynamic tile generation still pays for remote chunk fetches, window extraction, resize, and colorization on demand.
- Full-object remote chunk reads amplify latency and transferred bytes when the ideal access pattern only needs a shard index plus one inner chunk payload.
- Lack of multiscale overview arrays forces low zoom requests to touch higher-resolution source data than necessary.
- Missing shared metadata conventions make cross-tool layout assumptions harder and keep the reader/writer logic more custom than it should be.

## Active Optimization Plan

1. Make `/api/datasets` return a lightweight dataset index quickly.
2. Load variables and coordinates only for the selected dataset.
3. Expose and use dataset bounds so the map lands on the scene immediately.
4. Add a generated manifest and stronger caching once the interactive path is correct.
5. Add shard-aware Zarr v3 reads with byte-range access on OCI.
6. Update the writer to emit a Vizarr-native sharded layout.
7. Add multiscale overview levels and route low zoom requests to them.
8. Adopt shared spatial metadata conventions in both the reader and writer.
