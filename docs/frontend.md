# Frontend

React 18 + TypeScript + Vite viewer. Renders satellite data tiles on a WebGL map via Deck.gl and MapLibre GL. All server state is managed by TanStack Query; all UI state by Zustand.

---

## Folder structure

```
frontend/
├── src/
│   ├── main.tsx                        # React root, QueryClientProvider
│   ├── App.tsx                         # Root layout: Map + Sidebar
│   │
│   ├── components/
│   │   ├── Map/
│   │   │   ├── MapView.tsx             # DeckGL + MapboxMap composition
│   │   │   ├── SatelliteLayer.tsx      # TileLayer definition + URL builder
│   │   │   ├── MapControls.tsx         # Zoom in/out, reset view buttons
│   │   │   └── TileLoadingBar.tsx      # Top-of-page loading progress bar
│   │   │
│   │   ├── Sidebar/
│   │   │   ├── Sidebar.tsx             # Collapsible panel shell
│   │   │   ├── DatasetList.tsx         # Virtualized dataset browser
│   │   │   ├── VariableSelector.tsx    # Variable tabs/chips
│   │   │   ├── TimeSlider.tsx          # Scrub through time steps
│   │   │   └── FilterPanel.tsx         # Value range, region, date filters
│   │   │
│   │   ├── Colormap/
│   │   │   ├── ColormapPicker.tsx      # Grid of colormap swatches
│   │   │   └── Legend.tsx              # Gradient bar + min/max labels
│   │   │
│   │   └── ui/                         # Shared primitives
│   │       ├── Slider.tsx
│   │       ├── Badge.tsx
│   │       └── Spinner.tsx
│   │
│   ├── hooks/
│   │   ├── useDatasets.ts              # TanStack Query — dataset list
│   │   ├── useVariables.ts             # TanStack Query — variable metadata
│   │   ├── useViewport.ts              # Debounced viewport bbox from Deck.gl
│   │   └── usePrefetch.ts             # Fires adjacent tile prefetch requests
│   │
│   ├── store/
│   │   ├── mapStore.ts                 # Active dataset, variable, time, colormap
│   │   └── uiStore.ts                  # Sidebar open/closed, filter state
│   │
│   ├── api/
│   │   ├── client.ts                   # Fetch wrapper with base URL + error handling
│   │   └── endpoints.ts               # Typed endpoint helpers + tile URL builder
│   │
│   ├── workers/
│   │   └── prefetch.worker.ts          # Web Worker: adjacent tile prediction
│   │
│   ├── utils/
│   │   ├── tileUrl.ts                  # XYZ tile URL construction
│   │   ├── colormap.ts                 # Client-side colormap helpers
│   │   └── coords.ts                   # Geo coordinate utilities
│   │
│   └── types/
│       └── index.ts                    # Shared TypeScript types
│
├── public/
│   └── favicon.svg
│
├── index.html
├── vite.config.ts
├── tsconfig.json
├── tailwind.config.ts
└── package.json
```

---

## Key files

### `src/store/mapStore.ts`

The single source of truth for everything that affects tile fetching. Any component that reads from this store and changes (dataset, variable, time, colormap) automatically causes Deck.gl to re-evaluate `getTileData` and fetch new tiles.

```ts
import { create } from "zustand";

interface MapState {
  datasetId: string | null;
  variable: string | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  viewState: ViewState;
  setDataset: (id: string) => void;
  setVariable: (variable: string) => void;
  setTimeIndex: (i: number) => void;
  setColormap: (name: string) => void;
  setRange: (vmin: number, vmax: number) => void;
  setViewState: (vs: ViewState) => void;
}

export const useMapStore = create<MapState>((set) => ({
  datasetId: null,
  variable: null,
  timeIndex: 0,
  colormap: "viridis",
  vmin: null,
  vmax: null,
  viewState: { longitude: 0, latitude: 20, zoom: 2, pitch: 0, bearing: 0 },
  setDataset: (datasetId) => set({ datasetId, variable: null, timeIndex: 0 }),
  setVariable: (variable) => set({ variable, timeIndex: 0 }),
  setTimeIndex: (timeIndex) => set({ timeIndex }),
  setColormap: (colormap) => set({ colormap }),
  setRange: (vmin, vmax) => set({ vmin, vmax }),
  setViewState: (viewState) => set({ viewState }),
}));
```

