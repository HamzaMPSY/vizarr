import { useEffect, useRef } from "react";

import { buildPrefetchPlan } from "../lib/tilePrefetchPlanner";
import type { TilePrefetchBudget, TilePrefetchPlan, TilePrefetchPlanInput } from "../lib/tilePrefetchPlanner";
import type { MapViewState } from "../store/mapStore";

export type TilePrefetchDiagnosticSource =
  | "viewport-prefetch"
  | "adjacent-time-prefetch"
  | "maplibre"
  | "browser-gpu";

export interface TilePrefetchDiagnostic {
  source: TilePrefetchDiagnosticSource;
  path: string;
  z: string | null;
  x: string | null;
  y: string | null;
  status: number | null;
  ok: boolean;
  errorMessage: string | null;
  headers: Record<string, string>;
  recordedAt: number;
}

interface UseTilePrefetchOptions {
  tileTemplate: string | null;
  adjacentTimeTileTemplate?: string | null;
  adjacentTimePrefetchEnabled?: boolean;
  viewState: MapViewState;
  minZoom: number | null;
  maxZoom: number | null;
  enabled: boolean;
  vmin: number | null;
  vmax: number | null;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
  onTileDiagnostic?: (diagnostic: TilePrefetchDiagnostic) => void;
  radius?: number;
  adjacentTimeRadius?: number;
}

interface NavigatorWithPrefetchSignals extends Navigator {
  connection?: {
    effectiveType?: string;
    saveData?: boolean;
  };
  deviceMemory?: number;
}

interface IdleTask {
  cancel: () => void;
}

const DEFAULT_PREFETCH_BUDGET: TilePrefetchBudget = {
  maxInflightRequests: 3,
  maxQueuedTiles: 32
};

const REDUCED_PREFETCH_BUDGET: TilePrefetchBudget = {
  maxInflightRequests: 1,
  maxQueuedTiles: 8
};

export function useTilePrefetch({
  tileTemplate,
  adjacentTimeTileTemplate = null,
  adjacentTimePrefetchEnabled = false,
  viewState,
  minZoom,
  maxZoom,
  enabled,
  vmin,
  vmax,
  setRangeFromTileHeaders,
  onTileDiagnostic,
  radius = 2,
  adjacentTimeRadius = 1
}: UseTilePrefetchOptions): void {
  const previousViewStateRef = useRef<MapViewState | null>(null);

  useEffect(() => {
    if (!enabled || !tileTemplate) {
      previousViewStateRef.current = viewState;
      return;
    }

    const abortController = new AbortController();
    const budget = getPrefetchBudget();
    const input: TilePrefetchPlanInput = {
      tileTemplate,
      viewState,
      previousViewState: previousViewStateRef.current,
      minZoom,
      maxZoom,
      radius,
      budget
    };
    previousViewStateRef.current = viewState;

    let worker: Worker | null = null;
    let completed = false;
    const idleTask = scheduleIdleTask(() => {
      if (abortController.signal.aborted) {
        return;
      }

      const runPlan = (plan: TilePrefetchPlan) => {
        if (abortController.signal.aborted || completed || plan.urls.length === 0) {
          return;
        }
        completed = true;
        void prefetchUrls({
          urls: plan.urls,
          signal: abortController.signal,
          shouldSeedRange: vmin === null && vmax === null,
          setRangeFromTileHeaders,
          source: "viewport-prefetch",
          onTileDiagnostic,
          maxInflightRequests: budget.maxInflightRequests
        }).catch((error) => {
          if (!abortController.signal.aborted) {
            console.debug("Tile prefetch queue failed", error);
          }
        });
      };

      try {
        worker = new Worker(new URL("../workers/tilePrefetchPlanner.worker.ts", import.meta.url), { type: "module" });
        worker.onmessage = (event: MessageEvent<TilePrefetchPlan>) => {
          worker?.terminate();
          worker = null;
          runPlan(event.data);
        };
        worker.onerror = () => {
          worker?.terminate();
          worker = null;
          runPlan(buildPrefetchPlan(input));
        };
        worker.postMessage({ type: "plan", input });
      } catch {
        runPlan(buildPrefetchPlan(input));
      }
    });

    return () => {
      abortController.abort();
      idleTask.cancel();
      worker?.terminate();
    };
  }, [
    enabled,
    maxZoom,
    minZoom,
    radius,
    setRangeFromTileHeaders,
    onTileDiagnostic,
    tileTemplate,
    viewState.latitude,
    viewState.longitude,
    viewState.pitch,
    viewState.bearing,
    viewState.zoom,
    vmin,
    vmax
  ]);

  useEffect(() => {
    if (!enabled || !adjacentTimePrefetchEnabled || !adjacentTimeTileTemplate) {
      return;
    }

    const abortController = new AbortController();
    const baseBudget = getPrefetchBudget();
    const budget: TilePrefetchBudget = {
      maxInflightRequests: 1,
      maxQueuedTiles: Math.min(baseBudget.maxQueuedTiles, 8)
    };
    const input: TilePrefetchPlanInput = {
      tileTemplate: adjacentTimeTileTemplate,
      viewState,
      previousViewState: null,
      minZoom,
      maxZoom,
      radius: adjacentTimeRadius,
      budget
    };

    const idleTask = scheduleIdleTask(() => {
      if (abortController.signal.aborted) {
        return;
      }
      const plan = buildPrefetchPlan(input);
      if (plan.urls.length === 0) {
        return;
      }
      void prefetchUrls({
        urls: plan.urls,
        signal: abortController.signal,
        shouldSeedRange: false,
        setRangeFromTileHeaders,
        source: "adjacent-time-prefetch",
        onTileDiagnostic,
        maxInflightRequests: budget.maxInflightRequests
      }).catch((error) => {
        if (!abortController.signal.aborted) {
          console.debug("Adjacent time prefetch failed", error);
        }
      });
    });

    return () => {
      abortController.abort();
      idleTask.cancel();
    };
  }, [
    adjacentTimePrefetchEnabled,
    adjacentTimeRadius,
    adjacentTimeTileTemplate,
    enabled,
    maxZoom,
    minZoom,
    onTileDiagnostic,
    setRangeFromTileHeaders,
    viewState.latitude,
    viewState.longitude,
    viewState.pitch,
    viewState.bearing,
    viewState.zoom
  ]);
}

