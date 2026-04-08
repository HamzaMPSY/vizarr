# Dev Workflow

## Hot Reload

Use the development compose stack instead of the production one when you want file changes to apply without rebuilding images.

```bash
podman compose -f docker-compose.dev.yml up
```

Or with Docker:

```bash
docker compose -f docker-compose.dev.yml up
```

## What reloads automatically

- Backend: `uvicorn --reload` watches `backend/app`
- Frontend: Vite watches `frontend/src`

## OCI Local Development

The dev backend is wired for local OCI session-based auth:

- it mounts `${HOME}/.oci` read-only into the backend container
- it uses profile `prof`
- it reads the OCI config from `${HOME}/.oci/config`

This is important because OCI session profiles use token and key files with absolute host paths. The container mount preserves those host paths so the OCI SDK can resolve them correctly.

When the OCI CLI session expires, refresh it on the host and restart the backend container if necessary.

## Dev URLs

- Frontend dev app: `http://localhost:5173`
- Backend API: `http://localhost:8001`
- Backend health: `http://localhost:8001/api/healthz`

Useful OCI discovery endpoints:

- `http://localhost:8001/api/storage/objects`
- `http://localhost:8001/api/storage/prefixes`
- `http://localhost:8001/api/storage/zarr-stores`
- `http://localhost:8001/api/storage/inspect-zarr?zarr_path=<path>`
- `http://localhost:8001/api/storage/zarr-json?zarr_path=<path>`

## Notes

- The frontend container keeps `node_modules` in a named volume so file mounts do not wipe installed packages.
- The frontend still runs `npm install` on startup to keep dependencies present in the dev volume.
- Use `docker-compose.yml` only for production-style builds and Nginx testing.
- Use `backend/.env.oci.example` as the base config when working against OCI Object Storage.
