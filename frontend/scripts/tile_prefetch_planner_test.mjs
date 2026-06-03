import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import ts from "typescript";

const source = await readFile(new URL("../src/lib/tilePrefetchPlanner.ts", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source.replace(/^import type .+;\n/gm, ""), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.ES2020,
    strict: true
  }
});
const modulePath = join(tmpdir(), `vizarr-tile-prefetch-planner-${Date.now()}.mjs`);
await writeFile(modulePath, transpiled.outputText, "utf8");
const { buildPrefetchPlan, formatTileUrl, lonLatToTile } = await import(`file://${modulePath}`);

const tileTemplate = "/api/tiles/ds/NDVI/{z}/{x}/{y}";
const baseViewState = {
  longitude: 0,
  latitude: 0,
  zoom: 4.2,
  pitch: 0,
  bearing: 0
};
const budget = {
  maxInflightRequests: 3,
  maxQueuedTiles: 32
};

{
  const current = { ...baseViewState, longitude: 10 };
  const previous = { ...baseViewState, longitude: 0 };
  const center = lonLatToTile(current.longitude, current.latitude, Math.floor(current.zoom));
  const aheadUrl = formatTileUrl(tileTemplate, center.z, center.x + 1, center.y);
  const behindUrl = formatTileUrl(tileTemplate, center.z, center.x - 1, center.y);
  const plan = buildPrefetchPlan({
    tileTemplate,
    viewState: current,
    previousViewState: previous,
    minZoom: 0,
    maxZoom: 8,
    radius: 2,
    budget
  });

  assert.equal(plan.urls[0], formatTileUrl(tileTemplate, center.z, center.x, center.y));
  assert.ok(plan.urls.indexOf(aheadUrl) > -1, "plan includes the tile ahead of eastward pan");
  assert.ok(plan.urls.indexOf(behindUrl) > -1, "plan includes the tile behind the eastward pan");
  assert.ok(
    plan.urls.indexOf(aheadUrl) < plan.urls.indexOf(behindUrl),
    "directional tiles are prioritized before tiles behind the pan"
  );
}

{
  const plan = buildPrefetchPlan({
    tileTemplate,
    viewState: baseViewState,
    previousViewState: null,
    minZoom: 0,
    maxZoom: 8,
    radius: 4,
    budget: { ...budget, maxQueuedTiles: 5 }
  });

  assert.equal(plan.urls.length, 5, "queued tile budget is enforced");
}

{
  const current = { ...baseViewState, zoom: 4.8 };
  const previous = { ...baseViewState, zoom: 4.1 };
  const plan = buildPrefetchPlan({
    tileTemplate,
    viewState: current,
    previousViewState: previous,
    minZoom: 0,
    maxZoom: 8,
    radius: 1,
    budget
  });

  assert.ok(plan.tiles.some((tile) => tile.reason === "zoom-in"), "zoom-in intent adds child tiles");
}

{
  const plan = buildPrefetchPlan({
    tileTemplate,
    viewState: { ...baseViewState, zoom: 12 },
    previousViewState: null,
    minZoom: 0,
    maxZoom: 8,
    radius: 2,
    budget
  });

  assert.deepEqual(plan.urls, []);
  assert.equal(plan.skippedReason, "zoom_outside_tilejson_range");
}

console.log("tile prefetch planner tests passed");