### `src/components/Map/SatelliteLayer.tsx`

Defines the Deck.gl `TileLayer` that fetches satellite tiles from the backend. The `id` prop is set to a string that includes all tile-affecting parameters — Deck.gl uses this to detect changes and reload tiles automatically when any parameter changes.

```tsx
import { TileLayer } from "@deck.gl/geo-layers";
import { BitmapLayer } from "@deck.gl/layers";
import { useMapStore } from "@/store/mapStore";
import { buildTileUrl } from "@/api/endpoints";

export function useSatelliteLayer() {
  const { datasetId, variable, timeIndex, colormap, vmin, vmax } = useMapStore();

  if (!datasetId || !variable) return null;

  return new TileLayer({
    id: `satellite-${datasetId}-${variable}-${timeIndex}-${colormap}-${vmin}-${vmax}`,
    data: buildTileUrl({ datasetId, variable, timeIndex, colormap, vmin, vmax }),
    minZoom: 0,
    maxZoom: 12,
    tileSize: 256,
    renderSubLayers: (props) => {
      const { boundingBox } = props.tile;
      return new BitmapLayer(props, {
        data: undefined,
        image: props.data,
        bounds: [
          boundingBox[0][0], boundingBox[0][1],
          boundingBox[1][0], boundingBox[1][1],
        ],
      });
    },
    transitions: { opacity: 300 },
  });
}
```

### `src/api/endpoints.ts`

Centralises all URL construction. The tile URL template is a function so it can be passed directly to Deck.gl's `TileLayer`.

```ts
const BASE = import.meta.env.VITE_API_URL ?? "";

interface TileUrlParams {
  datasetId: string;
  variable: string;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
}

export function buildTileUrl(p: TileUrlParams): string {
  const qs = new URLSearchParams({
    time_index: String(p.timeIndex),
    colormap: p.colormap,
    ...(p.vmin != null && { vmin: String(p.vmin) }),
    ...(p.vmax != null && { vmax: String(p.vmax) }),
  });
  return `${BASE}/api/tiles/${p.datasetId}/${p.variable}/{z}/{x}/{y}?${qs}`;
}

export const api = {
  datasets: () => fetch(`${BASE}/api/datasets`).then((r) => r.json()),
  variables: (id: string) => fetch(`${BASE}/api/datasets/${id}/variables`).then((r) => r.json()),
  colormaps: () => fetch(`${BASE}/api/colormaps`).then((r) => r.json()),
};
```

### `src/hooks/useDatasets.ts`

Wraps the datasets endpoint in TanStack Query. The `staleTime` of 30 seconds means the list is served from cache instantly on repeat visits, with a silent background refresh.

```ts
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/endpoints";
import type { Dataset } from "@/types";

export function useDatasets() {
  return useQuery<Dataset[]>({
    queryKey: ["datasets"],
    queryFn: api.datasets,
    staleTime: 30_000,
    gcTime: 300_000,
  });
}

export function useVariables(datasetId: string | null) {
  return useQuery({
    queryKey: ["variables", datasetId],
    queryFn: () => api.variables(datasetId!),
    enabled: datasetId != null,
    staleTime: 60_000,
  });
}
```

### `src/hooks/useViewport.ts`

Debounces viewport changes so filter queries and prefetch triggers don't fire on every animation frame during a pan.

```ts
import { useState, useCallback, useRef } from "react";
import type { ViewState } from "@deck.gl/core";

export function useViewport(onSettled: (vs: ViewState) => void) {
  const [viewState, setViewState] = useState<ViewState>({
    longitude: 0, latitude: 20, zoom: 2, pitch: 0, bearing: 0,
  });
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const handleViewStateChange = useCallback(({ viewState: vs }: { viewState: ViewState }) => {
    setViewState(vs);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => onSettled(vs), 150);
  }, [onSettled]);

  return { viewState, handleViewStateChange };
}
```

### `src/workers/prefetch.worker.ts`

