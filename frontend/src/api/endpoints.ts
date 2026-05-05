import type { DatasetMeta, DatasetServingProfile, TileJson, VariableMeta } from "../types";

const API_BASE = import.meta.env.VITE_API_URL ?? "";
const WS_BASE = import.meta.env.VITE_WS_URL ?? "";

interface TileUrlParams {
  datasetId: string;
  variable: string;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function buildApiUrl(path: string): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

export function buildTileUrl(params: TileUrlParams): string {
  const query = new URLSearchParams({
    time_index: String(params.timeIndex),
    colormap: params.colormap
  });
  if (params.vmin !== null) {
    query.set("vmin", String(params.vmin));
  }
  if (params.vmax !== null) {
    query.set("vmax", String(params.vmax));
  }
  return `${buildApiUrl("/api/tiles")}/${encodeURIComponent(params.datasetId)}/${encodeURIComponent(params.variable)}/{z}/{x}/{y}?${query.toString()}`;
}

export function buildWebSocketUrl(path: string): string {
  if (/^wss?:\/\//.test(path)) {
    return path;
  }

  if (WS_BASE) {
    return `${WS_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  }

  const apiBase = API_BASE || window.location.origin;
  const resolved = new URL(path.startsWith("/") ? path : `/${path}`, apiBase);
  resolved.protocol = resolved.protocol === "https:" ? "wss:" : "ws:";
  return resolved.toString();
}

interface TileJsonParams extends TileUrlParams {}

export const api = {
  datasets: async (): Promise<DatasetMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets`);
    return parseJson<DatasetMeta[]>(response);
  },
  dataset: async (datasetId: string): Promise<DatasetMeta> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}`);
    return parseJson<DatasetMeta>(response);
  },
  servingProfile: async (datasetId: string): Promise<DatasetServingProfile> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/serving-profile`);
    return parseJson<DatasetServingProfile>(response);
  },
  tilejson: async (params: TileJsonParams): Promise<TileJson> => {
    const query = new URLSearchParams({
      time_index: String(params.timeIndex),
      colormap: params.colormap
    });
    if (params.vmin !== null) {
      query.set("vmin", String(params.vmin));
    }
    if (params.vmax !== null) {
      query.set("vmax", String(params.vmax));
    }
    const response = await fetch(
      `${API_BASE}/api/tilejson/${encodeURIComponent(params.datasetId)}/${encodeURIComponent(params.variable)}?${query.toString()}`
    );
    return parseJson<TileJson>(response);
  },
  variables: async (datasetId: string): Promise<VariableMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/variables`);
    return parseJson<VariableMeta[]>(response);
  },
  colormaps: async (): Promise<string[]> => {
    const response = await fetch(buildApiUrl("/api/colormaps"));
    return parseJson<string[]>(response);
  },
  colormapPalette: async (name: string, samples = 256): Promise<number[][]> => {
    const response = await fetch(buildApiUrl(`/api/colormaps/${encodeURIComponent(name)}/palette?samples=${samples}`));
    return parseJson<number[][]>(response);
  }
};
