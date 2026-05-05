# Vizarr Agent Guide

This repository is a satellite Zarr viewer. Treat the Markdown docs and the
checked-in code as the contract for implementation work.

## Architecture

- Backend: Python 3.11+, FastAPI, Xarray, NumPy, Redis, OCI/ocifs.
- Frontend: React, TypeScript, Vite, MapLibre, TanStack Query, Zustand.
- Data path: keep synthetic lat/lon demo tiles separate from OCI-backed
  projected multiband imagery and browser-served Zarr proxy paths.
- The backend serves image tiles and read-only Zarr proxy objects. Do not expose
  write paths for remote stores.
- OCI local development should reuse the host OCI session config. Do not add
  long-lived OCI credentials to the repo.

## Setup

- Backend local dev:
  `cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Backend server:
  `cd backend && uvicorn app.main:app --reload --port 8000`
- Frontend local dev:
  `cd frontend && npm install && npm run dev`
- Dev compose stack:
  `podman compose -f docker-compose.dev.yml up`
  or `docker compose -f docker-compose.dev.yml up`

## Validation

Choose the narrowest validation that proves the change.

- Backend tests:
  `cd backend && pytest tests -q`
- Frontend type check:
  `cd frontend && npm run type-check`
- Frontend build:
  `cd frontend && npm run build`
- Dev health check:
  `curl -fsS http://localhost:8001/api/healthz`

For OCI changes, prove each step explicitly: auth, object listing, Zarr root
detection, metadata inspection, dataset catalog exposure, and tile rendering.

## Change Discipline

- Read `README.md`, `docs/architecture.md`, and the task-relevant doc before
  large changes.
- Keep edits scoped to the issue. Do not reformat or refactor unrelated code.
- Prefer existing module boundaries over new abstractions.
- Do not commit secrets, generated caches, local environment files, logs, or
  workspace directories.
- Before handoff, report the validation commands run and any command that could
  not be run.