async function prefetchUrls({
  urls,
  signal,
  shouldSeedRange,
  setRangeFromTileHeaders,
  source,
  onTileDiagnostic,
  maxInflightRequests
}: {
  urls: string[];
  signal: AbortSignal;
  shouldSeedRange: boolean;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
  source: TilePrefetchDiagnosticSource;
  onTileDiagnostic?: (diagnostic: TilePrefetchDiagnostic) => void;
  maxInflightRequests: number;
}): Promise<void> {
  const cache = "caches" in window ? await window.caches.open("vizarr-prefetch-tiles") : null;
  let nextIndex = 0;

  const workers = Array.from({ length: Math.max(1, Math.min(maxInflightRequests, urls.length)) }, async () => {
    while (!signal.aborted && nextIndex < urls.length) {
      const index = nextIndex;
      nextIndex += 1;
      const url = urls[index];
      if (!url) {
        continue;
      }
      await prefetchUrl({
        url,
        signal,
        cache,
        shouldSeedRange: shouldSeedRange && index === 0,
        source,
        onTileDiagnostic,
        setRangeFromTileHeaders
      });
    }
  });

  await Promise.all(workers);
}

async function prefetchUrl({
  url,
  signal,
  cache,
  shouldSeedRange,
  source,
  onTileDiagnostic,
  setRangeFromTileHeaders
}: {
  url: string;
  signal: AbortSignal;
  cache: Cache | null;
  shouldSeedRange: boolean;
  source: TilePrefetchDiagnosticSource;
  onTileDiagnostic?: (diagnostic: TilePrefetchDiagnostic) => void;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
}): Promise<void> {
  if (signal.aborted) {
    return;
  }

  const request = new Request(url, { signal });
  if (cache && (await cache.match(request))) {
    return;
  }

  try {
    const response = await fetch(request);
    onTileDiagnostic?.(await buildTileDiagnostic(url, response, source));
    if (!response.ok) {
      return;
    }
    if (shouldSeedRange) {
      seedRangeFromHeaders(response, setRangeFromTileHeaders);
    }
    if (cache) {
      await cache.put(request, response.clone());
    }
  } catch (error) {
    if (!signal.aborted) {
      onTileDiagnostic?.(buildNetworkTileDiagnostic(url, error, source));
      console.debug("Tile prefetch failed", error);
    }
  }
}

async function buildTileDiagnostic(
  url: string,
  response: Response,
  source: TilePrefetchDiagnosticSource
): Promise<TilePrefetchDiagnostic> {
  const parsed = parseTileUrl(url);
  return {
    source,
    ...parsed,
    status: response.status,
    ok: response.ok,
    errorMessage: response.ok ? null : await readErrorMessage(response),
    headers: pickDiagnosticHeaders(response.headers),
    recordedAt: Date.now()
  };
}

