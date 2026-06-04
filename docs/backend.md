# Backend

The backend is a FastAPI service that catalogs datasets, plans requests, renders
WebP map tiles, exposes lossless source readback endpoints, and exposes
OCI-backed Zarr stores through safe read-only proxy routes.

## Runtime modes

| Mode | Setting | Purpose |
|---|---|---|
| Synthetic | `STORAGE_BACKEND=synthetic` | In-memory demo dataset for frontend and backend regression checks |
| OCI Zarr | `STORAGE_BACKEND=oci_zarr` | OCI Object Storage discovery, metadata hydration, tile rendering, and Zarr proxying |

`USE_SYNTHETIC_DATA` still exists in env examples for compatibility, but route
behavior is selected by `STORAGE_BACKEND`.

## Current folder structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, runtime state
│   ├── config.py                # Pydantic settings from environment/.env
│   ├── api/
│   │   ├── router.py            # Aggregates all /api routers
│   │   ├── health.py            # /healthz
│   │   ├── datasets.py          # Dataset, variables, serving profiles
│   │   ├── colormaps.py         # Colormap names and palettes
│   │   ├── tilejson.py          # TileJSON documents for map clients
│   │   ├── tiles.py             # WebP tile endpoint
│   │   ├── storage.py           # OCI discovery/debug endpoints
│   │   ├── zarr.py              # Dataset-scoped Zarr proxy
│   │   ├── websockets.py        # Dataset invalidation WebSocket
│   │   ├── query.py             # Source readback and planner preview/stats/clip endpoints
│   │   └── exports.py           # Export job creation/status
│   ├── core/
│   │   ├── cache.py
│   │   ├── colormap.py
│   │   ├── datasets.py
│   │   ├── dataset_catalog.py
│   │   ├── oci_auth.py
│   │   ├── oci_object_storage.py
│   │   ├── zarr_reader.py
│   │   ├── zarr_v3.py
│   │   ├── readback.py
│   │   ├── tile_generator.py
│   │   ├── projected_tile_generator.py
│   │   ├── browse_tiles.py
│   │   ├── browse_artifacts.py
│   │   ├── multiscale_builder.py
│   │   ├── multiscale_store.py
│   │   ├── multiscale_tiles.py
│   │   ├── serving_profile.py
│   │   └── tilejson.py
│   ├── index/
│   │   ├── catalog_store.py
│   │   └── planner_index.py
│   ├── models/
│   │   ├── artifacts.py
│   │   ├── dataset.py
│   │   ├── jobs.py
│   │   ├── plans.py
│   │   ├── requests.py
│   │   └── tile.py
│   ├── services/
│   │   ├── export_jobs.py
│   │   └── planner.py
│   └── tools/
│       ├── generate_browse.py
│       ├── generate_multiscale.py
│       └── parquet_to_zarr.py
├── tests/
├── Dockerfile
├── requirements.txt
├── .env.example
└── .env.oci.example
```

There is no `api/variables.py`, `core/prefetch.py`, or
`workers/dask_client.py` in the current implementation.

## Startup behavior

`app/main.py` registers `app.api.router.router` under `/api` and initializes
runtime state.

Synthetic mode:

- builds an in-memory demo dataset registry;
- uses the same tile and metadata routes as the OCI path where possible;
- does not require OCI credentials.

OCI mode:

- creates an `OCIObjectStorageConnector`;
- builds a dataset registry from `OCI_ZARR_PATH` or catalogs stores under
  `OCI_PREFIX`;
- recognizes projected variables shaped as static `y/x`, direct `time/y/x`, or
  banded `time/*/y/x` arrays;
- optionally warms catalog metadata and browse overviews;
- connects Redis for tile caching;
- catches expired OCI session auth and returns `503` with a readable detail.

## Settings

Current settings live in `app/config.py`.

Important settings:

| Setting | Role |
|---|---|
| `APP_ENVIRONMENT` | `development` by default; `production` enables API-key auth even when `AUTH_ENABLED=false` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated browser origins allowed by CORS. Empty means wildcard only outside production; production with an empty value sends no wildcard CORS headers |
| `CORS_ALLOWED_METHODS` | Comma-separated CORS methods, defaulting to `GET,POST,HEAD,OPTIONS` |
| `CORS_ALLOWED_HEADERS` | Comma-separated request headers allowed by CORS, including `Authorization`, `X-API-Key`, `Content-Type`, and range/cache validators |
| `AUTH_ENABLED` | Explicitly require API-key auth for protected routes |
| `AUTH_API_KEYS` | Comma-separated API keys; use `key=dataset_id_a\|dataset_id_b` for dataset-scoped access |
| `API_KEY_RATE_LIMIT_PER_MINUTE` | Per-API-key request limit. `0` disables rate limiting |
| `API_KEY_RATE_LIMIT_WINDOW_SECONDS` | Rate-limit window size in seconds |
| `STORAGE_BACKEND` | `synthetic` or `oci_zarr` |
| `REDIS_URL` | Redis connection string |
| `TILE_CACHE_TTL` | Redis tile byte TTL in seconds |
| `JOB_STORE_TTL` | Redis job status TTL in seconds for export, clip handoff, and browse generation job records |
| `TILE_CACHE_DISPLAY_RANGE_DECIMALS` | Decimal places used when normalizing `vmin`/`vmax` in tile cache keys |
| `TILE_CACHE_CUSTOM_RANGE_ENABLED` | When `false`, skip backend cache writes for explicit `vmin`/`vmax` tile requests |
| `DIRECT_TILE_MAX_PARALLEL_CHUNK_READS` | Maximum parallel source Zarr chunk reads per direct tile render; `0` uses the lower-level default |
| `DIRECT_TILE_MAX_OBJECT_GETS` | Direct tile object-read budget; `0` disables this limit |
| `DIRECT_TILE_MAX_BYTE_RANGE_GETS` | Direct tile byte-range read budget; `0` disables this limit |
| `DIRECT_TILE_MAX_OBJECT_BYTES` | Direct tile byte budget; `0` disables this limit |
| `DIRECT_TILE_MAX_ZARR_CHUNKS` | Direct tile Zarr chunk-read budget; `0` disables this limit |
| `DIRECT_TILE_MAX_SHARD_INDEX_READS` | Direct tile shard-index read budget; `0` disables this limit |
| `OCI_TEXT_CACHE_MAX_ENTRIES` | In-process LRU entry limit for reusable small text/JSON object reads |
| `OCI_BYTES_CACHE_MAX_ENTRIES` | In-process LRU entry limit for reusable byte object, range, and tail reads |
| `OCI_BYTES_CACHE_MAX_BYTES` | In-process byte cache size cap across full-object and range/tail reads |
| `ZARR_SHARD_INDEX_CACHE_ENTRIES` | Decoded Zarr v3 shard-index LRU entry limit |
| `ZARR_SHARD_INDEX_CACHE_BYTES` | Decoded Zarr v3 shard-index memory cap |
| `OCI_AUTH_MODE` | `auto`, `security_token`, `api_key`, `resource_principal`, or `instance_principal` |
| `OCI_CONFIG_PROFILE` | OCI config profile for local security-token or API-key auth |
| `OCI_CONFIG_FILE` | Mounted OCI config path |
| `OCI_NAMESPACE` | Object Storage namespace |
| `OCI_BUCKET` | Object Storage bucket |
| `OCI_PREFIX` | Prefix to scan or direct store prefix |
| `OCI_ZARR_PATH` | Optional direct Zarr store path |
| `OCI_BROWSE_PREFIX_ROOT` | Browse artifact root |
| `OCI_MULTISCALE_PREFIX_ROOT` | Multiscale artifact root |
| `BROWSE_PREWARM_ENABLED` | Startup browse/catalog warming toggle |

The canonical local OCI flow can use OCI session/profile auth from `~/.oci`, but
that profile is intentionally temporary and requires browser authentication when
it cannot be refreshed. For unattended access, use one of the non-interactive
OCI modes:

- `OCI_AUTH_MODE=api_key` with a normal OCI API-key profile;
- `OCI_AUTH_MODE=instance_principal` on an OCI compute instance with dynamic
  group policy;
- `OCI_AUTH_MODE=resource_principal` in supported OCI runtimes.

`OCI_AUTH_MODE=auto` uses resource principals in Data Flow/resource-principal
environments, otherwise reads the configured local profile and chooses API-key
auth when the profile has no `security_token_file`, or security-token auth when
it does.

## End-user auth

Vizarr has a minimal API-key gate for deployable environments. Auth is disabled
for local development unless `AUTH_ENABLED=true`. It is automatically required
when `APP_ENVIRONMENT=production`.

Protected HTTP routes accept either:

- `Authorization: Bearer <key>`;
- `X-API-Key: <key>`;
- `api_key=<key>` query parameter for browser-owned tile requests.

The `/ws/datasets` WebSocket accepts the same header forms or `api_key=<key>`.
`/api/healthz` remains public for health checks. In OCI mode it includes a
safe `oci_auth` block with the configured auth mode, status, and remaining
security-token lifetime when available. It does not expose config paths,
profile contents, credentials, bucket names, or object paths.

`AUTH_API_KEYS` supports two key shapes:

- `global-key` grants access to all datasets plus global/debug routes such as
  `/api/storage/*`, `/api/query/*`, and `/api/exports/*`;
- `scoped-key=dataset_a|dataset_b` grants only those dataset routes and filters
  dataset-list and WebSocket invalidation payloads to the allowed dataset ids.

Dataset-scoped keys also gate read-only Zarr proxy routes. Both source
`/api/zarr/{dataset_id}/...` and browser multiscale
`/api/zarr/multiscale/{dataset_id}/...` requests are denied when the key does
not include the route dataset id.

When `API_KEY_RATE_LIMIT_PER_MINUTE` is greater than zero, authenticated HTTP
requests are rate-limited per accepted key. Redis is used when reachable, with
a bounded in-process fallback for local development. Exceeded requests return
HTTP `429` with `Retry-After`, `X-RateLimit-Limit`, and
`X-RateLimit-Remaining` headers. WebSocket messages are not rate-limited by
this first guard.

Safe manual key rotation uses an overlap window:

1. Add the new key to `AUTH_API_KEYS` beside the old key, preserving any
   dataset scope, then reload or redeploy the backend.
2. Update frontend/deployment secrets that hold `VITE_API_KEY` or external
   callers to use the new key.
3. Confirm `/api/datasets` and a representative tile request succeed with the
   new key.
4. Remove the old key from `AUTH_API_KEYS` and reload or redeploy again.

This is intentionally a first production gate, not a full identity system.
OIDC/JWT, per-user roles, and persistent tenant policy remain future work.

## CORS behavior

Local development keeps CORS permissive when `CORS_ALLOWED_ORIGINS` is empty so
Vite, direct backend, and compose workflows remain simple. Production does not
default to `*`: set `CORS_ALLOWED_ORIGINS` to the exact viewer origin or
origins, for example `https://viewer.example.com`. A literal `*` is ignored in
production settings.

## API route surface

All routes below are mounted under `/api`.

| Method | Path | Compatibility | Surface | Role |
|---|---|---|---|---|
| GET | `/healthz` | synthetic + OCI | public app API | Liveness/readiness probe returning `{ "status": "ok" }` |
| GET | `/datasets` | synthetic + OCI | public app API | List dataset metadata records |
| GET | `/datasets/{dataset_id}` | synthetic + OCI | public app API | Return one dataset and hydrate OCI metadata as needed |
| GET | `/datasets/{dataset_id}/variables` | synthetic + OCI | public app API | Return variables/bands for a dataset |
| GET | `/datasets/{dataset_id}/serving-profile` | synthetic + OCI | public app API | Report browser/proxy/multiscale readiness and gaps |
| POST | `/datasets/{dataset_id}/browse-generation` | OCI-only | internal/operator API | Queue browse overview generation for a dataset; duplicate active requests return the existing job |
| GET | `/datasets/{dataset_id}/browse-generation/{job_id}` | OCI-only | internal/operator API | Return durable browse generation job status |
| GET | `/colormaps` | synthetic + OCI | public app API | List supported colormap names |
| GET | `/colormaps/{name}/palette` | synthetic + OCI | public app API | Return sampled RGBA palette values |
| GET | `/tilejson/{dataset_id}/{variable}` | synthetic + OCI | public app API | Return TileJSON with backend tile URL template |
| GET | `/tiles/{dataset_id}/{variable}/{z}/{x}/{y}` | synthetic + OCI | public app API | Return a rendered WebP map tile |
| GET | `/query/point` | synthetic + OCI | internal/experimental | Return one numeric source value at a WGS84 point before colormap or tile encoding |
| GET | `/query/bbox` | synthetic + OCI | internal/experimental | Return a bounded source-value window for a small WGS84 bbox before colormap or tile encoding |
| GET | `/query/range` | synthetic + OCI | internal/experimental | Return metadata or bounded active-view range stats, percentiles, and histogram bins before colormap or tile encoding |
| POST | `/query/preview` | synthetic + OCI | internal/experimental | Return a planner preview artifact descriptor |
| POST | `/query/stats` | synthetic + OCI | internal/experimental | Return a planner stats artifact descriptor |
| POST | `/query/clip` | synthetic + OCI | internal/experimental | Return small clip artifact descriptor or accepted batch export |
| POST | `/exports` | synthetic + OCI | internal/experimental | Create an export job from a planned request |
| GET | `/exports/{job_id}` | synthetic + OCI | internal/experimental | Return durable export job status |
| GET | `/storage/objects` | OCI-only | development/debug | List raw OCI objects under a prefix |
| GET | `/storage/prefixes` | OCI-only | development/debug | List folder-like OCI prefixes |
| GET | `/storage/zarr-stores` | OCI-only | development/debug | Detect candidate Zarr store roots |
| GET | `/storage/inspect-zarr` | OCI-only | development/debug | Open a store and summarize variables, coords, and attrs |
| GET | `/storage/zarr-json` | OCI-only | development/debug | Return a store root `zarr.json` document from a relative object path or full `oci://...` URI |
| GET | `/zarr/{dataset_id}` | OCI-only | public app API | Return source Zarr proxy metadata |
| GET/HEAD | `/zarr/{dataset_id}/{object_path}` | OCI-only | public app API | Proxy a source Zarr object with byte-range support |
| GET | `/zarr/multiscale/{dataset_id}` | OCI-only | public app API | Return multiscale Zarr proxy metadata |
| GET/HEAD | `/zarr/multiscale/{dataset_id}/{object_path}` | OCI-only | public app API | Proxy a multiscale Zarr object with byte-range support |

`GET /api/datasets` accepts an optional `bbox=west,south,east,north` WGS84
query parameter. The backend filters the authenticated dataset list to records
whose `DatasetMeta.bounds` intersects the bbox. Antimeridian viewports are
represented with `west > east`; malformed, non-finite, or out-of-range bbox
values return HTTP `422`.

The backend also registers top-level `WS /ws/datasets` outside the `/api`
prefix. It sends JSON dataset invalidation events and supports `{"type":"ping"}`
messages with `{"type":"pong"}` responses.

## Durable job status

Export jobs, clip handoff jobs, and browse generation jobs use the shared
backend job record store. When Redis is reachable through `REDIS_URL`, job
records are stored as versioned JSON payloads with the configured
`JOB_STORE_TTL` and can be read by another backend worker. If Redis is not
reachable at startup, the service falls back to in-memory job status for local
development and logs that the store is not durable.

Accepted job writes must complete against the active store. If Redis was
selected but a create/read/update operation fails, the API returns HTTP `503`
instead of silently acknowledging a job whose durable status was not recorded.
Malformed stored job payloads are ignored and removed so callers receive the
normal not-found response rather than invalid data.

Durable status does not make the background computation itself durable. Browse
generation still runs in the accepting backend process, and export output
durability remains limited to the existing artifact/export output path.

## Tile endpoint

`GET /api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`

For single-band rendering, `{variable}` is a dataset variable or band id. For
advertised composites, `{variable}` is the composite style id, such as
`true-color` or `false-color`. Composite requests bypass browse overviews and
pyramid artifacts for now, render from the source bands, and return RGB WebP
bytes with `X-Representation: serving`.

Query parameters:

| Parameter | Type | Default | Role |
|---|---|---|---|
| `time_index` | int | `0` | Dataset time index |
| `colormap` | string | `viridis` | Style name and planner style input |
| `vmin` | float | dataset/display default | Optional display minimum |
| `vmax` | float | dataset/display default | Optional display maximum |
| `cache_version` | string | dataset cache version | Internal cache-busting token emitted in TileJSON templates |

Response:

- body: `image/webp`;
- `Cache-Control: public, max-age=3600`;
- `X-Cache-Status: HIT` or `MISS`;
- `X-Data-Vmin` and `X-Data-Vmax`;
- `X-Request-Class`, `X-Execution-Path`, and `X-Representation`;
- `X-Request-Coalescing` for uncached in-flight request sharing;
- optional `X-Browse-Source` when served from browse artifacts.

Tiles outside `DatasetMeta.bounds` short-circuit before source rendering and
return a transparent 256x256 WebP with `X-Representation: empty`,
`X-Tile-Empty: bounds`, `X-Planned-Representation`, and cache headers. This
keeps MapLibre on the normal image-tile path while avoiding OCI object reads for
tiles that cannot intersect the dataset.

When `TILE_DEBUG_HEADERS_ENABLED=true`, the response also includes sanitized
diagnostics such as `X-Tile-Time-Ms`, `X-Tile-Render-Ms`,
`X-Tile-Encode-Ms`, `X-Tile-Coalescing-Ms`, `X-Object-Get-Count`,
`X-Object-Bytes-Read`, and `X-Zarr-Chunk-Count`. Direct source renders also include
`X-Tile-Budget-Status` and, when a limit is exceeded, the budget metric, limit,
and actual value. These are disabled by default and mirror the structured
`tile_request_metrics` backend log payload.

When a direct source render exceeds a configured budget, the endpoint returns
HTTP `503` with a `direct_tile_compute_budget_exceeded` JSON detail instead of
caching or returning the tile. Browse and prebuilt pyramid hits do not consume
the direct tile budget.

## Scientific readback endpoints

WebP tile responses are visual products for map navigation. They may be
colormapped, resampled, composited, and lossy-encoded, so clients must not use
tile pixels as scientific source values.

Use the readback endpoints when the caller needs numeric source data before
colormap or WebP encoding:

- `GET /api/query/point?dataset_id=...&variable=...&lon=...&lat=...`
  returns one source value plus source pixel coordinates when the point
  intersects the dataset;
- `GET /api/query/bbox?dataset_id=...&variable=...&bbox=west,south,east,north`
  returns a small two-dimensional value grid clipped to the source pixels whose
  centers intersect the WGS84 bbox;
- `GET /api/query/range?dataset_id=...&variable=...&bbox=west,south,east,north`
  samples a bounded source window and returns min/max, p02/p98, histogram bin
  edges, histogram counts, valid-value count, and unit. Without `bbox`, it
  returns the variable metadata stats with `stats_source: "metadata"`.

The point and bbox endpoints accept `time_index` and optional `diagnostics=true`.
Bbox readback and range sampling are intentionally bounded with `max_width` and
`max_height` query parameters. Bbox defaults to `64`; range sampling defaults to
`128`; both are capped at `512`, and oversized requests return HTTP `413`.
Diagnostics may include the source store path, array name, dtype, source CRS,
source window, chunk shape, and object/chunk read counters.

These endpoints preserve the source array dtype until JSON serialization and
convert nodata, fill values, and non-finite numeric values to JSON `null`. They
are read-only and do not add a PNG or lossless image tile mode; exports and
future product workflows should build on explicit source-data APIs instead of
changing the default WebP tile route.

## Dataset metadata

`DatasetMeta` includes `variables` for scalar/band rendering and
`composite_styles` for RGB rendering. Static `y/x` projected arrays are exposed
as one-step variables. OCI catalog hydration advertises composite styles only
when all required roles are present:

- `true-color`: red, green, blue;
- `false-color`: near infrared, red, green.

Each composite style records the concrete band ids used by the tile route so the
frontend can present style names without hardcoding Landsat band mappings.

OCI dataset metadata also exposes CRS fields when the source store has
`spatial_ref` metadata:

- `crs_wkt`: raw CRS WKT;
- `crs_authority`: normalized authority string, such as `EPSG:32629` or
  `OGC:CRS84`, when resolvable.

The catalog supports the current banded 4D `time/*/y/x` layout, direct 3D
`time/y/x` projected variables, and static 2D `y/x` projected variables.
Layouts outside those shapes are skipped with a clear unsupported-layout
diagnostic during catalog build.

## Storage inspection

`GET /api/storage/zarr-json?zarr_path=...` accepts either a relative store path,
such as `cubes/example.zarr`, or a fully qualified `oci://bucket@namespace/...`
store URI. Empty paths return `400`; missing root `zarr.json` documents return
`404`.

