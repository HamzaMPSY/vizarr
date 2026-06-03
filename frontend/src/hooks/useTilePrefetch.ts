import { useEffect, useRef } from "react";

import { buildPrefetchPlan } from "../lib/tilePrefetchPlanner";
import type { TilePrefetchBudget, TilePrefetchPlan, TilePrefetchPlanInput } from "../lib/tilePrefetchPlanner";
import type { MapViewState } from "../store/mapStore";

interface UseTilePrefetchOptions {
  tileTemplate: string | null;
  viewState: MapViewState;
  minZoom: number | null;
  maxZoom: number | null;
  enabled: boolean;
  vmin: number | null;
  vmax: number | null;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
  radius?: number;
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
  viewState,
  minZoom,
  maxZoom,
  enabled,
  vmin,
  vmax,
  setRangeFromTileHeaders,
  radius = 2
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
    tileTemplate,
    viewState.latitude,
    viewState.longitude,
    viewState.pitch,
    viewState.bearing,
    viewState.zoom,
    vmin,
    vmax
  ]);
}

async function prefetchUrls({
  urls,
  signal,
  shouldSeedRange,
  setRangeFromTileHeaders,
  maxInflightRequests
}: {
  urls: string[];
  signal: AbortSignal;
  shouldSeedRange: boolean;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
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
  setRangeFromTileHeaders
}: {
  url: string;
  signal: AbortSignal;
  cache: Cache | null;
  shouldSeedRange: boolean;
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
      console.debug("Tile prefetch failed", error);
    }
  }
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