function buildNetworkTileDiagnostic(
  url: string,
  error: unknown,
  source: TilePrefetchDiagnosticSource
): TilePrefetchDiagnostic {
  const parsed = parseTileUrl(url);
  return {
    source,
    ...parsed,
    status: null,
    ok: false,
    errorMessage: error instanceof Error ? error.message : "Tile request failed",
    headers: {},
    recordedAt: Date.now()
  };
}

function parseTileUrl(url: string): Pick<TilePrefetchDiagnostic, "path" | "z" | "x" | "y"> {
  const parsed = new URL(url, window.location.origin);
  const parts = parsed.pathname.split("/").filter(Boolean);
  const y = parts.length >= 1 ? parts[parts.length - 1] : null;
  const x = parts.length >= 2 ? parts[parts.length - 2] : null;
  const z = parts.length >= 3 ? parts[parts.length - 3] : null;
  return {
    path: parsed.pathname,
    z,
    x,
    y
  };
}

function pickDiagnosticHeaders(headers: Headers): Record<string, string> {
  const names = [
    "X-Cache-Status",
    "X-Request-Class",
    "X-Execution-Path",
    "X-Representation",
    "X-Planned-Representation",
    "X-Request-Coalescing",
    "X-Tile-Empty",
    "X-Tile-Time-Ms",
    "X-Tile-Planner-Ms",
    "X-Tile-Cache-Lookup-Ms",
    "X-Tile-Coalescing-Ms",
    "X-Tile-Catalog-Ms",
    "X-Tile-Render-Ms",
    "X-Tile-Encode-Ms",
    "X-Object-Get-Count",
    "X-Object-Byte-Range-Get-Count",
    "X-Object-Bytes-Read",
    "X-Zarr-Shard-Index-Reads",
    "X-Zarr-Chunk-Count",
    "X-Tile-Budget-Status",
    "X-Tile-Budget-Reason",
    "X-Tile-Budget-Metric",
    "X-Tile-Budget-Limit",
    "X-Tile-Budget-Actual",
    "X-Data-Vmin",
    "X-Data-Vmax"
  ];
  const picked: Record<string, string> = {};
  for (const name of names) {
    const value = headers.get(name);
    if (value !== null && value !== "") {
      picked[name] = value;
    }
  }
  return picked;
}

async function readErrorMessage(response: Response): Promise<string | null> {
  const contentType = response.headers.get("content-type") ?? "";
  try {
    if (contentType.includes("application/json")) {
      const payload = await response.clone().json() as unknown;
      return errorPayloadToMessage(payload);
    }
    const text = await response.clone().text();
    return text.trim().slice(0, 240) || null;
  } catch {
    return null;
  }
}

function errorPayloadToMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (detail && typeof detail === "object") {
    const fields = detail as Record<string, unknown>;
    return [fields.error, fields.reason, fields.metric, fields.actual, fields.limit]
      .filter((item) => item !== undefined && item !== null && item !== "")
      .join(" ")
      .slice(0, 240);
  }
  return JSON.stringify(payload).slice(0, 240);
}

function seedRangeFromHeaders(response: Response, setRangeFromTileHeaders: (vmin: number, vmax: number) => void): void {
  const rawVmin = response.headers.get("X-Data-Vmin");
  const rawVmax = response.headers.get("X-Data-Vmax");
  if (rawVmin === null || rawVmax === null) {
    return;
  }
  const parsedVmin = Number(rawVmin);
  const parsedVmax = Number(rawVmax);
  if (Number.isFinite(parsedVmin) && Number.isFinite(parsedVmax) && parsedVmax > parsedVmin) {
    setRangeFromTileHeaders(parsedVmin, parsedVmax);
  }
}

function getPrefetchBudget(): TilePrefetchBudget {
  const navigatorSignals = navigator as NavigatorWithPrefetchSignals;
  const connection = navigatorSignals.connection;
  const slowConnection =
    connection?.saveData === true ||
    connection?.effectiveType === "slow-2g" ||
    connection?.effectiveType === "2g";
  const lowMemory = typeof navigatorSignals.deviceMemory === "number" && navigatorSignals.deviceMemory <= 4;
  return slowConnection || lowMemory ? REDUCED_PREFETCH_BUDGET : DEFAULT_PREFETCH_BUDGET;
}

function scheduleIdleTask(callback: () => void): IdleTask {
  if ("requestIdleCallback" in window && "cancelIdleCallback" in window) {
    const idleId = window.requestIdleCallback(callback, { timeout: 750 });
    return {
      cancel: () => window.cancelIdleCallback(idleId)
    };
  }

  const timeoutId = globalThis.setTimeout(callback, 120);
  return {
    cancel: () => globalThis.clearTimeout(timeoutId)
  };
}
