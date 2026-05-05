# Satellite Zarr Viewer

A high-performance web application for visualizing satellite data stored in [Zarr](https://zarr.dev/) format on object storage. The current implementation is OCI-first and combines server-side tile rendering with a browser-native multiscale path backed by dataset-scoped Zarr proxy endpoints.

---

## Repository layout

```
/
├── backend/              # Python / FastAPI tile server
├── frontend/             # React / TypeScript viewer
├── nginx/
│   └── nginx.conf        # Reverse proxy + tile cache
├── docker-compose.yml
└── README.md             ← you are here
```

## Documentation

| Document | Description |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design, component roles, data flow |
| [docs/backend.md](docs/backend.md) | Backend folder structure, API reference, key modules |
| [docs/frontend.md](docs/frontend.md) | Frontend folder structure, component guide, state management |
| [docs/performance.md](docs/performance.md) | Caching layers, prefetch strategy, tile encoding |
| [docs/build.md](docs/build.md) | Step-by-step setup for local dev and production |

---

## Quick start (Docker)

```bash
# 1. Clone and configure
cp backend/.env.example backend/.env
# edit backend/.env with your object store credentials

# 2. Start everything
docker compose up --build

# 3. Open
open http://localhost
```

The viewer will be at `http://localhost`, the API at `http://localhost/api`, and the WebSocket at `ws://localhost/ws`.

## Quick start (local dev)

```bash
# Terminal 1 — backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:5173`, proxying `/api` and `/ws` to the backend on port 8000.

---

## Browser-servable Zarr proxy

When `STORAGE_BACKEND=oci_zarr`, Vizarr now exposes dataset-scoped proxy endpoints for remote Zarr stores:

- `GET /api/zarr/{dataset_id}`
- `GET /api/zarr/{dataset_id}/zarr.json`
- `GET /api/zarr/{dataset_id}/{object_path}`

These endpoints are read-only, return dataset metadata and raw Zarr objects, and support byte-range requests for chunk and shard access.

Dataset responses also include:

- `zarr_format`
- `zarr_consolidated`
- `zarr_proxy_root`
- `multiscale_store_path`
- `multiscale_zarr_format`
- `multiscale_zarr_consolidated`
- `multiscale_proxy_root`

For datasets that satisfy the current browser contract, the frontend reads consolidated metadata from the multiscale proxy, fetches the selected chunk directly in the browser, colorizes it client-side, and falls back to server-rendered tiles when needed.

---

## Tech stack at a glance

| Layer | Technology | Why |
|---|---|---|
| Object store | OCI Object Storage via fsspec / ocifs | Cloud-native remote Zarr access without copying data into the app |
| Data format | Zarr v3 + browse overviews | Chunked, random-access storage plus lower-zoom overview serving |
| Backend framework | FastAPI + Uvicorn | Async, fast, WebSocket native |
| Array engine | Xarray + NumPy | Lazy metadata access plus direct chunk reads for projected imagery |
| Tile cache | Redis | Sub-millisecond tile cache with TTL |
| Tile encoding | Pillow → WebP | ~40% smaller than PNG |
| Map rendering | Deck.gl + MapLibre GL | WebGL2, handles thousands of tiles |
| Data fetching | TanStack Query | stale-while-revalidate, background sync |
| State | Zustand | Minimal, no unnecessary re-renders |
| Build tool | Vite | Fast HMR, native Web Worker support |
