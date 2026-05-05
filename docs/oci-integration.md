# OCI Integration

This document captures the current OCI Object Storage integration status for Vizarr.

## Current auth model

Local development:
- mount the host `~/.oci` directory into the backend container
- use OCI profile `prof`
- read `OCI_CONFIG_FILE` from the mounted config path
- use session-token-based OCI auth locally

OCI Data Flow / in-cloud execution:
- use OCI resource principals

This matches the existing `dgov_dataflow_helpers` pattern: local session profile for local runs, resource principals for Data Flow.

## Current storage mode design

`STORAGE_BACKEND` supports:
- `synthetic`
- `oci_zarr`

`synthetic`:
- generates an in-memory demo dataset
- useful for frontend and baseline backend verification

`oci_zarr`:
- authenticates with OCI
- lists objects and prefixes with the OCI SDK
- opens Zarr stores through `fsspec`/`ocifs`
- exposes discovery, inspection, and browser-facing Zarr proxy endpoints

## OCI discovery endpoints

Available backend endpoints:
- `GET /api/storage/objects`
- `GET /api/storage/prefixes`
- `GET /api/storage/zarr-stores`
- `GET /api/storage/inspect-zarr?zarr_path=...`
- `GET /api/storage/zarr-json?zarr_path=...`
- `GET /api/zarr/{dataset_id}`
- `GET /api/zarr/{dataset_id}/{object_path}`
- `GET /api/zarr/multiscale/{dataset_id}`
- `GET /api/zarr/multiscale/{dataset_id}/{object_path}`
- `GET /api/datasets/{dataset_id}/serving-profile`

Purpose:
- list raw objects and folder-like prefixes
- detect Zarr v2 and v3 store roots
- inspect variable, coordinate, and metadata structure before attempting visualization
- expose read-only dataset-scoped Zarr metadata and chunk objects through Vizarr itself

## Browser-serving metadata

For OCI-backed datasets, `GET /api/datasets` and `GET /api/datasets/{id}` now include:

- `zarr_format`
- `zarr_consolidated`
- `zarr_proxy_root`
- `multiscale_store_path`
- `multiscale_zarr_format`
- `multiscale_zarr_consolidated`
- `multiscale_proxy_root`

`zarr_proxy_root` points to the backend-served root for the dataset, for example:

- `/api/zarr/{dataset_id}`

`multiscale_proxy_root` points to the separate browser-facing store when it exists, for example:

- `/api/zarr/multiscale/{dataset_id}`

This lets frontend code discover whether a dataset can be consumed as a proxied Zarr store without embedding raw OCI paths or credentials.

`GET /api/datasets/{dataset_id}/serving-profile` now reports:

- multiscale availability
- browse overview zoom levels
- shard and inner-chunk layout
- supported rendering modes
- `browser_multiscale_ready`
- `seamless_rendering_ready`
- explicit readiness gaps

## Current Oracle/Object Storage settings

Proven working bucket context:
- namespace: `lrdwfp6kyp5x`
- bucket: `STAY`
- top-level prefix: `cubes`

Important finding:
- `cubes/` is a prefix containing candidate datasets
- it is not itself a Zarr store root

## Proven Zarr store

Validated store path:
- `cubes/landsat/LC08_L1TP_202037_20260117_20260122_02_T1.zarr`

Root metadata:
- contains `zarr.json`
- Zarr format: `3`
- consolidated metadata: inline

## Proven store structure

The Landsat store exposes:
- data array: `bands`
- dimensions: `time`, `band`, `y`, `x`
- shape: `[1, 7, 7741, 7611]`
- chunk shape: `[1, 1, 512, 512]`
- dtype: `uint16`

Coordinates:
- `band`
- `time`
- `x`
- `y`

Spatial metadata:
- projected CRS: `EPSG:32629`
- geotransform present in `spatial_ref`

## What this means for visualization

This store is not a global lat/lon scalar field.

It is:
- projected imagery
- multiband raster data
- one array with a band dimension

Therefore it needs:
- dataset discovery by store path
- band selection in the frontend
- projected tile extraction in the backend
- eventually RGB composites for natural/false color rendering

The current implementation already supports:
- discovery of Zarr v3 stores under `cubes`
- exposing band names as dataset variables
- per-band projected tile generation
- direct Zarr v3 chunk reads for projected imagery without relying on `zarr.open_group()`
- byte-range-capable proxy serving for raw Zarr metadata and chunk objects
- skipping unreadable stores during catalog build so one bad store does not fail the whole API
- a backend readiness audit for deciding whether a dataset is actually close to `zarr-vis`-style seamless rendering

## Live maize readiness

