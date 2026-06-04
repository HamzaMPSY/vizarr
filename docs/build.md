# Build And Run Guide

This guide reflects the checked-in implementation and compose files.

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Backend runtime |
| Node.js | 20+ | Frontend build and dev server |
| Docker or Podman | Recent stable | Container runtime |
| Docker Compose or Podman Compose | Recent stable | Multi-container runs |
| Redis | 7+ | Tile cache when running outside compose |
| OCI CLI | Current | Local OCI session auth for `oci_zarr` mode |

Synthetic mode does not need OCI credentials. OCI mode expects a valid OCI
profile/session on the host, normally under `~/.oci`.

## Environment files

| File | Purpose |
|---|---|
| `backend/.env.example` | Sanitized synthetic/default backend settings |
| `backend/.env.oci.example` | Sanitized OCI settings template |
| `backend/.env.production.example` | Sanitized production-style compose template |
| `backend/.env` | Local private backend settings, ignored by git |
| `backend/.env.production` | Private production-style compose settings, ignored by git |

For OCI local development:

```bash
cp backend/.env.oci.example backend/.env
```

Then fill:

```bash
OCI_CONFIG_PROFILE=prof
OCI_CONFIG_FILE=/home/app/.oci/config
OCI_AUTH_MODE=auto
OCI_NAMESPACE=<object-storage-namespace>
OCI_BUCKET=<bucket-name>
OCI_PREFIX=<prefix-or-zarr-store>
```

`OCI_AUTH_MODE=auto` supports:

- `security_token` profiles created by `oci session authenticate`;
- normal API-key profiles with `user`, `fingerprint`, and `key_file`;
- `resource_principal` in supported OCI runtimes;
- explicit `instance_principal` on OCI compute instances.

For autonomous runs, prefer `OCI_AUTH_MODE=api_key`,
`OCI_AUTH_MODE=instance_principal`, or `OCI_AUTH_MODE=resource_principal`.
Session-token auth is useful for local development but it is not autonomous.
On OCI compute, prefer `OCI_AUTH_MODE=instance_principal` so the service does
not depend on a browser-refreshed local session token.

Refresh local OCI session auth before starting the backend when using
`OCI_AUTH_MODE=security_token` or a session-token profile in `auto` mode:

```bash
oci session authenticate --profile-name prof
```

You can keep an already-authenticated local session alive while OCI still allows
refresh by running:

```bash
python3 scripts/oci_session_watchdog.py \
  --profile-name prof \
  --config-file ~/.oci/config \
  --loop
```

This watchdog does not bypass OCI browser authentication. If the token is
already expired or OCI refuses refresh, it prints the interactive
`oci session authenticate ...` command that must be run by a user.

The tracked examples intentionally do not include AWS access keys, GCS service
account paths, or live bucket names.

## URL and port matrix

| Mode | Command | Frontend URL | API URL | Health URL | Notes |
|---|---|---|---|---|---|
| Backend only local | `uvicorn app.main:app --reload --port 8000` from `backend/` | None | `http://localhost:8000/api` | `http://localhost:8000/api/healthz` | Use Redis locally or rely on cache fallback behavior |
| Frontend Vite local | `npm run dev` from `frontend/` | `http://localhost:5173` | Proxies to `http://127.0.0.1:8000/api` by default | Backend health above | Override with `VITE_PROXY_TARGET` |
| Dev compose | `docker compose -f docker-compose.dev.yml up --build` | `http://localhost:5173` | `http://localhost:8001/api` | `http://localhost:8001/api/healthz` | Backend hot reload, Vite HMR, mounts host `~/.oci` |
| Production-style compose | `docker compose up --build` | `http://localhost:8000` | `http://localhost:8000/api` through Nginx | `http://localhost:8000/api/healthz` | Backend is internal-only; Nginx proxies `/api`, `/api/tiles`, `/ws`, and frontend traffic |
| VM handoff local backend | See `VM_HANDOFF.md` | `http://<vm-host>:5173` | `http://<vm-host>:8015/api` | `http://<vm-host>:8015/api/healthz` | Uses VM-local OCI profile/session |

There is no `/api/health` alias. Use `/api/healthz`.

## Backend: local development

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000 --log-level info
```

Verify:

```bash
curl http://localhost:8000/api/healthz
curl http://localhost:8000/api/datasets
```

Run tests:

```bash
pytest tests/ -v
```

## Frontend: local development

```bash
cd frontend
npm install
npm run dev
```

Vite defaults to `http://localhost:5173` and proxies `/api` to
`http://127.0.0.1:8000`. For a different backend:

```bash
VITE_PROXY_TARGET=http://127.0.0.1:8001 npm run dev
```

Build:

```bash
npm run build
```

Type check:

