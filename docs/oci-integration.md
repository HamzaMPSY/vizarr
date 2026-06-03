# OCI Integration

This document captures the current OCI Object Storage integration status for Vizarr.

## Current auth model

Local development can use:
- mount the host `~/.oci` directory into the backend container
- use OCI profile `prof`
- read `OCI_CONFIG_FILE` from the mounted config path
- `OCI_AUTH_MODE=security_token` for browser-authenticated OCI CLI sessions
- `OCI_AUTH_MODE=api_key` for non-interactive local or VM automation
- `OCI_AUTH_MODE=auto` to choose API-key auth when the profile has no
  `security_token_file`, or security-token auth when it does

OCI Data Flow / in-cloud execution:
- use OCI resource principals

OCI Compute execution:
- use `OCI_AUTH_MODE=instance_principal` with dynamic group policy

Session-token auth is not a fully autonomous backend credential. It can be
refreshed before expiry, but once OCI requires browser authentication again a
user must authenticate. For services, benchmarks, and long-running browse or
multiscale jobs, prefer API keys, instance principals, or resource principals.

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
- read root `zarr.json` metadata from either relative object paths or full `oci://...` URIs
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

## Historical verification context

The following values document a previously verified private environment. They
are not public setup defaults and should not be copied into tracked env files:

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
- RGB composites for natural/false-color rendering when the required bands are present

The current implementation already supports:
- discovery of Zarr v3 stores under `cubes`
- exposing band names as dataset variables
- direct static projected `y/x` variables as single-step frontend variables
- direct projected 3D `time/y/x` variables without requiring a synthetic band dimension
- non-Landsat band dimension names for compatible 4D `time/*/y/x` arrays
- CRS metadata exposure through `crs_wkt` and normalized `crs_authority`
- advertising true-color and false-color composite styles for recognized Landsat-style band sets
- per-band projected tile generation
- RGB WebP tile generation for advertised composite styles
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

The full standards-facing compatibility contract and serving-profile gap
vocabulary live in [compatibility.md](compatibility.md).

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
- `scripts/oci_session_watchdog.py --profile-name prof --config-file ~/.oci/config --loop`
  can keep an already-authenticated local session fresh while OCI allows
  refresh; it cannot bypass browser authentication after the session expires
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

## Parquet to Zarr source-cube conversion

The source-cube converter can ingest partitioned Parquet from a different OCI
bucket than the configured Zarr destination bucket. Bucket-relative source
prefixes are resolved against `--source-bucket`; the destination `--output-store`
is still resolved against the configured `OCI_BUCKET` unless it is passed as a
full `oci://bucket@namespace/path.zarr` URI.

Example Sentinel-2 tile ingest:

```bash
podman exec vizarr_backend_1 python -m app.tools.parquet_to_zarr \
  --source-bucket bu-lhr-dp-dibe-si007-dev-detcd-AppintegDIdev \
  --parquet-prefix '20260401/48C676A9D6277B3A21F4EC87F8C70F56B7D057CE3AF4E2CDF79E11221840C639/F5347356860C76BC6E7A6B9505789C79798191668A626078CC704568E5294423/20260325_20260401/1d2053b2-34b7-49b1-8b8f-0935d4bf1b0b/35MQS_1_0_2026-03-25_2026-04-01.parquet' \
  --output-store cubes/35MQS_1_0_2026-03-25_2026-04-01.zarr \
  --layout bands \
  --crs EPSG:4326 \
  --source-crs EPSG:4326 \
  --overwrite
```

If `--value-columns` is omitted, all numeric non-coordinate, non-time columns
are written as bands. If `--x-column`, `--y-column`, and `--timestamp-regex` are
omitted, the converter tries common coordinate names such as `LONGITUDE` /
`LATITUDE` and extracts a `YYYY-MM-DD` or `YYYYMMDD` date from the source path.
For point/quadkey Parquet whose lon/lat centroids are all distinct, the
converter detects the axis explosion and infers a regular target grid from row
count and coordinate extent before snapping the points. For projected 10 m
Sentinel-2 output, pass the explicit projected `--crs` plus `--x-resolution 10
--y-resolution 10`.

## Current implementation limitations

- composite style detection is limited to common Landsat/Sentinel-like red, green, blue, and near-infrared aliases
- generic support for arbitrary projected Zarr layouts beyond direct `y/x`,
  `time/y/x`, and banded `time/*/y/x` arrays still needs more work

## Representative cube matrix

`docs/oci-cube-matrix.example.json` defines the secret-free compatibility matrix
used for live OCI benchmark selection. It tracks representative cube families by
shape class, Zarr format, consolidation state, CRS authority, chunk/shard layout
class, expected variables/composites, and expected planner representation by
zoom band.

The matrix intentionally does not contain OCI namespace, bucket, object path, or
token values. Each benchmarkable entry names environment variables for local
private dataset and variable selectors. This lets private deployments bind real
OCI datasets to the public compatibility matrix without committing sensitive
storage details.

## Next recommended steps

1. Run `python3 scripts/oci_browser_smoke.py` against a live OCI dev stack, then
   complete the printed browser checklist for the frontend picker, auto-fit, and
   visible tile preview.
2. Run `python3 scripts/oci_performance_benchmark.py --output
   .cache/benchmarks/oci-benchmark.json` against the same stack to capture
   metadata timing, cold/warm tile timing, cache headers, and planner
   representation headers.
3. Verify true-color and false-color composite tiles against a live Landsat store.
4. Verify CRS metadata and direct 3D variables against a live non-Landsat store.
5. Generalize the adapter layer for more Zarr layouts under `cubes`.
