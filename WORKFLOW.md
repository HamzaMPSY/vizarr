---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: "__SYMPHONY_LINEAR_PROJECT_SLUG__"
  active_states:
    - Todo
    - In Progress
    - Rework
    - Merging
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done
polling:
  interval_ms: 5000
workspace:
  root: $SYMPHONY_WORKSPACE_ROOT
hooks:
  after_create: |
    repo_url="${SYMPHONY_SOURCE_REPO_URL:-https://github.com/HamzaMPSY/vizarr.git}"
    git clone --depth 1 "$repo_url" .
    git config rerere.enabled true
    git config rerere.autoupdate true
agent:
  max_concurrent_agents: 2
  max_turns: 20
codex:
  command: codex --config shell_environment_policy.inherit=all app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
---

You are working on Linear ticket `{{ issue.identifier }}` for the Vizarr
repository.

{% if attempt %}
Continuation context:

- This is retry attempt #{{ attempt }} because the ticket is still in an active
  state.
- Resume from the current workspace state. Do not restart investigation unless
  the issue content or local code changed.
{% endif %}

Issue context:

- Identifier: {{ issue.identifier }}
- Title: {{ issue.title }}
- Current status: {{ issue.state }}
- Labels: {{ issue.labels }}
- URL: {{ issue.url }}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

## Operating Rules

1. Work only in the provided workspace copy.
2. Operate autonomously end to end unless blocked by missing required auth,
   permissions, secrets, or an unavailable external service.
3. Keep one persistent Linear comment titled `## Codex Workpad`; update it in
   place for plan, acceptance criteria, validation, notes, blockers, and final
   handoff.
4. Do not create extra status comments unless comment editing is unavailable.
5. Keep Linear state and PR status aligned with real progress.
6. If the team lacks a named state from this workflow, use the closest
   equivalent state of the same type and record that mapping in the workpad.
7. Do not commit secrets, local `.env` files, caches, logs, or Symphony
   workspaces.

## Status Flow

- `Backlog`: out of scope for the active workflow. Do not modify.
- `Todo`: move to `In Progress`, create/update the workpad, then begin.
- `In Progress`: implement and validate.
- `Human Review` or equivalent review state: do not code; wait for review or
  status changes.
- `Rework`: re-read issue and review feedback, update the plan, implement, and
  validate again.
- `Merging`: land the approved PR using the `land` skill.
- `Done`, `Closed`, `Cancelled`, `Canceled`, `Duplicate`: terminal; stop.

## Kickoff Checklist

1. Fetch the issue by identifier and confirm its current state.
2. Find or create the single `## Codex Workpad` comment.
3. Add an environment stamp at the top:

   ```text
   <hostname>:<absolute-workdir>@<short-sha>
   ```

4. Record a concise plan, acceptance criteria, and validation plan.
5. Reproduce the issue or establish the current behavior before editing.
6. Sync with `origin/main` using the `pull` skill before implementation.

## Vizarr Project Rules

- Read `AGENTS.md`, `README.md`, `docs/architecture.md`, and task-relevant docs.
- Preserve the documented backend/frontend split:
  - backend: FastAPI tile server, OCI/Zarr discovery, tile rendering, cache,
    read-only Zarr proxy;
  - frontend: React/Vite viewer, MapLibre map, TanStack Query server
    state, Zustand UI/map state.
- Keep synthetic lat/lon demo data separate from OCI-backed projected imagery.
- For OCI work, prove auth, object listing, Zarr root detection, metadata
  inspection, dataset catalog exposure, and tile rendering.
- Prefer targeted tests first, then broader checks when the blast radius is
  shared or user-facing.

## Validation Commands

Choose commands that match the files changed:

- Backend:
  `cd backend && pytest tests -q`
- Frontend type check:
  `cd frontend && npm run type-check`
- Frontend build:
  `cd frontend && npm run build`
- Dev compose:
  `podman compose -f docker-compose.dev.yml up`
  or `docker compose -f docker-compose.dev.yml up`
- Health check when the dev backend is running:
  `curl -fsS http://localhost:8001/api/healthz`

Document every validation result in the workpad. If a required validation cannot
run because of missing credentials, network, runtime, or external services, mark
the blocker precisely and move to review only when no in-session fallback is
available.

## PR And Handoff

1. Commit coherent changes with the `commit` skill.
2. Push and create or update a PR with the `push` skill.
3. Attach or link the PR to the Linear issue.
4. Run a PR feedback sweep before moving to review:
   - top-level PR comments,
   - inline review comments,
   - review summaries,
   - CI/check failures.
5. Address feedback or respond with concise pushback before handoff.
6. Move the issue to `Human Review` or the closest equivalent only after
   acceptance criteria and validation are complete.
7. Final response must report completed work, PR URL, validation evidence, and
   blockers only.
