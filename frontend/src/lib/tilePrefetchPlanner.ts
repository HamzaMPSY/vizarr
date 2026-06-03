import type { MapViewState } from "../store/mapStore";

export interface TilePrefetchBudget {
  maxInflightRequests: number;
  maxQueuedTiles: number;
}

export interface TilePrefetchPlanInput {
  tileTemplate: string;
  viewState: MapViewState;
  previousViewState: MapViewState | null;
  minZoom: number | null;
  maxZoom: number | null;
  radius: number;
  budget: TilePrefetchBudget;
}

export interface PlannedTile {
  url: string;
  z: number;
  x: number;
  y: number;
  priority: number;
  reason: "center" | "pan-direction" | "ring" | "zoom-in" | "zoom-out";
}

export interface TilePrefetchPlan {
  tiles: PlannedTile[];
  urls: string[];
  skippedReason: string | null;
}

interface TileCoordinate {
  z: number;
  x: number;
  y: number;
}

interface FloatTileCoordinate {
  x: number;
  y: number;
}

export function buildPrefetchPlan(input: TilePrefetchPlanInput): TilePrefetchPlan {
  const zoom = Math.floor(input.viewState.zoom);
  if ((input.minZoom !== null && zoom < input.minZoom) || (input.maxZoom !== null && zoom > input.maxZoom)) {
    return emptyPlan("zoom_outside_tilejson_range");
  }

  const center = lonLatToTile(input.viewState.longitude, input.viewState.latitude, zoom);
  const currentFloat = lonLatToFloatTile(input.viewState.longitude, input.viewState.latitude, zoom);
  const previousFloat = input.previousViewState
    ? lonLatToFloatTile(input.previousViewState.longitude, input.previousViewState.latitude, zoom)
    : null;
  const direction = previousFloat
    ? {
        x: currentFloat.x - previousFloat.x,
        y: currentFloat.y - previousFloat.y
      }
    : { x: 0, y: 0 };
  const zoomDelta = input.previousViewState ? input.viewState.zoom - input.previousViewState.zoom : 0;
  const candidates = new Map<string, PlannedTile>();

  addCandidate(candidates, input.tileTemplate, center, 0, "center");
  addDirectionalRing(candidates, input.tileTemplate, center, input.radius, direction);
  addZoomIntentCandidates(candidates, input, center, zoomDelta);

  const tiles = [...candidates.values()]
    .sort((left, right) => left.priority - right.priority || left.z - right.z || left.y - right.y || left.x - right.x)
    .slice(0, Math.max(0, input.budget.maxQueuedTiles));

  return {
    tiles,
    urls: tiles.map((tile) => tile.url),
    skippedReason: null
  };
}

export function formatTileUrl(template: string, z: number, x: number, y: number): string {
  return template
    .replace("{z}", String(z))
    .replace("{x}", String(x))
    .replace("{y}", String(y));
}

export function lonLatToTile(longitude: number, latitude: number, zoom: number): TileCoordinate {
  const float = lonLatToFloatTile(longitude, latitude, zoom);
  const limit = 2 ** zoom;
  return {
    z: zoom,
    x: Math.max(0, Math.min(limit - 1, Math.floor(float.x))),
    y: Math.max(0, Math.min(limit - 1, Math.floor(float.y)))
  };
}

function addDirectionalRing(
  candidates: Map<string, PlannedTile>,
  tileTemplate: string,
  center: TileCoordinate,
  radius: number,
  direction: { x: number; y: number }
): void {
  const limit = 2 ** center.z;
  const directionMagnitude = Math.hypot(direction.x, direction.y);
  const directionX = directionMagnitude > 0.001 ? direction.x / directionMagnitude : 0;
  const directionY = directionMagnitude > 0.001 ? direction.y / directionMagnitude : 0;

  for (let dy = -radius; dy <= radius; dy += 1) {
    for (let dx = -radius; dx <= radius; dx += 1) {
      if (dx === 0 && dy === 0) {
        continue;
      }
      const x = center.x + dx;
      const y = center.y + dy;
      if (x < 0 || x >= limit || y < 0 || y >= limit) {
        continue;
      }

      const distance = Math.hypot(dx, dy);
      const aheadScore = dx * directionX + dy * directionY;
      const reason = aheadScore > 0.2 ? "pan-direction" : "ring";
      const priority = 10 + distance - aheadScore * 4;
      addCandidate(candidates, tileTemplate, { z: center.z, x, y }, priority, reason);
    }
  }
}

function addZoomIntentCandidates(
  candidates: Map<string, PlannedTile>,
  input: TilePrefetchPlanInput,
  center: TileCoordinate,
  zoomDelta: number
): void {
  if (zoomDelta > 0.05 && (input.maxZoom === null || center.z + 1 <= input.maxZoom)) {
    const childZ = center.z + 1;
    const childX = center.x * 2;
    const childY = center.y * 2;
    for (let dy = 0; dy <= 1; dy += 1) {
      for (let dx = 0; dx <= 1; dx += 1) {
        addCandidate(
          candidates,
          input.tileTemplate,
          { z: childZ, x: childX + dx, y: childY + dy },
          3 + Math.hypot(dx, dy),
          "zoom-in"
        );
      }
    }
  }

  if (zoomDelta < -0.05 && center.z > 0 && (input.minZoom === null || center.z - 1 >= input.minZoom)) {
    addCandidate(
      candidates,
      input.tileTemplate,
      { z: center.z - 1, x: Math.floor(center.x / 2), y: Math.floor(center.y / 2) },
      2,
      "zoom-out"
    );
  }
}

function addCandidate(
  candidates: Map<string, PlannedTile>,
  tileTemplate: string,
  tile: TileCoordinate,
  priority: number,
  reason: PlannedTile["reason"]
): void {
  const key = `${tile.z}/${tile.x}/${tile.y}`;
  const existing = candidates.get(key);
  if (existing && existing.priority <= priority) {
    return;
  }
  candidates.set(key, {
    ...tile,
    url: formatTileUrl(tileTemplate, tile.z, tile.x, tile.y),
    priority,
    reason
  });
}

function lonLatToFloatTile(longitude: number, latitude: number, zoom: number): FloatTileCoordinate {
  const n = 2 ** zoom;
  const clampedLatitude = Math.max(-85.05112878, Math.min(85.05112878, latitude));
  const normalizedLongitude = Math.max(-180, Math.min(180, longitude));
  const x = ((normalizedLongitude + 180) / 360) * n;
  const latRad = (clampedLatitude * Math.PI) / 180;
  const y = ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n;
  return { x, y };
}

function emptyPlan(skippedReason: string): TilePrefetchPlan {
  return {
    tiles: [],
    urls: [],
    skippedReason
  };
}