Runs in a dedicated thread. Receives the current viewport and emits `fetch()` calls for surrounding tiles before the user reaches them. Uses the Cache API to skip tiles the browser already has.

```ts
interface PrefetchMessage {
  z: number;
  centerX: number;
  centerY: number;
  tileUrlTemplate: string;
  radius: number;
}

self.onmessage = async (e: MessageEvent<PrefetchMessage>) => {
  const { z, centerX, centerY, tileUrlTemplate, radius } = e.data;
  const cache = await caches.open("satellite-tiles");

  for (let dx = -radius; dx <= radius; dx++) {
    for (let dy = -radius; dy <= radius; dy++) {
      if (dx === 0 && dy === 0) continue;
      const x = centerX + dx;
      const y = centerY + dy;
      const url = tileUrlTemplate
        .replace("{z}", String(z))
        .replace("{x}", String(x))
        .replace("{y}", String(y));

      const cached = await cache.match(url);
      if (!cached) {
        fetch(url, { priority: "low" }).then((res) => {
          if (res.ok) cache.put(url, res.clone());
        });
      }
    }
  }
};
```

### `src/components/Map/MapView.tsx`

Composes Deck.gl and MapLibre into the main viewer. The satellite layer is passed as a Deck.gl layer; MapLibre renders the base map underneath.

```tsx
import DeckGL from "@deck.gl/react";
import { MapboxMap } from "react-map-gl";
import { useSatelliteLayer } from "./SatelliteLayer";
import { useViewport } from "@/hooks/useViewport";
import { useMapStore } from "@/store/mapStore";
import { TileLoadingBar } from "./TileLoadingBar";

export function MapView() {
  const setViewState = useMapStore((s) => s.setViewState);
  const storedViewState = useMapStore((s) => s.viewState);
  const satelliteLayer = useSatelliteLayer();
  const { viewState, handleViewStateChange } = useViewport(setViewState);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <TileLoadingBar layer={satelliteLayer} />
      <DeckGL
        viewState={viewState}
        onViewStateChange={handleViewStateChange}
        controller
        layers={satelliteLayer ? [satelliteLayer] : []}
      >
        <MapboxMap
          mapStyle="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
        />
      </DeckGL>
    </div>
  );
}
```

---

## State flow

```
User clicks dataset        User scrubs time slider     User pans map
        │                         │                         │
        ▼                         ▼                         ▼
mapStore.setDataset()     mapStore.setTimeIndex()    useViewport debounce (150ms)
        │                         │                         │
        └──────────────┬──────────┘                         │
                       ▼                                     ▼
              SatelliteLayer id changes             Prefetch worker fires
                       │                           (radius 2 surrounding tiles)
                       ▼
              TileLayer discards old tiles
              getTileData() called for new tiles
                       │
                       ├── Browser cache? → render instantly
                       └── No → fetch /api/tiles/...
```

---

## Dependencies (`package.json` key deps)

```json
{
  "dependencies": {
    "@deck.gl/core": "^9",
    "@deck.gl/geo-layers": "^9",
    "@deck.gl/layers": "^9",
    "@deck.gl/react": "^9",
    "@tanstack/react-query": "^5",
    "maplibre-gl": "^4",
    "react": "^18",
    "react-dom": "^18",
    "react-map-gl": "^7",
    "zustand": "^4"
  },
  "devDependencies": {
    "@types/react": "^18",
    "autoprefixer": "^10",
    "postcss": "^8",
    "tailwindcss": "^3",
    "typescript": "^5",
    "vite": "^5",
    "vite-plugin-worker": "^1"
  }
}
```

### `vite.config.ts`

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  worker: {
    format: "es",
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
```

---

## TypeScript types (`src/types/index.ts`)

```ts
export interface Dataset {
  id: string;
  name: string;
  description: string;
  variables: string[];
  time_steps: number;
  bounds: [number, number, number, number]; // west, south, east, north
  created_at: string;
}

export interface VariableMeta {
  name: string;
  long_name: string;
  units: string;
  dtype: string;
  shape: number[];
  p2: number;
  p98: number;
}

export interface ViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}
```
