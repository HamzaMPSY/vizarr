# Frontend

The frontend is a React + TypeScript + Vite app. The active viewer renders
backend TileJSON as a MapLibre raster source/layer. Deck.gl was part of the
original blueprint, but it is not used by the checked-in viewer.

## Current folder structure

```
frontend/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── components/
│   │   ├── MapView.tsx
│   │   └── Sidebar.tsx
│   ├── api/
│   │   └── endpoints.ts
│   ├── hooks/
│   │   ├── useBrowserMultiscale.ts
│   │   ├── useDatasetInvalidation.ts
│   │   ├── useDatasets.ts
│   │   ├── useDebouncedValue.ts
│   │   └── useTilePrefetch.ts
│   ├── lib/
│   │   └── multiscale.ts
│   ├── store/
│   │   └── mapStore.ts
│   ├── styles.css
│   └── types.ts
├── index.html
├── package.json
├── vite.config.ts
└── Dockerfile
```

There is no Deck.gl `TileLayer` in the current code. The MapLibre path now has
a debounced prefetch hook rather than a dedicated Web Worker.

## Application flow

`src/App.tsx` loads datasets and variables through TanStack Query, selects the
first available dataset/variable, and syncs default display range/colormap from
the selected variable metadata. It also subscribes to `/ws/datasets` and shows a
compact dataset-sync status chip over the map.

`src/components/Sidebar.tsx` exposes dataset, render mode, variable/composite,
time, colormap, and range controls. Composite mode is shown only when the
selected dataset advertises `composite_styles`; colormap/range controls continue
to apply to single-band rendering.

`src/components/MapView.tsx`:

- requests TileJSON for the active dataset and selected band or composite style;
- fits the map to TileJSON bounds when the active layer changes;
- adds a MapLibre raster source using the TileJSON tile template;
- attempts browser-native multiscale rendering when the serving profile and
  multiscale metadata are explicitly compatible;
- falls back to server-rendered tiles when browser-native rendering is not
  ready, unsupported, too large, or fails;
- debounces viewport-driven prefetch work so it does not run on every move
  frame;
- shows guards when the current zoom is below the data/detail zoom;
- tracks tile loading through MapLibre `dataloading`, `idle`, and `error`
  events.

## State and data

`src/store/mapStore.ts` holds tile-affecting state:

- active dataset id;
- active variable id;
- render mode, either `band` or `composite`;
- active composite style id;
- time index;
- colormap;
- vmin/vmax;
- current map view state.

Dataset metadata types include optional `native_resolution_m`, `crs_wkt`, and
`crs_authority` fields. The current UI does not render CRS details directly, but
the fields are available to future inspectors and diagnostics.

`src/hooks/useDatasets.ts` wraps these API calls:

- datasets;
- variables;
- colormaps;
- colormap palettes;
- TileJSON;
- serving profiles.

`src/hooks/useDatasetInvalidation.ts` opens `/ws/datasets`, listens for
`datasets.invalidate` events, and invalidates dataset, variable,
serving-profile, and TileJSON query roots in TanStack Query.

`src/api/endpoints.ts` centralizes URL construction for:

- `/api/datasets`
- `/api/datasets/{dataset_id}`
- `/api/datasets/{dataset_id}/variables`
- `/api/datasets/{dataset_id}/serving-profile`
- `/api/tilejson/{dataset_id}/{variable}`
- `/api/tiles/{dataset_id}/{variable}/{z}/{x}/{y}`
- `/api/colormaps`
- `/api/colormaps/{name}/palette`
- `/ws/datasets`

## Browser-native multiscale status

`src/lib/multiscale.ts` can load consolidated multiscale metadata, select a
level, read uncompressed float32 chunks through the dataset-scoped multiscale
proxy, compose a plane, and render it to a data URL.

`src/hooks/useBrowserMultiscale.ts` gates the path with the serving profile,
Zarr format/consolidation flags, chunk-layout availability, level compressor and
filter metadata, pixel/chunk limits, and profile gaps. When the gate passes,
MapView displays the generated image as a MapLibre image source. Otherwise, it
keeps the server TileJSON raster layer active.

## Prefetch and range seeding

`src/hooks/useTilePrefetch.ts` runs from debounced viewport state. It computes
the current XYZ tile plus a radius-2 surrounding ring, skips entries already in
the browser Cache API, and fetches missing URLs through the normal TileJSON tile
template.

The first center-tile response seeds `vmin`/`vmax` from `X-Data-Vmin` and
`X-Data-Vmax` when no explicit range is already set. MapLibre still owns visible
tile loading; the prefetch hook only warms nearby URLs after the viewport
settles.

## Vite proxy

`frontend/vite.config.ts` proxies `/api` and `/ws` to:

```text
process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000"
```

The `/ws` proxy enables WebSocket upgrades for local Vite development. In
production-style compose, Nginx handles the same upgrade proxying.

## Dependencies

The active frontend depends on:

- React;
- TanStack Query;
- Zustand;
- MapLibre GL;
- `react-map-gl/maplibre`;
- Vite.

Deck.gl is not part of the active rendering stack unless a future ticket adds it
back intentionally.
