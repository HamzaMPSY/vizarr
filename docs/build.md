# Build guide

Step-by-step instructions for local development, testing, and production deployment.

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Backend runtime |
| Node.js | 20+ | Frontend build + dev server |
| Docker | 24+ | Production containers |
| Docker Compose | 2.2+ | Multi-container orchestration |
| Redis | 7+ | Tile cache (or use Docker) |
| Object store access | — | S3, GCS, or Azure credentials |

---

## 1. Clone and configure

```bash
git clone https://github.com/your-org/satellite-zarr-viewer.git
cd satellite-zarr-viewer
cp backend/.env.example backend/.env
```

Edit `backend/.env` with your object store credentials and Zarr path:

```bash
# Minimum required
ZARR_STORE_URL=s3://your-bucket/path/to/data.zarr
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=eu-west-1

# Optional — defaults are usually fine
REDIS_URL=redis://localhost:6379
TILE_CACHE_TTL=3600
DASK_THREADS=4
COLORMAP_DEFAULT=viridis
```

For GCS, replace the AWS variables with:

```bash
ZARR_STORE_URL=gcs://your-bucket/data.zarr
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

---

## 2. Backend — local development

```bash
cd backend

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Start the dev server with auto-reload
uvicorn app.main:app --reload --port 8000 --log-level info
```

The API is now available at `http://localhost:8000`. Verify it with:

```bash
curl http://localhost:8000/api/datasets
```

You should get a JSON list of the datasets discovered from your Zarr store's `.zmetadata`.

### Start Redis locally (if not using Docker)

```bash
# macOS
brew install redis && brew services start redis

# Ubuntu
sudo apt install redis-server && sudo systemctl start redis

# Or just use Docker for Redis alone
docker run -d -p 6379:6379 redis:7-alpine
```

### Run tests

```bash
pytest tests/ -v
```

The test suite uses a synthetic in-memory Zarr store fixture so no object store credentials are needed to run tests.

---

## 3. Frontend — local development

```bash
cd frontend

# Install dependencies
npm install

# Start the dev server
npm run dev
```

The viewer is at `http://localhost:5173`. It proxies `/api` and `/ws` to the backend on port 8000, so both need to be running.

### Build for production

```bash
npm run build
# Output is in frontend/dist/
```

### Type-check without building

```bash
npm run type-check
```

---

## 4. Prepare your Zarr data

If your existing Zarr data has suboptimal chunking, rewrite it before deploying. The recommended chunk layout is `[1, 256, 256]` (time, lat, lon):

```python
import xarray as xr
import numcodecs

# Open your existing data
ds = xr.open_zarr("s3://your-bucket/original.zarr", consolidated=True)

# Rewrite with tile-aligned chunks
encoding = {
    var: {
        "chunks": [1, 256, 256],
        "compressor": numcodecs.Blosc(cname="zstd", clevel=3,
                                       shuffle=numcodecs.Blosc.BITSHUFFLE),
        "dtype": "float32",
    }
    for var in ds.data_vars
}

ds.to_zarr(
    "s3://your-bucket/data.zarr",
    encoding=encoding,
    consolidated=True,
    mode="w",
)
```

This is a one-time operation. Run it on a machine with fast access to the source bucket (ideally in the same cloud region) to minimise transfer time.

---

## 5. Docker Compose — production

The `docker-compose.yml` at the project root starts four containers: the FastAPI backend, Redis, the Vite-built frontend served by Nginx, and Nginx as the reverse proxy.

```yaml
services:
  backend:
    build: ./backend
    env_file: ./backend/.env
    expose:
      - "8000"
    depends_on:
      - redis
    deploy:
      replicas: 1

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

  frontend:
    build: ./frontend
    expose:
      - "80"

  nginx:
    image: nginx:1.25-alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - backend
      - frontend

volumes:
  redis_data:
```

### Backend `Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["gunicorn", "app.main:app",
     "--workers", "4",
     "--worker-class", "uvicorn.workers.UvicornWorker",
     "--bind", "0.0.0.0:8000",
     "--timeout", "60"]
