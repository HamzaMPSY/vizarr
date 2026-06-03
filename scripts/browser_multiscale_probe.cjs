#!/usr/bin/env node

const playwrightModule = process.env.PLAYWRIGHT_MODULE || "playwright";
const { chromium } = require(playwrightModule);

const url = process.argv[2] || process.env.VIZARR_FRONTEND_URL || "http://localhost:5173";
const expectedMode = process.env.VIZARR_EXPECTED_FRONTEND_RENDER_MODE || "";
const scenario = process.env.VIZARR_BROWSER_PROBE_SCENARIO || "";
const interaction = process.env.VIZARR_BROWSER_PROBE_INTERACTION || "";
const shouldToggleCountryBorders = process.env.VIZARR_BROWSER_PROBE_COUNTRY_BORDERS === "true";
const countryBordersUrlPattern = /ne_110m_admin_0_boundary_lines_land\.geojson$/;

(async () => {
  const launchOptions = {
    headless: true,
    args: ["--disable-dev-shm-usage"]
  };
  if (process.env.CHROME_PATH) {
    launchOptions.executablePath = process.env.CHROME_PATH;
  }

  const browser = await chromium.launch(launchOptions);
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  const events = [];
  const timings = {};
  let failedRequestCount = 0;

  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      events.push({ type: "console", level: message.type(), text: message.text() });
    }
  });
  page.on("pageerror", (error) => {
    events.push({ type: "pageerror", text: error.stack || error.message });
  });
  page.on("requestfailed", (request) => {
    failedRequestCount += 1;
    events.push({
      type: "requestfailed",
      url: request.url(),
      failure: request.failure()?.errorText || "unknown"
    });
  });

  if (scenario) {
    await installMockRoutes(page, scenario);
  }
  if (shouldToggleCountryBorders) {
    await page.context().route(countryBordersUrlPattern, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/geo+json",
        headers: {
          "Access-Control-Allow-Origin": "*"
        },
        body: JSON.stringify({
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: { name: "probe-border" },
              geometry: {
                type: "LineString",
                coordinates: [
                  [-1, -1],
                  [1, 1]
                ]
              }
            }
          ]
        })
      })
    );
  }

  const navigationStartedAt = Date.now();
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  timings.domcontentloaded_ms = Date.now() - navigationStartedAt;
  try {
    await page.waitForSelector(".map-shell", { state: "attached", timeout: 30000 });
    timings.map_shell_attached_ms = Date.now() - navigationStartedAt;
  } catch (error) {
    const bodyText = await page.locator("body").innerText().catch(() => "");
    console.error(JSON.stringify({ error: error.message, bodyText: bodyText.slice(0, 1000), events }, null, 2));
    throw error;
  }
  const renderModeObserved = await page.waitForFunction(
    () => {
      const element = document.querySelector(".map-shell");
      const mode = element?.getAttribute("data-render-mode");
      const nativeStatus = element?.getAttribute("data-browser-native-status");
      const gpuStatus = element?.getAttribute("data-browser-gpu-status");
      return mode === "browser-gpu" || mode === "browser-native" || nativeStatus === "fallback" || gpuStatus === "fallback";
    },
    null,
    { timeout: Number(process.env.VIZARR_BROWSER_PROBE_TIMEOUT_MS || "15000") }
  ).then(() => true).catch(() => false);
  if (renderModeObserved) {
    timings.render_mode_observed_ms = Date.now() - navigationStartedAt;
  }
  await page.waitForTimeout(Number(process.env.VIZARR_BROWSER_PROBE_SETTLE_MS || "1000"));
  timings.settled_ms = Date.now() - navigationStartedAt;
  if (shouldToggleCountryBorders) {
    const countryBordersResponse = page.waitForResponse(countryBordersUrlPattern, { timeout: 5000 }).catch(() => null);
    await page.locator("#country-borders").check();
    await page.waitForFunction(
      () => document.querySelector(".map-shell")?.getAttribute("data-country-borders-enabled") === "true",
      null,
      { timeout: 5000 }
    );
    await countryBordersResponse;
    await page.waitForTimeout(500);
  }
  if (interaction === "pan-zoom") {
    await runPanZoomSmoke(page);
  }

  const attributes = await page.locator(".map-shell").evaluate((node) => {
    const element = node;
    return {
      renderMode: element.getAttribute("data-render-mode"),
      browserNativeStatus: element.getAttribute("data-browser-native-status"),
      browserNativeMode: element.getAttribute("data-browser-native-mode"),
      browserNativeReason: element.getAttribute("data-browser-native-reason"),
      browserNativeLevel: element.getAttribute("data-browser-native-level"),
      browserNativePixels: Number(element.getAttribute("data-browser-native-pixels") || "0"),
      browserNativeChunks: Number(element.getAttribute("data-browser-native-chunks") || "0"),
      browserNativeLoadedBytes: Number(element.getAttribute("data-browser-native-loaded-bytes") || "0"),
      browserNativeEstimatedBytes: Number(element.getAttribute("data-browser-native-estimated-bytes") || "0"),
      browserNativeMaxPixels: Number(element.getAttribute("data-browser-native-max-pixels") || "0"),
      browserNativeMaxChunks: Number(element.getAttribute("data-browser-native-max-chunks") || "0"),
      browserNativeMaxBytes: Number(element.getAttribute("data-browser-native-max-bytes") || "0"),
      browserNativeMaxConcurrency: Number(element.getAttribute("data-browser-native-max-concurrency") || "0"),
      browserGpuStatus: element.getAttribute("data-browser-gpu-status"),
      browserGpuReady: element.getAttribute("data-browser-gpu-ready") === "true",
      browserGpuMode: element.getAttribute("data-browser-gpu-mode"),
      browserGpuReason: element.getAttribute("data-browser-gpu-reason"),
      browserGpuLevel: element.getAttribute("data-browser-gpu-level"),
      browserGpuRenderer: element.getAttribute("data-browser-gpu-renderer"),
      browserGpuMaxTextureDimension: Number(element.getAttribute("data-browser-gpu-max-texture-dimension") || "0"),
      browserGpuFailureFallbackThreshold: Number(
        element.getAttribute("data-browser-gpu-failure-fallback-threshold") || "0"
      ),
      browserGpuFailureCount: Number(element.getAttribute("data-browser-gpu-failure-count") || "0"),
      browserGpuLastError: element.getAttribute("data-browser-gpu-last-error"),
      selected: {
        dataset_id: element.getAttribute("data-selected-dataset-id"),
        variable_id: element.getAttribute("data-selected-variable-id"),
        render_kind: element.getAttribute("data-selected-render-kind"),
        composite_style_id: element.getAttribute("data-selected-composite-style-id"),
        tile_variable_id: element.getAttribute("data-selected-tile-variable-id"),
        time_index: Number(element.getAttribute("data-selected-time-index") || "0"),
        zoom: Number(element.getAttribute("data-map-zoom") || "0")
      },
      countryBordersEnabled: element.getAttribute("data-country-borders-enabled") === "true"
    };
  });
  const paintTimings = await page.evaluate(() =>
    performance.getEntriesByType("paint").reduce((acc, entry) => {
      acc[`${entry.name.replaceAll("-", "_")}_ms`] = Math.round(entry.startTime);
      return acc;
    }, {})
  );

  const output = {
    status: response?.status() || null,
    url: page.url(),
    scenario: scenario || null,
    interaction: interaction || null,
    renderMode: attributes.renderMode,
    active_rendering_mode: attributes.renderMode,
    gpu_status: attributes.browserGpuStatus,
    gpu_ready: attributes.browserGpuReady,
    gpu_reason: attributes.browserGpuReason,
    gpu_renderer: attributes.browserGpuRenderer,
    failed_request_count: failedRequestCount,
    selected: attributes.selected,
    timings_ms: {
      ...timings,
      ...paintTimings
    },
    attributes,
    events
  };
  console.log(JSON.stringify(output));
  await browser.close();

  if (expectedMode && attributes.renderMode !== expectedMode) {
    console.error(`expected render mode ${expectedMode}, got ${attributes.renderMode}`);
    process.exit(1);
  }
  if (events.some((event) => event.type === "pageerror")) {
    process.exit(1);
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});

