# Satellite Zarr Viewer

A high-performance, tile-based web application for visualizing satellite data stored in [Zarr](https://zarr.dev/) format on object storage (S3, GCS, Azure Blob). Designed to feel instant — stacked caching, predictive prefetching, and WebGL rendering ensure the viewer never makes the user wait.

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

## Tech stack at a glance

| Layer | Technology | Why |
|---|---|---|
| Object store | S3 / GCS / Azure via fsspec | Cloud-native Zarr access, no data copy |
| Data format | Zarr v2 + Blosc-zstd | Chunked, compressed, random-access |
| Backend framework | FastAPI + Uvicorn | Async, fast, WebSocket native |
| Array engine | Xarray + Dask | Lazy parallel reads, dimension-aware |
| Tile cache | Redis | Sub-millisecond tile cache with TTL |
| Tile encoding | Pillow → WebP | ~40% smaller than PNG |
| Map rendering | Deck.gl + MapLibre GL | WebGL2, handles thousands of tiles |
| Data fetching | TanStack Query | stale-while-revalidate, background sync |
| State | Zustand | Minimal, no unnecessary re-renders |
| Build tool | Vite | Fast HMR, native Web Worker support |