```

### Frontend `Dockerfile`

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.25-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx-frontend.conf /etc/nginx/conf.d/default.conf
```

### `nginx/nginx.conf`

```nginx
worker_processes auto;

events { worker_connections 1024; }

http {
  include mime.types;

  proxy_cache_path /var/cache/nginx/tiles
      levels=1:2
      keys_zone=tiles:10m
      max_size=2g
      inactive=1h
      use_temp_path=off;

  upstream backend {
    server backend:8000;
    keepalive 32;
  }

  server {
    listen 80;

    # Tile requests — cache at Nginx level
    location /api/tiles/ {
      proxy_pass         http://backend;
      proxy_http_version 1.1;
      proxy_set_header   Connection "";
      proxy_cache        tiles;
      proxy_cache_key    "$uri$is_args$args";
      proxy_cache_valid  200 1h;
      proxy_cache_use_stale error timeout updating;
      add_header         X-Cache-Status $upstream_cache_status;
    }

    # WebSocket
    location /ws {
      proxy_pass         http://backend;
      proxy_http_version 1.1;
      proxy_set_header   Upgrade $http_upgrade;
      proxy_set_header   Connection "upgrade";
    }

    # Other API routes — no Nginx cache
    location /api/ {
      proxy_pass         http://backend;
      proxy_http_version 1.1;
      proxy_set_header   Connection "";
    }

    # Frontend static files
    location / {
      proxy_pass         http://frontend;
    }
  }
}
```

### Start everything

```bash
docker compose up --build -d
docker compose logs -f
```

The viewer is at `http://localhost`.

---

## 6. Production checklist

Before going live, verify each item:

- [ ] `ZARR_STORE_URL` points to the correctly chunked Zarr store (`[1, 256, 256]`, `consolidated=True`)
- [ ] Object store credentials are set in the environment (not committed to git)
- [ ] Redis persistence is configured (`appendonly yes` in redis.conf) if tile cache durability matters
- [ ] Backend runs with at least 4 Gunicorn workers (`--workers 4`)
- [ ] Nginx tile cache directory has sufficient disk space (2GB default in the config)
- [ ] HTTPS is configured (add SSL certs to the Nginx config; consider Certbot)
- [ ] `TILE_CACHE_TTL` is appropriate for how often your data updates (default 3600s)
- [ ] CORS is configured in FastAPI if the frontend is on a different domain
- [ ] Health check endpoint is available (`GET /api/health`) for load balancer probes

### Add a health check endpoint

In `app/api/router.py`:

```python
@router.get("/health")
async def health():
    return {"status": "ok"}
```

---

## 7. Scaling

### Horizontal backend scaling

Run multiple backend replicas behind a load balancer. Because all state is in Redis (not in the process), any replica can serve any request. Update `docker-compose.yml`:

```yaml
backend:
  deploy:
    replicas: 4
```

Add Nginx upstream balancing:

```nginx
upstream backend {
  server backend_1:8000;
  server backend_2:8000;
  server backend_3:8000;
  server backend_4:8000;
  keepalive 64;
}
```

### Dask cluster for heavy loads

For large datasets or high-concurrency tile generation, connect to an external Dask cluster instead of the built-in `LocalCluster`. Set in `.env`:

```bash
DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786
```

Then in `workers/dask_client.py`:

```python
from dask.distributed import Client
from app.config import settings

async def start_dask() -> Client:
    if settings.dask_scheduler_address:
        return await Client(settings.dask_scheduler_address, asynchronous=True)
    return await Client(
        n_workers=settings.dask_threads,
        threads_per_worker=1,
        asynchronous=True,
    )
```

### Redis cluster

For very high cache hit rates (millions of tiles), Redis Cluster spreads the keyspace across multiple nodes. Point `REDIS_URL` at a Redis Cluster endpoint — the `redis.asyncio` client supports cluster mode transparently.
