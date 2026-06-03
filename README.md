# Satellite Zarr Viewer

Vizarr is a web viewer for satellite data stored as Zarr on object storage. The
current implementation is OCI-first, with a synthetic demo mode for local
regression checks.

The app has two serving paths:

- server-rendered WebP map tiles from FastAPI;
- read-only, dataset-scoped Zarr proxy endpoints for browser/native multiscale
  experiments.

---

## Repository layout

```
/
├── backend/              # Python / FastAPI tile server and OCI adapters
├── frontend/             # React / TypeScript MapLibre viewer
├── nginx/
│   └── nginx.conf        # Reverse proxy for production-style compose
├── docker-compose.yml    # Production-style stack behind Nginx
├── docker-compose.dev.yml
└── README.md
```

## Documentation

| Document | Description |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Current system architecture and implemented/planned boundaries |
| [docs/backend.md](docs/backend.md) | Backend modules, route surface, and runtime modes |
| [docs/frontend.md](docs/frontend.md) | Frontend component guide and state model |
| [docs/performance.md](docs/performance.md) | Implemented caching paths and planned performance work |
| [docs/performance-baselines.md](docs/performance-baselines.md) | CI quality gates and benchmark baseline expectations |
| [docs/build.md](docs/build.md) | Local dev, compose, OCI, and VM setup |
| [docs/oci-integration.md](docs/oci-integration.md) | OCI-specific discovery and live-store notes |
| [docs/compatibility.md](docs/compatibility.md) | GeoZarr, CF, STAC, and serving-profile compatibility contract |

Operational files:

- [AGENTS.md](AGENTS.md): repository instructions for coding agents.
- [WORKFLOW.md](WORKFLOW.md): Symphony/Linear orchestration prompt template.
- [VM_HANDOFF.md](VM_HANDOFF.md): local-only source bundle and VM runbook.

---

## Quick start: development compose

Use this while changing code. It gives backend hot reload and Vite HMR.

```bash
cp backend/.env.oci.example backend/.env
# Fill OCI_NAMESPACE, OCI_BUCKET, OCI_PREFIX, and refresh your OCI session.

docker compose -f docker-compose.dev.yml up --build
```

Default URLs:

- frontend: `http://localhost:5173`
- backend API: `http://localhost:8001/api`
- health: `http://localhost:8001/api/healthz`

The Vite dev server proxies `/api` to the backend.

For OCI browser smoke verification after the stack is up:

```bash
python3 scripts/oci_browser_smoke.py
```

The smoke command skips cleanly when OCI auth or OCI-backed datasets are not
available. See [docs/build.md](docs/build.md) for the full checklist.

For live OCI cold/warm tile timing and cache/representation headers:

```bash
python3 scripts/oci_performance_benchmark.py --output .cache/benchmarks/oci-benchmark.json
```

Set `TILE_DEBUG_HEADERS_ENABLED=true` on the backend to include tile timing and
object I/O counters in the report. The benchmark also skips cleanly when the
current stack is synthetic-only or OCI auth is unavailable.

## Quick start: production-style compose

```bash
docker compose up --build
```

Default URLs:

- viewer through Nginx: `http://localhost:8000`
- API through Nginx: `http://localhost:8000/api`
- direct backend API: `http://localhost:8001/api`
- health: `http://localhost:8000/api/healthz`

The default tracked `backend/.env.example` is safe for synthetic mode. For OCI,
copy `backend/.env.oci.example` to `backend/.env` and update compose to load it,
or run the dev stack which already treats `backend/.env` as optional.

## Local backend and frontend without compose

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Default URLs:

- frontend: `http://localhost:5173`
- backend API: `http://localhost:8000/api`
- health: `http://localhost:8000/api/healthz`

---

## Current API highlights

- `GET /api/healthz`
- `GET /api/datasets`
- `GET /api/datasets/{dataset_id}`
- `GET /api/datasets/{dataset_id}/variables`
- `GET /api/datasets/{dataset_id}/serving-profile`
- `GET /api/tilejson/{dataset_id}/{variable}`
- `GET /api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`
- `GET /api/colormaps`
- `GET /api/colormaps/{name}/palette`
- `POST /api/query/preview`, `/api/query/stats`, `/api/query/clip`
- `POST /api/exports`, `GET /api/exports/{job_id}`
- `GET /api/storage/*` for OCI-only discovery/debug workflows
- `GET` and `HEAD /api/zarr/*` for OCI-only dataset-scoped Zarr proxying

See [docs/backend.md](docs/backend.md) for route classification and response
roles.

## Tech stack

| Layer | Technology | Current role |
|---|---|---|
| Object store | OCI Object Storage | Primary remote Zarr backend |
| Data format | Zarr v3 source stores, generated browse/multiscale artifacts | Random access and map-tile serving |
| Backend | FastAPI, Xarray, NumPy, Pillow | Catalog, planning, tile rendering, proxy routes |
| Cache | Redis plus browser HTTP cache | Tile byte cache and repeat navigation speed |
| Frontend | React, TypeScript, Vite, MapLibre | Dataset picker and raster tile viewer |
| Query/state | TanStack Query, Zustand | Server metadata cache and map/UI state |
| Reverse proxy | Nginx | Production-style app/API routing |

## Auth model

Local development keeps `AUTH_ENABLED=false` by default. Production profiles
must set `APP_ENVIRONMENT=production` or `AUTH_ENABLED=true` and provide
`AUTH_API_KEYS`.

`AUTH_API_KEYS` accepts comma-separated keys. A bare key has access to all
datasets and debug/global routes. A scoped key uses
`key=dataset_id_a|dataset_id_b` and can only access those datasets.

The frontend can pass a key with `VITE_API_KEY` for browser smoke tests and
private deployments. Tile and WebSocket URLs include the key as `api_key`
because MapLibre raster requests and browser WebSockets cannot set custom
headers.

## Known planned work

- browser-native multiscale rendering is attempted for explicitly compatible
  serving profiles and falls back to server tiles otherwise;
- debounced adjacent-tile prefetch is implemented without a worker;
- RGB true-color and false-color composites are available when dataset metadata
  advertises the required bands;
- WebSocket dataset invalidation is implemented at `/ws/datasets`;
- direct projected `y/x`, `time/y/x`, and compatible banded `time/*/y/x`
  stores are supported; arbitrary projected Zarr layouts beyond those shapes
  remain backlog work;
- Nginx proxies `/api`, `/api/tiles/`, `/ws`, and frontend traffic; the tile
  route has a named disk cache in production-style compose;
- production-style compose keeps the backend internal-only by default, runs
  multiple Uvicorn workers, and persists Redis data in a named volume.