The live store `cubes/maize_2025_live4.zarr` was inspected directly against OCI and through Vizarr.

Current state:
- valid Zarr v3
- inline consolidated metadata
- sharded layout present
- outer shard shape: `[1, 1, 4096, 4096]`
- inner chunk shape: `[1, 1, 256, 256]`
- no `multiscales` pyramid metadata detected in the source store
- this section is the pre-multiscale-store baseline; live browse coverage was later extended to zoom levels `0..8`

Current readiness verdict:
- `browser_multiscale_ready = false`
- `seamless_rendering_ready = true`

Current gaps:
- `missing_multiscale_pyramid`

Browse generation status:
- browse overview generation now uses a sparse fast-lat/lon path for affine `EPSG:4326` datasets before falling back to the older projected render paths
- lower browse levels are derived from the highest built overview instead of re-rendering the full dataset at each zoom
- live maize now has durable browse overviews for zoom levels `0..8`
- the live serving profile on `2026-04-24` reports:
  - `browse_overview_zoom_levels = [0,1,2,3,4,5,6,7,8]`
  - `browse_overview_max_zoom = 8`
  - `seamless_rendering_ready = true`

Operational note:
- when using local security-token auth, refresh the OCI session before long-running browse generation jobs
- long multiscale builds now preflight the remaining token TTL, but if the OCI CLI session expires after the build starts you still need to re-authenticate and rerun

## Separate multiscale store

Vizarr now treats the browser-facing multiscale pyramid as a separate OCI store instead of mutating the native source store.

Current design:
- source store stays under the original dataset path, for example `cubes/maize_2025_live4.zarr`
- browser store is written under `OCI_MULTISCALE_PREFIX_ROOT`, for example `multiscale/cubes/maize_2025_live4.zarr`
- source proxy stays at `/api/zarr/{dataset_id}`
- browser multiscale proxy is exposed at `/api/zarr/multiscale/{dataset_id}`

Generator:
- `PYTHONPATH=backend backend/.venv/bin/python -m app.tools.generate_multiscale ...`
- useful knobs:
  - `--prepopulate-through-zoom N` to eagerly fill levels `0..N`
  - `--prepopulate-tile-budget N` to let the builder choose the highest eagerly filled zoom that stays within a cumulative tile budget
  - `--max-zoom N` to intentionally extend the live pyramid past the automatic tile-count ceiling when seamless high zooms matter more than build cost
  - `--min-token-ttl-seconds N` to fail fast before a long build if the remaining local OCI token lifetime is too short

Important correction from live maize:
- the source maize Zarr is already chunked, but a fully lazy browser pyramid still does not produce Earth Engine-like first-view performance because mid-zoom cold misses must synchronously reproject many source chunks
- the active browser-store design is now a tile-aligned Web Mercator pyramid where each map tile is one `256x256` Zarr chunk
- the builder writes all pyramid levels up front as metadata and can now eagerly populate low/mid zoom levels during the build
- coarse levels reuse browse overviews when possible; higher eagerly populated levels still render from source tiles
- the intended serving profile is hybrid:
  - prepopulated coarse and mid zooms for fast initial navigation
  - lazy population only for the finest zooms where user-driven exploration is narrower
  - after correcting the live maize native resolution to about `1.109 m`, the practical target is pyramid coverage through `z17`, with direct serving only above that

Live maize target shape:
- generated store: `multiscale/cubes/maize_2025_live4.zarr`
- generated multiscale paths: `0..14`
- generated store format: `Zarr v2`, consolidated
- discovered proxy root: `/api/zarr/multiscale/Y3ViZXMvbWFpemVfMjAyNV9saXZlNC56YXJy`
- root attrs now distinguish:
  - `population_strategy = prepopulated_then_lazy` when coarse levels were eagerly filled
  - `prepopulated_zoom_max = N` for the highest eagerly filled zoom
  - `max_zoom = N` for the highest built pyramid level
  - `population_strategy = lazy_on_demand` when no eager fill was requested
- planner routing now consumes that metadata so the app can distinguish prebuilt/hybrid pyramid zooms from the first direct full-resolution zoom

## Current implementation limitations

- RGB composites are not implemented yet
- the current catalog path is tuned toward Landsat-style Zarr v3 stores with a `bands` array
- generic support for arbitrary projected Zarr layouts still needs more work

## Next recommended steps

1. Verify that discovered OCI datasets appear in the frontend dataset picker.
2. Verify that selecting a band returns visible projected tiles on the map.
3. Add RGB composite support for common Landsat band combinations.
4. Expose dataset bounds and CRS metadata in the API.
5. Generalize the adapter layer for more Zarr layouts under `cubes`.
