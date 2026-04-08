# Running the POC

## Requirements

- Docker with Compose, or Podman with Compose support

## Development Mode With Hot Reload

If you want code changes to apply without rebuilding images, use:

```bash
podman compose -f docker-compose.dev.yml up
```

Or:

```bash
docker compose -f docker-compose.dev.yml up
```

This starts:
- Vite dev server on `http://localhost:5173`
- FastAPI with `--reload` on `http://localhost:8001`

See `DEV_WORKFLOW.md` for the development workflow.

## Current Recommendation

Use the development stack first while building the OCI path:

```bash
podman compose -f docker-compose.dev.yml up --build
```

The production-style stack behind Nginx is still useful, but it is slower to iterate on and less convenient for discovery/debug work.

## If you are on a company network

If container builds hang on `npm install` or `pip install`, the build containers probably cannot reach the public registries.

Copy the example env file and fill in your company proxy or internal registry settings:

```bash
cp .env.compose.example .env
```

Typical values to set:

```text
HTTP_PROXY=http://your-proxy-host:port
HTTPS_PROXY=http://your-proxy-host:port
NO_PROXY=localhost,127.0.0.1,backend,frontend,redis,nginx
NPM_CONFIG_REGISTRY=https://your-company-npm-registry/
PIP_INDEX_URL=https://your-company-pypi/simple
PIP_TRUSTED_HOST=your-company-pypi
```

Compose will pass these values into both the frontend and backend builds.

For Oracle networks, prefer your internal Artifactory registry over the public npm registry when available. Example:

```text
NPM_CONFIG_REGISTRY=https://artifactory.oci.oraclecorp.com/api/npm/global-dev-npm/
```

If npm fails with `UNABLE_TO_VERIFY_LEAF_SIGNATURE`, your company network is likely intercepting TLS with a certificate the container does not trust. For a quick internal POC only, you can relax npm TLS validation:

```text
NPM_CONFIG_STRICT_SSL=false
NODE_TLS_REJECT_UNAUTHORIZED=0
```

Use that only on trusted internal networks. The better long-term fix is to install your company root CA into the container image.

## Start with Docker

Make sure the Docker daemon is running first.

```bash
docker compose up --build
```

## Start with Podman

On macOS, start the Podman VM first if it is not already running:

```bash
podman machine start
```

Then run:

```bash
podman compose up --build
```

If the Podman VM itself also needs proxy settings, set them in the same shell before running compose so Podman and the build context inherit them.

## Open the app

The default entrypoint is:

```text
http://localhost:8000
```

The backend API is also published directly at:

```text
http://localhost:8001/api/healthz
```

## Services

- `nginx` exposes the app on port `8000`
- `frontend` serves the Vite production build
- `backend` serves the FastAPI API on port `8001` on the host and `8000` inside the network
- `redis` stores cached tiles

## What you should see

- In synthetic mode:
  - one synthetic dataset
  - variables `temperature` and `precipitation`
  - map overlay plus tile preview
- In OCI mode:
  - discovered datasets from the configured Object Storage prefix
  - variable picker populated from dataset-specific band metadata
  - storage inspection endpoints under `/api/storage/*`

## Notes

- Synthetic mode does not require object store credentials.
- OCI mode requires a mounted local OCI profile or Data Flow resource principals.
- The current real store that has been inspected is a Zarr v3 Landsat cube under `cubes/landsat/`.
- See `docs/oci-integration.md` for the current OCI design and proven findings.
