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
- exposes discovery and inspection endpoints

## OCI discovery endpoints

Available backend endpoints:
- `GET /api/storage/objects`
- `GET /api/storage/prefixes`
- `GET /api/storage/zarr-stores`
- `GET /api/storage/inspect-zarr?zarr_path=...`
- `GET /api/storage/zarr-json?zarr_path=...`

Purpose:
- list raw objects and folder-like prefixes
- detect Zarr v2 and v3 store roots
- inspect variable, coordinate, and metadata structure before attempting visualization

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
- skipping unreadable stores during catalog build so one bad store does not fail the whole API

## Current implementation limitations

- RGB composites are not implemented yet
- projected bounds are not yet surfaced back to the frontend for auto-fit
- the current catalog path is tuned toward Landsat-style Zarr v3 stores with a `bands` array
- generic support for arbitrary projected Zarr layouts still needs more work

## Next recommended steps

1. Verify that discovered OCI datasets appear in the frontend dataset picker.
2. Verify that selecting a band returns visible projected tiles on the map.
3. Add RGB composite support for common Landsat band combinations.
4. Expose dataset bounds and CRS metadata in the API.
5. Generalize the adapter layer for more Zarr layouts under `cubes`.
