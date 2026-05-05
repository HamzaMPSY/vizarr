# Backend

The backend is a FastAPI service that catalogs datasets, plans requests, renders
WebP map tiles, and exposes OCI-backed Zarr stores through safe read-only proxy
routes.

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
│   │   ├── query.py             # Planner preview/stats/clip endpoints
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
| `STORAGE_BACKEND` | `synthetic` or `oci_zarr` |
| `REDIS_URL` | Redis connection string |
| `TILE_CACHE_TTL` | Redis tile byte TTL in seconds |
| `OCI_CONFIG_PROFILE` | OCI CLI profile for local session auth |
| `OCI_CONFIG_FILE` | Mounted OCI config path |
| `OCI_NAMESPACE` | Object Storage namespace |
| `OCI_BUCKET` | Object Storage bucket |
| `OCI_PREFIX` | Prefix to scan or direct store prefix |
| `OCI_ZARR_PATH` | Optional direct Zarr store path |
| `OCI_BROWSE_PREFIX_ROOT` | Browse artifact root |
| `OCI_MULTISCALE_PREFIX_ROOT` | Multiscale artifact root |
| `BROWSE_PREWARM_ENABLED` | Startup browse/catalog warming toggle |

The canonical local OCI flow uses OCI session/profile auth from `~/.oci`. The
tracked examples do not use AWS access keys or generic object-store credentials.

## API route surface

All routes below are mounted under `/api`.

| Method | Path | Compatibility | Surface | Role |
|---|---|---|---|---|
| GET | `/healthz` | synthetic + OCI | public app API | Liveness/readiness probe returning `{ "status": "ok" }` |
| GET | `/datasets` | synthetic + OCI | public app API | List dataset metadata records |
| GET | `/datasets/{dataset_id}` | synthetic + OCI | public app API | Return one dataset and hydrate OCI metadata as needed |
| GET | `/datasets/{dataset_id}/variables` | synthetic + OCI | public app API | Return variables/bands for a dataset |
| GET | `/datasets/{dataset_id}/serving-profile` | synthetic + OCI | public app API | Report browser/proxy/multiscale readiness and gaps |
| GET | `/colormaps` | synthetic + OCI | public app API | List supported colormap names |
| GET | `/colormaps/{name}/palette` | synthetic + OCI | public app API | Return sampled RGBA palette values |
| GET | `/tilejson/{dataset_id}/{variable}` | synthetic + OCI | public app API | Return TileJSON with backend tile URL template |
| GET | `/tiles/{dataset_id}/{variable}/{z}/{x}/{y}` | synthetic + OCI | public app API | Return a rendered WebP map tile |
| POST | `/query/preview` | synthetic + OCI | internal/experimental | Return a planner preview artifact descriptor |
| POST | `/query/stats` | synthetic + OCI | internal/experimental | Return a planner stats artifact descriptor |
| POST | `/query/clip` | synthetic + OCI | internal/experimental | Return small clip artifact descriptor or accepted batch export |
| POST | `/exports` | synthetic + OCI | internal/experimental | Create an export job from a planned request |
| GET | `/exports/{job_id}` | synthetic + OCI | internal/experimental | Return in-memory export job status |
| GET | `/storage/objects` | OCI-only | development/debug | List raw OCI objects under a prefix |
| GET | `/storage/prefixes` | OCI-only | development/debug | List folder-like OCI prefixes |
| GET | `/storage/zarr-stores` | OCI-only | development/debug | Detect candidate Zarr store roots |
| GET | `/storage/inspect-zarr` | OCI-only | development/debug | Open a store and summarize variables, coords, and attrs |
| GET | `/storage/zarr-json` | OCI-only | development/debug | Return a store root `zarr.json` document from a relative object path or full `oci://...` URI |
| GET | `/zarr/{dataset_id}` | OCI-only | public app API | Return source Zarr proxy metadata |
| GET/HEAD | `/zarr/{dataset_id}/{object_path}` | OCI-only | public app API | Proxy a source Zarr object with byte-range support |
| GET | `/zarr/multiscale/{dataset_id}` | OCI-only | public app API | Return multiscale Zarr proxy metadata |
| GET/HEAD | `/zarr/multiscale/{dataset_id}/{object_path}` | OCI-only | public app API | Proxy a multiscale Zarr object with byte-range support |

The backend also registers top-level `WS /ws/datasets` outside the `/api`
prefix. It sends JSON dataset invalidation events and supports `{"type":"ping"}`
messages with `{"type":"pong"}` responses.

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

Response:

- body: `image/webp`;
- `Cache-Control: public, max-age=3600`;
- `X-Cache-Status: HIT` or `MISS`;
- `X-Data-Vmin` and `X-Data-Vmax`;
- `X-Request-Class`, `X-Execution-Path`, and `X-Representation`;
- optional `X-Browse-Source` when served from browse artifacts.

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

## Dependencies

The backend uses FastAPI, Pydantic settings, Xarray, NumPy, Pillow, Redis,
OCI/ocifs/fsspec libraries, and test utilities from `requirements.txt`.

Dask is present as a dependency and remains part of the broader data-processing
toolbox, but the checked-in request path does not start a Dask scheduler or
worker cluster.
