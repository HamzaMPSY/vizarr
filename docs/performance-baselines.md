# Performance Baselines

This document records the baseline gates used by CI and local release checks.

## CI baseline

The default CI workflow runs without private OCI credentials:

- backend unit tests with `pytest`;
- frontend TypeScript type-check;
- frontend production build.

These checks prove the synthetic route surface, tile generation, cache behavior,
auth gate, and frontend compile path still work on a clean checkout.

## Synthetic benchmark baseline

Live OCI benchmarks are environment-specific and can skip when credentials are
absent. CI should still run a synthetic smoke/baseline path so basic latency and
cache behavior remain visible on every pull request.

Initial budget targets:

| Check | Budget |
|---|---:|
| Backend unit tests | pass |
| Frontend type-check | pass |
| Frontend build | pass |
| Synthetic tile route | returns WebP and `X-Cache-Status` |
| Repeated synthetic tile | cache hit |

## Live OCI baseline

Use `scripts/oci_performance_benchmark.py` for private live stores. A live run
should publish:

- metadata p95;
- cold tile p95;
- warm tile p95;
- cache hit rate;
- selected representation by zoom band;
- active frontend render mode, including `server-tiles`, `browser-native`, or
  `browser-gpu` when a Playwright probe is configured;
- browser-GPU readiness, renderer, and fallback reason when observable;
- object GET count and bytes read when debug headers are enabled.

Live OCI benchmark failures should be interpreted against the cube matrix and
artifact coverage. A valid cube can still be slow when browse or multiscale
artifacts are missing.
