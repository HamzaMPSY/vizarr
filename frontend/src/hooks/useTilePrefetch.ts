import { useEffect } from "react";

import { useMapStore, type MapViewState } from "../store/mapStore";

interface UseTilePrefetchOptions {
  tileTemplate: string | null;
  viewState: MapViewState;
  minZoom: number | null;
  maxZoom: number | null;
  enabled: boolean;
  radius?: number;
}

export function useTilePrefetch({
  tileTemplate,
  viewState,
  minZoom,
  maxZoom,
  enabled,
  radius = 2
}: UseTilePrefetchOptions): void {
  const { vmin, vmax, setRangeFromTileHeaders } = useMapStore((state) => ({
    vmin: state.vmin,
    vmax: state.vmax,
    setRangeFromTileHeaders: state.setRangeFromTileHeaders
  }));

  useEffect(() => {
    if (!enabled || !tileTemplate) {
      return;
    }
    const zoom = Math.floor(viewState.zoom);
    if ((minZoom !== null && zoom < minZoom) || (maxZoom !== null && zoom > maxZoom)) {
      return;
    }

    const abortController = new AbortController();
    const { x, y } = lonLatToTile(viewState.longitude, viewState.latitude, zoom);
    const urls = buildPrefetchUrls(tileTemplate, zoom, x, y, radius);

    void prefetchUrls({
      urls,
      signal: abortController.signal,
      shouldSeedRange: vmin === null && vmax === null,
      setRangeFromTileHeaders
    });

    return () => abortController.abort();
  }, [
    enabled,
    maxZoom,
    minZoom,
    radius,
    setRangeFromTileHeaders,
    tileTemplate,
    viewState.latitude,
    viewState.longitude,
    viewState.zoom,
    vmin,
    vmax
  ]);
}

function buildPrefetchUrls(tileTemplate: string, z: number, centerX: number, centerY: number, radius: number): string[] {
  const limit = 2 ** z;
  const urls: string[] = [];
  urls.push(formatTileUrl(tileTemplate, z, centerX, centerY));

  for (let dy = -radius; dy <= radius; dy += 1) {
    for (let dx = -radius; dx <= radius; dx += 1) {
      if (dx === 0 && dy === 0) {
        continue;
      }
      const x = centerX + dx;
      const y = centerY + dy;
      if (x < 0 || x >= limit || y < 0 || y >= limit) {
        continue;
      }
      urls.push(formatTileUrl(tileTemplate, z, x, y));
    }
  }

  return urls;
}

async function prefetchUrls({
  urls,
  signal,
  shouldSeedRange,
  setRangeFromTileHeaders
}: {
  urls: string[];
  signal: AbortSignal;
  shouldSeedRange: boolean;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
}): Promise<void> {
  const cache = "caches" in window ? await window.caches.open("vizarr-prefetch-tiles") : null;

  for (const [index, url] of urls.entries()) {
    if (signal.aborted) {
      return;
    }

    const request = new Request(url, { signal });
    if (cache && (await cache.match(request))) {
      continue;
    }

    try {
      const response = await fetch(request);
      if (!response.ok) {
        continue;
      }
      if (shouldSeedRange && index === 0) {
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

function formatTileUrl(template: string, z: number, x: number, y: number): string {
  return template
    .replace("{z}", String(z))
    .replace("{x}", String(x))
    .replace("{y}", String(y));
}

function lonLatToTile(longitude: number, latitude: number, zoom: number): { x: number; y: number } {
  const n = 2 ** zoom;
  const clampedLatitude = Math.max(-85.05112878, Math.min(85.05112878, latitude));
  const x = Math.floor(((longitude + 180) / 360) * n);
  const latRad = (clampedLatitude * Math.PI) / 180;
  const y = Math.floor(((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n);
  return {
    x: Math.max(0, Math.min(n - 1, x)),
    y: Math.max(0, Math.min(n - 1, y))
  };
}
