import type { DatasetMeta, VariableMeta } from "../types";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

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
  return `${API_BASE}/api/tiles/${encodeURIComponent(params.datasetId)}/${encodeURIComponent(params.variable)}/{z}/{x}/{y}?${query.toString()}`;
}

export const api = {
  datasets: async (): Promise<DatasetMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets`);
    return parseJson<DatasetMeta[]>(response);
  },
  dataset: async (datasetId: string): Promise<DatasetMeta> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}`);
    return parseJson<DatasetMeta>(response);
  },
  variables: async (datasetId: string): Promise<VariableMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/variables`);
    return parseJson<VariableMeta[]>(response);
  },
  colormaps: async (): Promise<string[]> => {
    const response = await fetch(`${API_BASE}/api/colormaps`);
    return parseJson<string[]>(response);
  }
};
