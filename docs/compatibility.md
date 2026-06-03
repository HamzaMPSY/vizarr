# Compatibility Contract

This document defines whether a candidate Zarr cube is renderable by Vizarr and
what metadata is required for fast map interaction.

## Compatibility states

| State | Meaning |
|---|---|
| Supported | Implemented and covered by tests for the current backend/frontend path. |
| Partially supported | Implemented for the layouts listed here, but not a complete standard implementation. |
| Planned | A known interoperability target that is not implemented yet. |
| Out of scope | Not a current product goal. |

## Source Zarr stores

| Contract item | State | Requirement |
|---|---|---|
| Zarr v3 source store with inline consolidated metadata | Supported | Root `zarr.json` includes `consolidated_metadata.metadata`; arrays are discoverable without listing every chunk. |
| Zarr v3 source store without consolidated metadata | Partially supported | Child arrays must expose `zarr.json`; catalog hydration may require more object reads. |
| Zarr v2 source store | Planned | Current source catalog selection targets v3 stores. Generated multiscale stores may be v2. |
| Data array layouts | Supported | Static `y/x`, `time/y/x`, and banded `time/*/y/x` arrays. The band dimension name may vary. |
| Separate variable arrays | Supported | Multiple compatible 2D or 3D arrays can be exposed as variables. |
| Dimension metadata | Supported | Zarr v3 `dimension_names` for source arrays. |
| Xarray/Zarr v2 `_ARRAY_DIMENSIONS` | Planned | Not used by the current source catalog path. |
| Chunking | Supported | Unsharded chunks and Zarr v3 `sharding_indexed` chunks. Inner chunks near `256x256` are preferred. |
| Unsupported dimension order | Out of scope | Arrays whose spatial dimensions are not trailing `y/x`, or whose time dimension is not leading when present. |

## CRS And Spatial Transform

| Contract item | State | Requirement |
|---|---|---|
| CF-style `spatial_ref` array | Supported | Store a `spatial_ref` node with `attributes.crs_wkt`. |
| Normalized CRS authority | Supported | Backend exposes `crs_authority` when `crs_wkt` can be normalized by PyProj. |
| Affine transform | Supported | `spatial_ref.attributes.GeoTransform` may define pixel-to-world mapping. |
| Coordinate arrays | Supported | `x` and `y` arrays are required by the catalog and can provide spatial axes when no affine transform is available. |
| EPSG:4326 and projected CRS | Supported | Direct serving transforms Web Mercator tile requests into the source CRS. |
| Missing CRS | Partially supported | Some paths assume geographic coordinates, but serving-profile reports `missing_crs_metadata`; add `crs_wkt` for reliable rendering. |
| Rotated/skewed affine transforms | Out of scope | Current tile sampling assumes north-up grids. |

## Multiscale And Fast Interaction

| Contract item | State | Requirement |
|---|---|---|
| Browse overviews | Supported | Durable browse artifacts provide low/mid zoom speed. Missing or partial coverage appears as serving-profile gaps. |
| Generated multiscale Zarr v2 stores | Supported | Browser-readable levels use 4D `time/band/y/x`, `<f4`, `C` order, no compressor/filters, and `1/1/256/256` chunks. |
| Browser-GPU rendering from generated multiscale stores | Partially supported | The current deck.gl path uses the same generated Zarr v2 sidecar profile. Single-band rendering uploads one raw `r32float` value texture plus a palette texture; composite rendering uploads three raw `r32float` band textures. |
| Zarr v3 multiscale source read in browser | Planned | Server-rendered TileJSON remains the fallback. |
| Lazy multiscale population | Partially supported | Backend can lazily populate some missing pyramid tiles. Prepopulation is preferred for predictable latency. |

## Browser-GPU Rendering Contract

The browser-GPU path is an optimization over existing server-rendered tiles and
browser-native canvas rendering. It is compatible only when the dataset has a
generated browser-facing sidecar with:

- Zarr v2 consolidated metadata;
- dimensions `time`, `band`, `y`, and `x`;
- `<f4` dtype, C order, no compressor, and no filters;
- chunks `[1, 1, 256, 256]`;
- stable multiscale level paths and level bounds;
- browse zoom mapping;
- data array name;
- CRS and transform metadata where the source provides it.

The browser must read through the dataset-scoped multiscale proxy, never through
raw OCI object URLs. The current server-rendered TileJSON path remains the
fallback for missing sidecars, synthetic-only datasets, unsupported codecs,
unsupported layouts, excessive browser budgets, failed chunk requests, or
missing metadata.

The frontend rendering state should distinguish:

| Render mode | Meaning |
|---|---|
| `server-tiles` | MapLibre is rendering backend WebP tiles from TileJSON. |
| `browser-native` | MapLibre is rendering a browser-composed image source from compatible multiscale chunks. |
| `browser-gpu` | Deck.gl is rendering the compatible multiscale raster overlay through `ZarrColormapBitmapLayer` or `ZarrCompositeBitmapLayer` with raw float texture normalization. |

## CF And STAC

| Contract item | State | Requirement |
|---|---|---|
| CF `grid_mapping` via `spatial_ref` | Partially supported | The converter writes it and the catalog consumes `spatial_ref` attributes, but Vizarr does not validate full CF compliance. |
| CF units, calendar, and axis metadata | Planned | Time labels and variable display defaults are inferred for current fixtures, not fully CF-validated. |
| STAC item/collection discovery | Planned | The app discovers OCI object prefixes and Zarr roots directly. STAC links can describe the data externally, but they are not parsed by the backend yet. |
| STAC datacube/raster metadata | Planned | Useful future source for variable metadata, roles, scale, and display ranges. |

## Serving-profile gap vocabulary

`GET /api/datasets/{id}/serving-profile` reports gaps with these values:

| Gap | Meaning |
|---|---|
| `missing_data_array_metadata` | The catalog has not resolved a renderable source array. |
| `missing_dimension_metadata` | The source array does not expose usable dimension names. |
| `unsupported_dimension_order` | Dimensions do not match supported `y/x`, `time/y/x`, or `time/*/y/x` layouts. |
| `missing_crs_metadata` | No `spatial_ref.attributes.crs_wkt` was found. |
| `missing_spatial_transform` | Neither `GeoTransform` nor usable `x`/`y` coordinate metadata is available. |
| `missing_browser_proxy` | The dataset cannot be read through the backend Zarr proxy. |
| `missing_multiscale_pyramid` | No multiscale store is attached to the dataset. |
| `multiscale_store_not_browser_readable` | A multiscale store exists but does not match the browser-readable contract. |
| `missing_browse_overviews` | No browse overview artifacts are available. |
| `incomplete_browse_overview_coverage` | Browse overview zoom coverage is below the configured target. |

`browser_gpu_gaps` uses the same idea for the deck.gl path and may also report
GPU-specific values such as `missing_multiscale_proxy`,
`unsupported_multiscale_zarr_format`, `missing_consolidated_metadata`,
`missing_multiscale_levels`, or `level:<path>:<gap>` for per-level metadata,
chunk-layout, bounds, or browse-zoom problems.

## Quick candidate check

A cube is a good first target when:

- it is a Zarr v3 store with discoverable metadata;
- variables are `y/x`, `time/y/x`, or `time/*/y/x`;
- source arrays include `dimension_names`;
- `x`, `y`, and `spatial_ref.attributes.crs_wkt` are present;
- `GeoTransform` is present for projected grids;
- source chunks or inner shard chunks are near `256x256`;
- browse overviews or a generated multiscale pyramid exist for first-view speed.
