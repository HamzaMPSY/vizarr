# Zarr 3 Native Reader Migration Spike

Ticket: `.tickets/043-native-zarr3-reader-migration-spike.md`

## Summary

Recommendation: replace selected metadata reads only after the layout adapter
work provides a stable abstraction. Keep Vizarr's custom chunk and indexed
shard reader for tile rendering.

Native `zarr-python` 3 can read a representative Zarr v3 indexed-sharding store
correctly, but the direct `open_group` / `get_basic_selection` path does not
surface the object GET, byte-range, chunk, and shard-index counters that Vizarr
uses in tile debug headers and direct-read budgets. A full migration would
reduce local maintenance but would weaken the observability and budget controls
that currently prevent slow OCI-backed tiles from becoming silent latency
failures.

## Fixture And Method

The repeatable spike harness is `scripts/zarr3_native_reader_spike.py`.

It creates a temporary local Zarr v3 store with:

- array: `bands`
- shape: `[1, 1, 4, 4]`
- outer shard/chunk shape: `[1, 1, 4, 4]`
- inner chunk shape: `[1, 1, 2, 2]`
- codec path: `sharding_indexed` with `bytes` chunks and end-of-object shard
  index
- dimension names: `time`, `band`, `y`, `x`

The harness reads the same `time=0`, `band=0`, `y=1:4`, `x=1:4` window through:

- native `zarr-python` 3.1.6 with `open_group(..., zarr_format=3)` and
  `get_basic_selection`;
- Vizarr's `read_store_metadata`, `parse_array_metadata`, and `load_4d_window`
  path using a local connector that records object and byte-range counters.

## Results

Last run:

```bash
backend/.venv/bin/python3.11 scripts/zarr3_native_reader_spike.py
```

The native and custom readers both returned:

```text
[[5, 6, 7],
 [9, 10, 11],
 [13, 14, 15]]
```

Measured output from the local fixture:

| Area | Native zarr-python | Vizarr custom reader |
| --- | --- | --- |
| Data parity | Matches expected and custom output | Matches expected and native output |
| Elapsed time | 3.364 ms | 2.183 ms |
| Object/range counts visible to Vizarr | No | Yes |
| Object GET count during window read | Not exposed | 5 |
| Byte-range count during window read | Not exposed | 5 |
| Chunk reads | Not exposed | 4 |
| Shard-index reads | Not exposed | 1 |
| Connector metadata reads | Not instrumented | 2 text reads |
| Connector byte reads | Not instrumented | 4 range reads, 1 tail read |

The timings are local-fixture smoke numbers, not production benchmarks. The
important signal is capability and observability, not the millisecond delta.

## OCI And Auth Notes

The spike did not use live OCI credentials. It therefore does not claim native
`zarr-python` plus `ocifs` is ready for production tile reads.

Before any chunk-read migration, a live OCI run must prove:

- host OCI session auth works through the native store path;
- native reads preserve bounded range behavior for indexed shards;
- object GET, byte-range GET, bytes-read, chunk, and shard-index counts remain
  visible to tile debug headers and budget enforcement;
- tile latency is no worse than the current custom reader on representative
  browse, multiscale, and direct-source datasets.

## Safe Migration Boundary

Safe to explore:

- using native Zarr APIs for selected metadata inspection where reads are not
  on the hot tile path;
- adapting native metadata results into the existing dataset catalog structures;
- placing this behind the future layout adapter registry from ticket `026`.

Keep custom for now:

- direct source tile chunk reads;
- indexed sharding byte-tail and byte-range reads;
- shard-index cache behavior;
- debug-header and direct-read budget counters;
- OCI connector session and cache control.

## Follow-Up Implementation Plan

Candidate follow-up: native Zarr 3 metadata adapter behind the layout adapter
registry.

Expected files:

- `backend/app/core/zarr_v3.py`
- `backend/app/core/dataset_catalog.py`
- `backend/app/core/multiscale_builder.py`
- `backend/tests/test_zarr_v3.py`
- `backend/tests/test_dataset_catalog.py`
- `backend/tests/test_projected_tile_generator.py`
- `docs/compatibility.md`

Test plan:

- keep existing custom-reader tests green;
- add a metadata-only fixture test proving native metadata parses the same
  `shape`, `dimension_names`, chunk shape, dtype, fill value, and sharding
  summary as `parse_array_metadata`;
- run representative catalog discovery against the native metadata adapter;
- run live OCI catalog discovery before enabling it outside local development.

Rollback strategy:

- keep the existing custom metadata/chunk reader as the default;
- gate the native metadata adapter with a config flag or adapter-registry
  selection;
- disable the native adapter without changing tile-generation code if OCI auth,
  metadata compatibility, or catalog latency regresses.