async function installMockRoutes(page, selectedScenario) {
  const unsupportedProfile = selectedScenario === "server-fallback";
  const failedChunk = selectedScenario === "failed-chunk-fallback";
  const oversized = selectedScenario === "oversized-fallback";
  const browserGpu = selectedScenario === "browser-gpu";
  const shape = oversized ? [1, 1, 4096, 4096] : [1, 1, 256, 256];
  const chunks = [1, 1, 256, 256];
  const palette = Array.from({ length: 256 }, (_, index) => [index, index, index, 255]);
  const chunk = new Float32Array(256 * 256);
  chunk.fill(0.5);

  await page.route("**/api/**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const path = requestUrl.pathname;

    if (!path.startsWith("/api/")) {
      return route.continue();
    }

    if (path === "/api/datasets") {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "ds",
            name: "Mock multiscale dataset",
            description: "Mocked browser-native probe dataset",
            variables: [],
            composite_styles: [],
            bounds: { west: -1, south: -1, east: 1, north: 1 },
            zarr_format: 3,
            zarr_consolidated: true,
            zarr_proxy_root: "/api/zarr/ds",
            multiscale_zarr_format: 2,
            multiscale_zarr_consolidated: true,
            multiscale_proxy_root: "/api/zarr/multiscale/ds"
          }
        ])
      });
    }

    if (path === "/api/datasets/ds/variables") {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "NDVI",
            name: "NDVI",
            unit: "1",
            time_steps: 1,
            stats: { min: 0, max: 1, p02: 0, p98: 1 },
            display_vmin: 0,
            display_vmax: 1,
            default_colormap: "viridis"
          }
        ])
      });
    }

    if (path === "/api/datasets/ds/serving-profile") {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          dataset_id: "ds",
          zarr_format: 3,
          zarr_consolidated: true,
          zarr_proxy_root: "/api/zarr/ds",
          multiscale_store_path: "multiscale/ds.zarr",
          multiscale_zarr_format: 2,
          multiscale_zarr_consolidated: true,
          multiscale_proxy_root: "/api/zarr/multiscale/ds",
          data_array_name: "bands",
          variable_ids: ["NDVI"],
          has_multiscale: !unsupportedProfile,
          multiscale_paths: unsupportedProfile ? [] : ["0"],
          browse_overview_zoom_levels: [],
          chunk_layout: {
            sharded: false,
            inner_chunk_shape: [1, 1, 256, 256]
          },
          supported_rendering_modes: unsupportedProfile
            ? ["dynamic_tiles"]
            : browserGpu
              ? ["dynamic_tiles", "multiscale_proxy", "browser_gpu"]
              : ["dynamic_tiles", "multiscale_proxy"],
          browser_multiscale_ready: !unsupportedProfile,
          browser_gpu_ready: browserGpu,
          browser_gpu_reason: browserGpu ? "browser GPU eligible" : "missing browser_gpu rendering mode",
          browser_gpu_gaps: browserGpu ? [] : ["missing_browser_gpu_rendering_mode"],
          seamless_rendering_ready: false,
          seamless_rendering_gaps: unsupportedProfile ? ["missing_multiscale_pyramid"] : []
        })
      });
    }

    if (path === "/api/tilejson/ds/NDVI") {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          tilejson: "3.0.0",
          name: "mock",
          tiles: ["/api/tiles/ds/NDVI/{z}/{x}/{y}?time_index=0&colormap=viridis"],
          bounds: [-1, -1, 1, 1],
          minzoom: 0,
          maxzoom: 8,
          detail_minzoom: 0,
          has_coarse_fallback: true,
          coarse_representation: "browse"
        })
      });
    }

    if (path === "/api/colormaps") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(["viridis"]) });
    }

    if (path === "/api/colormaps/viridis/palette") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(palette) });
    }

    if (path === "/api/zarr/multiscale/ds/.zmetadata") {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          metadata: {
            ".zattrs": {
              multiscales: [{ datasets: [{ path: "0" }] }],
              browse_zoom_levels: [1]
            },
            "0/.zattrs": {
              bbox_wgs84: [-1, -1, 1, 1]
            },
            "0/bands/.zarray": {
              shape,
              chunks,
              dtype: "<f4",
              compressor: null,
              filters: null,
              order: "C",
              dimension_separator: "."
            }
          }
        })
      });
    }

    if (path.startsWith("/api/zarr/multiscale/ds/0/bands/")) {
      if (failedChunk) {
        return route.fulfill({ status: 503, contentType: "text/plain", body: "chunk failed" });
      }
      return route.fulfill({
        contentType: "application/octet-stream",
        body: Buffer.from(chunk.buffer)
      });
    }

    if (path.startsWith("/api/tiles/")) {
      return route.fulfill({ status: 204, body: "" });
    }

    return route.fulfill({ status: 404, contentType: "text/plain", body: `unmocked ${path}` });
  });
}

async function runPanZoomSmoke(page) {
  const box = await page.locator(".map-shell").boundingBox();
  if (!box) {
    throw new Error("map shell bounding box is unavailable");
  }
  const startX = box.x + box.width * 0.55;
  const startY = box.y + box.height * 0.55;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX - 220, startY - 70, { steps: 8 });
  await page.mouse.up();
  await page.mouse.wheel(0, -450);
  await page.waitForTimeout(750);
}
