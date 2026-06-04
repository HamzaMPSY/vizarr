import type {
  BrowseGenerationAcceptedResponse,
  BBox,
  DatasetMeta,
  DatasetServingProfile,
  RangeStatsResponse,
  TileJson,
  VariableMeta
} from "../types";

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

interface DatasetListParams {
  bbox?: BBox | null;
}

interface RangeStatsParams {
  datasetId: string;
  variable: string;
  timeIndex: number;
  bbox?: BBox | null;
  bins?: number;
  maxWidth?: number;
  maxHeight?: number;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new ApiError(errorDetailToMessage(detail, response.status), response.status, detail);
  }
  return response.json() as Promise<T>;
}

export function isOciAuthApiError(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 503) {
    return false;
  }
  const message = error.message.toLowerCase();
  return message.includes("oci") || message.includes("auth") || message.includes("session") || message.includes("token");
}

async function readErrorDetail(response: Response): Promise<unknown> {
  try {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const payload = await response.clone().json() as unknown;
      if (payload && typeof payload === "object" && "detail" in payload) {
        return (payload as { detail?: unknown }).detail;
      }
      return payload;
    }
    const text = await response.clone().text();
    return text.trim() || null;
  } catch {
    return null;
  }
}

function errorDetailToMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (detail && typeof detail === "object") {
    const fields = detail as Record<string, unknown>;
    return [fields.error, fields.reason, fields.message]
      .filter((item): item is string | number => typeof item === "string" || typeof item === "number")
      .join(" ")
      .trim() || `Request failed with ${status}`;
  }
  return `Request failed with ${status}`;
}

function authHeaders(): Record<string, string> | undefined {
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
  datasets: async (params: DatasetListParams = {}): Promise<DatasetMeta[]> => {
    const query = new URLSearchParams();
    if (params.bbox) {
      query.set("bbox", params.bbox.map((value) => String(value)).join(","));
    }
    const suffix = query.toString();
    const response = await fetch(`${API_BASE}/api/datasets${suffix ? `?${suffix}` : ""}`, { headers: authHeaders() });
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
  createBrowseGeneration: async (datasetId: string): Promise<BrowseGenerationAcceptedResponse> => {
    const headers = authHeaders();
    const response = await fetch(`${API_BASE}/api/datasets/${encodeURIComponent(datasetId)}/browse-generation`, {
      method: "POST",
      headers: headers ? { "Content-Type": "application/json", ...headers } : { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    return parseJson<BrowseGenerationAcceptedResponse>(response);
  },
  rangeStats: async (params: RangeStatsParams): Promise<RangeStatsResponse> => {
    const query = new URLSearchParams({
      dataset_id: params.datasetId,
      variable: params.variable,
      time_index: String(params.timeIndex),
      bins: String(params.bins ?? 32),
      max_width: String(params.maxWidth ?? 128),
      max_height: String(params.maxHeight ?? 128)
    });
    if (params.bbox) {
      query.set("bbox", params.bbox.map((value) => String(value)).join(","));
    }
    const response = await fetch(`${API_BASE}/api/query/range?${query.toString()}`, { headers: authHeaders() });
    return parseJson<RangeStatsResponse>(response);
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