```bash
npm run type-check
```

## Dev compose

Use dev compose for normal iteration:

```bash
docker compose -f docker-compose.dev.yml up --build
```

or:

```bash
podman compose -f docker-compose.dev.yml up --build
```

What it does:

- backend runs `uvicorn --reload` inside the container on port `8000`;
- backend is published on host port `${API_PORT:-8001}`;
- frontend runs Vite on host port `${APP_PORT:-5173}`;
- frontend proxies `/api` to `http://backend:8000`;
- Redis is published on host port `6379`;
- host `${HOME}/.oci` is mounted read-only so OCI session token paths keep
  working inside the backend container.

### Compose proxy and registry settings

If container builds cannot reach public registries from a company network, copy
the compose env template and fill the proxy or internal registry values:

```bash
cp .env.compose.example .env
```

Common values:

```text
HTTP_PROXY=http://your-proxy-host:port
HTTPS_PROXY=http://your-proxy-host:port
NO_PROXY=localhost,127.0.0.1,backend,frontend,redis,nginx
NPM_CONFIG_REGISTRY=https://your-company-npm-registry/
PIP_INDEX_URL=https://your-company-pypi/simple
PIP_TRUSTED_HOST=your-company-pypi
```

If npm fails because the network intercepts TLS, `NPM_CONFIG_STRICT_SSL=false`
and `NODE_TLS_REJECT_UNAUTHORIZED=0` can unblock an internal POC. Prefer
installing the company root CA in container images for longer-lived setups.

## Production-style compose

```bash
cp backend/.env.production.example backend/.env.production
BACKEND_ENV_FILE=./backend/.env.production docker compose up --build
```

For a synthetic demo, the default template is enough and this also works:

```bash
docker compose up --build
```

What it does:

- backend is exposed internally on `backend:8000`;
- backend is not published directly on the host by default;
- backend runs Uvicorn with `${BACKEND_WORKERS:-2}` workers;
- frontend serves the production Vite build on the internal compose network;
- Nginx is published on host port `${APP_PORT:-8000}`;
- Nginx proxies `/api/` to the backend and `/` to the frontend;
- Nginx caches successful `/api/tiles/` responses in a named disk cache and
  reports cache state with `X-Cache-Status`;
- Nginx proxies `/ws/` to the backend with WebSocket upgrade headers.
- Redis persists append-only data in the named `redis_data` volume.

## OCI discovery checks

When `STORAGE_BACKEND=oci_zarr`, useful checks are:

```bash
curl http://localhost:8001/api/storage/prefixes
curl http://localhost:8001/api/storage/zarr-stores
curl 'http://localhost:8001/api/storage/inspect-zarr?zarr_path=<path>'
curl http://localhost:8001/api/datasets
```

If the backend returns `503` with an OCI auth message, refresh the host OCI
session and restart the backend container if necessary.

## OCI browser smoke

Use the smoke harness after starting the dev stack in OCI mode:

```bash
python3 scripts/oci_browser_smoke.py \
  --api-url http://localhost:8001/api \
  --frontend-url http://localhost:5173
```

The script verifies:

- backend health at `/api/healthz`;
- OCI-backed dataset discovery through `/api/datasets`;
- variables for the selected OCI dataset;
- TileJSON bounds and tile URL construction;
- one center tile request returning `200 image/webp`;
- the frontend HTML shell.

It exits successfully with a `SKIP:` message when no OCI-backed dataset is
returned or when the backend reports expired/missing OCI auth. It does not store
OCI namespace, bucket, profile, token, or dataset values.

Optional selectors:

```bash
VIZARR_OCI_DATASET_ID=<dataset-id> \
VIZARR_OCI_VARIABLE=<band-or-composite-id> \
python3 scripts/oci_browser_smoke.py
```

Complete the visual browser pass with the checklist printed by the script:

- open the frontend URL;
- select the same dataset and variable/composite;
- confirm the map auto-fits the dataset footprint;
- confirm the visible map raster layer or sidebar tile preview is populated.

## OCI performance benchmark

Use the benchmark harness when the dev stack is running in OCI mode and you need
repeatable cold/warm timing for real object-storage-backed cubes:

```bash
python3 scripts/oci_performance_benchmark.py \
  --api-url http://localhost:8001/api \
  --frontend-url http://localhost:5173 \
  --tile-radius 1 \
  --output .cache/benchmarks/oci-benchmark.json
```

The command verifies health, dataset discovery, variables, serving profile,
TileJSON, a center viewport tile set, a repeated warm tile pass, and the
frontend HTML shell. It records tile latency plus `X-Cache-Status`,
`X-Representation`, `X-Execution-Path`, and `X-Browse-Source` for every tile.
When `TILE_DEBUG_HEADERS_ENABLED=true` is set on the backend, it also records
tile timing and object I/O diagnostics such as object GET counts, byte-range GET
counts, approximate bytes read, shard index reads, and Zarr chunk reads.