## Frontend endpoint alignment

`frontend/src/api/endpoints.ts` currently calls:

- `/api/datasets`
- `/api/datasets/{dataset_id}`
- `/api/datasets/{dataset_id}/variables`
- `/api/datasets/{dataset_id}/serving-profile`
- `/api/tilejson/{dataset_id}/{variable}`
- `/api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`
- `/api/colormaps`
- `/api/colormaps/{name}/palette`
- `/ws/datasets`

Those names line up with the documented public app API above.

Serving-profile `seamless_rendering_gaps` use the compatibility vocabulary in
[compatibility.md](compatibility.md), so clients can explain whether a cube is
blocked by missing CRS metadata, unsupported dimension order, missing browse
overviews, or missing browser-readable multiscale data.

For generated multiscale stores, the serving profile also exposes normalized
level descriptors in `multiscale_levels`. Each descriptor includes the level
path, optional browse zoom, level bounds, shape, chunk shape, dtype,
compressor/filter/order fields, dimension separator, browser-readable status,
browser-GPU compatibility status, and per-level gaps. `browser_gpu_ready` is a
separate readiness flag for the planned deck.gl path; it is stricter than
`browser_multiscale_ready` because it requires consolidated Zarr v2 metadata,
per-level bounds, browse zoom mapping, and GPU-compatible chunk layout. When it
is false, `browser_gpu_reason` and `browser_gpu_gaps` provide the exact fallback
cause for UI debug state and browser probes.

## Dependencies

The backend uses FastAPI, Pydantic settings, Xarray, NumPy, Pillow, Redis,
OCI/ocifs/fsspec libraries, and test utilities from `requirements.txt`.

Dask is present as a dependency and remains part of the broader data-processing
toolbox, but the checked-in request path does not start a Dask scheduler or
worker cluster. Current tile work is in-process, with bounded per-request source
chunk concurrency and optional direct tile read budgets.
