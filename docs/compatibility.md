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

## Interoperability tracks

These tracks keep compatibility expansion scoped around the existing OCI-first,
read-only Zarr viewer. A track may have a metadata proof without being a
supported rendering path.

| Track | Product status | Current outcome |
|---|---|---|
| STAC item/collection discovery | Planned | STAC can describe candidate assets externally, but Vizarr still discovers OCI prefixes and Zarr roots directly. |
| Zarr v2 source stores | Planned | Metadata parsing proof exists for consolidated and unconsolidated v2 stores, including Xarray `_ARRAY_DIMENSIONS`; direct source rendering remains v3-only. |
| COG source imagery | Out of scope for Vizarr-owned serving until the TiTiler decision record | COG is a separate raster/tile stack candidate. Prefer a TiTiler or hybrid spike before adding native COG tile routes. |

### STAC discovery track

Target metadata inputs:

- STAC Item and Collection JSON with `id`, `bbox`, `geometry`, temporal
  properties, `links`, and `assets`;
- asset roles, media types, titles, and hrefs that identify Zarr stores or
  derived browse/multiscale artifacts;
- optional STAC datacube and raster extension fields for variable names, band
  roles, scale/offset, nodata, and display hints.

Reader and adapter boundary:

- A future STAC discovery adapter should map approved STAC assets into the same
  catalog candidate records that OCI prefix discovery uses.
- The catalog must still validate every candidate through the layout adapter
  boundary from ticket `026`; STAC metadata cannot bypass Zarr metadata
  validation.

Auth and storage assumptions:

- First implementation should use OCI-hosted STAC JSON or STAC assets that
  resolve to existing OCI object paths.
- Generic STAC API, S3, GCS, Azure, signed URL, and public HTTP claims stay
  planned until a ticket selects and tests them.

TileJSON, tile, and readback support:

- STAC should not introduce a new tile path. Renderable Zarr assets should use
  the existing TileJSON, tile, query, and read-only proxy paths after catalog
  validation succeeds.

Tests and fixtures:

- Add secret-free Item and Collection fixtures with one Zarr asset, one ignored
  non-renderable asset, temporal metadata, bbox/geometry, and datacube/raster
  hints.
- Validate that unsupported STAC assets produce catalog diagnostics instead of
  partially registered datasets.

Reasons to defer:

- STAC expands discovery breadth more than rendering capability; Zarr v2 source
  metadata unlocks more existing cubes with less architecture risk.

### Zarr v2 source-store track

Target metadata inputs:

- consolidated `.zmetadata` or root `.zgroup` plus child `.zarray` and
  `.zattrs` documents;
- Xarray `_ARRAY_DIMENSIONS` on array attributes;
- CF-style `spatial_ref` attributes, `x`/`y` coordinate arrays, optional `time`
  coordinate arrays, and existing band-label conventions.

Reader and adapter boundary:

- The implemented proof normalizes v2 array metadata into catalog-readable
  nodes with `shape`, `attributes`, and `dimension_names`.
- The future source adapter should consume the same projected layout contract as
  v3: `y/x`, `time/y/x`, or `time/*/y/x`, with trailing spatial dimensions.
- Direct tile reading, codec support, and chunk path generation must stay behind
  a named adapter from ticket `026`; metadata parsing alone must not mark a v2
  source store renderable.

Auth and storage assumptions:

- Initial support remains OCI-only and read-only through the existing connector
  and dataset-scoped proxy rules.
- Do not expose raw object URLs or add write paths for source stores.

TileJSON, tile, and readback support:

- Current TileJSON, tile, and readback support remain implemented only for
  cataloged renderable source layouts.
- Zarr v2 TileJSON/tile/readback support requires a follow-up adapter spike for
  v2 chunk keys, dtype/fill handling, compressor/filter policy, and budgeted
  object reads.

Tests and fixtures:

- Implemented: unit coverage for consolidated and unconsolidated v2 metadata
  normalization and `_ARRAY_DIMENSIONS` layout validation.
- Next fixtures should cover one uncompressed 2D or 3D v2 source cube, one
  compressed v2 cube rejected with a clear reason, and one missing
  `_ARRAY_DIMENSIONS` cube rejected with remediation guidance.

Reasons to defer:

- The current hot path uses custom Zarr v3 chunk/shard readers. Full v2 source
  support needs explicit codec and chunk-key policy before direct serving is
  safe to advertise.

### COG track

Target metadata inputs:

- COG href, bbox, CRS, band count/names, nodata, overviews, internal tiling, and
  optional STAC asset metadata.

Reader and adapter boundary:

- COG should not be folded into the Zarr layout adapter. Treat it as a separate
  raster source type behind a TiTiler/hybrid decision.
- If accepted later, Vizarr should own catalog, auth, dataset selection,
  readiness, and UI policy while TiTiler or a similar raster service owns COG
  tile, point, preview, and statistics semantics.

Auth and storage assumptions:

- OCI auth and read-only access remain mandatory for private assets.
- Public HTTP COGs or non-OCI buckets need a separate threat model and cache
  policy.