It exits successfully with `SKIP:` when the running stack has no OCI-backed
dataset metadata or when OCI auth is unavailable. It does not store OCI
namespace, bucket, profile, token, or dataset secrets.

Optional budget gates make the command fail when performance or routing drifts:

```bash
python3 scripts/oci_performance_benchmark.py \
  --metadata-p95-budget-ms 750 \
  --cold-tile-p95-budget-ms 2500 \
  --warm-tile-p95-budget-ms 250 \
  --expected-representation browse
```

Use `--forbid-serving` for zoom bands that should be covered by browse or
pyramid artifacts. Use the browser rendering probe when Playwright is available
in the environment:

```bash
PLAYWRIGHT_MODULE=playwright \
python3 scripts/oci_performance_benchmark.py \
  --playwright-command 'node scripts/browser_multiscale_probe.cjs {frontend_url}'
```

The probe reads `.map-shell` data attributes such as `data-render-mode`,
`data-browser-native-mode`, `data-browser-gpu-status`, selected
dataset/variable/time/zoom, and the pixel/chunk/byte budget counters, then
prints JSON containing `renderMode` as `browser-gpu`, `browser-native`, or
`server-tiles`.

For a local mocked GPU-path check that does not require OCI credentials:

```bash
VIZARR_BROWSER_PROBE_SCENARIO=browser-gpu \
VIZARR_EXPECTED_FRONTEND_RENDER_MODE=browser-gpu \
node scripts/browser_multiscale_probe.cjs http://localhost:5173
```

Use the pan/zoom smoke option after prefetch changes:

```bash
VIZARR_BROWSER_PROBE_INTERACTION=pan-zoom \
node scripts/browser_multiscale_probe.cjs http://localhost:5173
```

The frontend prefetch planner also has a dependency-free node test that runs in
the frontend container:

```bash
podman exec vizarr_frontend_1 node scripts/tile_prefetch_planner_test.mjs
```

For repeatable coverage across cube families, use the secret-free matrix in
`docs/oci-cube-matrix.example.json` and provide local private selectors through
the environment variables named by that matrix:

```bash
VIZARR_MATRIX_EPSG4326_SHARDED_DATASET_ID=<local-dataset-id> \
VIZARR_MATRIX_EPSG4326_SHARDED_VARIABLE=NDVI \
python3 scripts/oci_performance_benchmark.py \
  --matrix docs/oci-cube-matrix.example.json \
  --matrix-entry epsg4326-sharded-time-band-yx
```

## Data preparation guidance

The current OCI implementation supports projected multiband Zarr v3 stores and
generated browse/multiscale artifacts. The ideal source layout depends on the
dataset, but tile serving works best when spatial chunks are close to tile size.

For lat/lon scalar datasets, `[1, 256, 256]` time/spatial chunking is still a
good target. For projected imagery, the adapter reads source chunks and can use
browse or multiscale artifacts for lower zooms.

## Production checklist

- [ ] Use `/api/healthz` for load balancer probes.
- [ ] Keep private OCI settings in `backend/.env.production`,
      `backend/.env`, or deployment secrets, not in tracked example files.
- [ ] Confirm `STORAGE_BACKEND` is correct for the deployment.
- [ ] Confirm Redis is reachable by the backend.
- [ ] Confirm `OCI_CONFIG_FILE` and profile/resource-principal auth match the
      runtime environment.
- [ ] Use `OCI_AUTH_MODE=instance_principal` on OCI compute, or another
      non-interactive mode for autonomous production runs.
- [ ] Set `CORS_ALLOWED_ORIGINS` to the exact public viewer origin; production
      does not wildcard CORS when this value is empty.
- [ ] Set `AUTH_API_KEYS` through deployment secrets and rotate by adding the
      new key, updating clients, verifying, then removing the old key.
- [ ] Set `API_KEY_RATE_LIMIT_PER_MINUTE` to a non-zero value for private
      deployments that need per-key throttling.
- [ ] Confirm browse or multiscale artifacts exist for datasets that need fast
      first-view performance.
- [ ] Add HTTPS for production traffic.
- [ ] Verify Nginx tile cache and `/ws` behavior against the target runtime.

## Scaling notes

The current app keeps request coordination simple: backend processes share tile
bytes through Redis and serve object data directly from OCI. Horizontal backend
scaling is possible if every replica has the same OCI auth and Redis settings.

External Dask scheduling is not wired into the checked-in request path. Treat it
as future architecture until implemented. Direct source tile renders are
currently bounded with per-request chunk concurrency and optional object/chunk
read budgets.
