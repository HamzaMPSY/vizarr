import type { DatasetMeta, DatasetServingProfile, TileJson, VariableMeta } from "../types";

const API_BASE = import.meta.env.VITE_API_URL ?? "";
const WS_BASE = import.meta.env.VITE_WS_URL ?? "";
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

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

function authHeaders(): HeadersInit | undefined {
  return API_KEY ? { "X-API-Key": API_KEY } : undefined;
}

function withApiKey(url: string): string {
  if (!API_KEY) {
    return url;
  }
  const resolved = new URL(url, window.location.origin);
  if (!resolved.searchParams.has("api_key")) {
    resolved.searchParams.set("api_key", API_KEY);
  }
  if (!/^https?:\/\//.test(url) && url.startsWith("/")) {
    return `${resolved.pathname}${resolved.search}`;
  }
  return resolved.toString();
}

function normalizeApiUrlForBrowser(url: string): string {
  if (!/^https?:\/\//.test(url)) {
    return buildApiUrl(url);
  }

  const parsed = new URL(url);
  if (parsed.pathname.startsWith("/api/")) {
    const templatePath = parsed.pathname.replace(/%7B([xyz])%7D/gi, "{$1}");
    return buildApiUrl(`${templatePath}${parsed.search}`);
  }
  return url;
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
  return withApiKey(
    `${buildApiUrl("/api/tiles")}/${encodeURIComponent(params.datasetId)}/${encodeURIComponent(params.variable)}/{z}/{x}/{y}?${query.toString()}`
  );
}

export function buildWebSocketUrl(path: string): string {
  if (/^wss?:\/\//.test(path)) {
    return withApiKey(path);
  }

  if (WS_BASE) {
    return withApiKey(`${WS_BASE}${path.startsWith("/") ? path : `/${path}`}`);
  }

  const apiBase = API_BASE || window.location.origin;
  const resolved = new URL(path.startsWith("/") ? path : `/${path}`, apiBase);
  resolved.protocol = resolved.protocol === "https:" ? "wss:" : "ws:";
  return withApiKey(resolved.toString());
}

interface TileJsonParams extends TileUrlParams {}

export const api = {
  datasets: async (): Promise<DatasetMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets`, { headers: authHeaders() });
    return parseJson<DatasetMeta[]>(response);
  },
  dataset: async (datasetId: string): Promise<DatasetMeta> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}`, { headers: authHeaders() });
    return parseJson<DatasetMeta>(response);
  },
  servingProfile: async (datasetId: string): Promise<DatasetServingProfile> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/serving-profile`, {
      headers: authHeaders()
    });
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
      `${API_BASE}/api/tilejson/${encodeURIComponent(params.datasetId)}/${encodeURIComponent(params.variable)}?${query.toString()}`,
      { headers: authHeaders() }
    );
    const tilejson = await parseJson<TileJson>(response);
    return {
      ...tilejson,
      tiles: tilejson.tiles.map((tileUrl) => withApiKey(normalizeApiUrlForBrowser(tileUrl)))
    };
  },
  variables: async (datasetId: string): Promise<VariableMeta[]> => {
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/variables`, {
      headers: authHeaders()
    });
    return parseJson<VariableMeta[]>(response);
  },
  colormaps: async (): Promise<string[]> => {
    const response = await fetch(buildApiUrl("/api/colormaps"), { headers: authHeaders() });
    return parseJson<string[]>(response);
  },
  colormapPalette: async (name: string, samples = 256): Promise<number[][]> => {
    const response = await fetch(buildApiUrl(`/api/colormaps/${encodeURIComponent(name)}/palette?samples=${samples}`), {
      headers: authHeaders()
    });
    return parseJson<number[][]>(response);
  }
};