TileJSON, tile, and readback support:

- No native COG TileJSON or tile route is implemented in Vizarr.
- A hybrid spike should compare TiTiler `/cog` TileJSON, tile, point, preview,
  validation, and statistics routes with Vizarr's current TileJSON, tile cache,
  colormap/range, and query semantics.

Tests and fixtures:

- Add an ADR/spike fixture only after ticket `028` decides whether COG is
  delegated, hybrid, or out of scope.
- If delegated, tests should assert catalog handoff, auth propagation, URL
  signing/redaction, TileJSON compatibility, and fallback behavior.

Reasons to defer:

- Owning COG directly would add a second geospatial tile stack beside the Zarr
  renderer. That is valuable for broad satellite workflows, but it is outside
  the core satellite Zarr viewer until the TiTiler comparison is complete.

## Source Zarr stores

| Contract item | State | Requirement |
|---|---|---|
| Zarr v3 source store with inline consolidated metadata | Supported | Root `zarr.json` includes `consolidated_metadata.metadata`; arrays are discoverable without listing every chunk. |
| Zarr v3 source store without consolidated metadata | Partially supported | Child arrays must expose `zarr.json`; catalog hydration may require more object reads. |
| Zarr v2 source store | Planned | Backend can parse v2 metadata for adapter validation, but current source catalog rendering still accepts only v3 stores. Generated multiscale stores may be v2. |
| Data array layouts | Supported | Static `y/x`, `time/y/x`, and banded `time/*/y/x` arrays. The band dimension name may vary. |
| Separate variable arrays | Supported | Multiple compatible 2D or 3D arrays can be exposed as variables. |
| Dimension metadata | Supported | Zarr v3 `dimension_names` for source arrays. |
| Xarray/Zarr v2 `_ARRAY_DIMENSIONS` | Partially supported | Metadata normalization and layout validation are implemented for v2 source-store planning; direct source rendering remains planned. |
| Chunking | Supported | Unsharded chunks and Zarr v3 `sharding_indexed` chunks. Inner chunks near `256x256` are preferred. |
| Unsupported dimension order | Out of scope | Arrays whose spatial dimensions are not trailing `y/x`, or whose time dimension is not leading when present. |

### Layout adapter registry

Catalog discovery validates candidate stores through a named layout adapter
registry. A store is accepted only when one adapter returns an accepted schema;
otherwise the live catalog scan records a structured unsupported-layout
diagnostic with reason and remediation.

| Adapter | Priority | State | Accepted dimensions | Required metadata | Capabilities |
|---|---:|---|---|---|---|
| `projected-4d-banded` | 100 | Supported | `time/*/y/x` | data array shape and dimensions, band coordinate or `band_labels`, `x`, `y`, `spatial_ref.crs_wkt`, and `GeoTransform` or coordinate arrays | dynamic tiles, browse overviews, multiscale source, point/bbox/range/clip readback |
| `projected-3d-time-variable` | 90 | Supported | `time/y/x` per variable array | data array shape and dimensions, `x`, `y`, `spatial_ref.crs_wkt`, and `GeoTransform` or coordinate arrays | dynamic tiles, browse overviews, multiscale source, point/bbox/range/clip readback |
| `projected-2d-static-variable` | 80 | Supported | `y/x` per variable array | data array shape and dimensions, `x`, `y`, `spatial_ref.crs_wkt`, and `GeoTransform` or coordinate arrays | dynamic tiles, browse overviews, multiscale source, point/bbox/range/clip readback |
| `geographic-lat-lon` | 20 | Planned | `lat/lon`, `time/lat/lon` | latitude and longitude coordinate arrays plus adapter-specific reprojection policy | none yet; rejected with `unsupported_dimension_order` |
| `unsupported-ambiguous-dimensions` | 0 | Diagnostic fallback | none | usable dimension names | none; rejected with `missing_dimension_metadata` or `unsupported_dimension_order` |

Project-specific adapters can be registered without rewriting the core catalog.
They must declare a name, priority, accepted dimensions, required metadata,
CRS/transform conventions, tile capabilities, and readback capabilities.

`GET /api/datasets/{id}/serving-profile` exposes the accepted adapter contract
as `layout_validation`. Live catalog diagnostics also include accepted and
unsupported layout-validation records.

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
| Cloud Optimized GeoTIFF source imagery | Out of scope | Do not advertise native COG support until ticket `028` decides whether COG is delegated to TiTiler or handled through a hybrid service. |

## References

- [STAC Specification](https://stacspec.org/en/about/stac-spec/)
- [Zarr storage specification version 2](https://zarr.readthedocs.io/en/v2.13.6/spec/v2.html)
- [Xarray Zarr encoding specification](https://docs.xarray.dev/en/stable/internals/zarr-encoding-spec.html)
- [OGC Cloud Optimized GeoTIFF standard](https://www.ogc.org/standards/ogc-cloud-optimized-geotiff/)
- [TiTiler COG endpoints](https://developmentseed.org/titiler/endpoints/cog/)

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
