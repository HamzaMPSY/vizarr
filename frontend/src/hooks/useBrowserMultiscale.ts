import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { useColormapPalette } from "./useDatasets";
import {
  chooseReadWindow,
  explainUnsupportedLevel,
  estimateReadWindow,
  loadLevelPlaneWindow,
  loadMultiscaleMetadata,
  renderCompositeMultiscaleRaster,
  renderMultiscaleRaster,
  selectMultiscaleLevel
} from "../lib/multiscale";
import type { MultiscaleLevelDescriptor, MultiscaleReadBudget, MultiscaleReadWindow } from "../lib/multiscale";
import type { DatasetServingProfile } from "../types";

const MAX_BROWSER_NATIVE_PIXELS = 1024 * 1024;
const MAX_BROWSER_NATIVE_CHUNKS = 64;
const MAX_BROWSER_NATIVE_CHUNK_BYTES = 16 * 1024 * 1024;
const MAX_BROWSER_NATIVE_CONCURRENT_CHUNKS = 4;
const BROWSER_NATIVE_BUDGET: MultiscaleReadBudget = {
  maxPixels: MAX_BROWSER_NATIVE_PIXELS,
  maxChunks: MAX_BROWSER_NATIVE_CHUNKS,
  maxChunkBytes: MAX_BROWSER_NATIVE_CHUNK_BYTES,
  maxConcurrentChunkLoads: MAX_BROWSER_NATIVE_CONCURRENT_CHUNKS
};

interface UseBrowserMultiscaleOptions {
  profile: DatasetServingProfile | undefined;
  variable: string | null;
  compositeBands?: BrowserMultiscaleBandInput[] | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  zoom: number;
  viewportBounds: [number, number, number, number] | null;
}

export interface BrowserMultiscaleBandInput {
  variable: string;
  vmin: number;
  vmax: number;
}

export interface BrowserMultiscaleBandPlane {
  variable: string;
  rawValues: Float32Array;
  width: number;
  height: number;
  vmin: number;
  vmax: number;
}

interface BrowserMultiscaleImageBase {
  dataUrl: string;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  levelPath: string;
  browseZoom: number | null;
  mode: "full-level" | "viewport-window";
  pixelCount: number;
  chunkCount: number;
  loadedBytes: number;
  estimatedChunkBytes: number;
}

export interface BrowserSingleBandMultiscaleImage extends BrowserMultiscaleImageBase {
  renderKind: "single-band";
  rawValues: Float32Array;
  width: number;
  height: number;
  vmin: number;
  vmax: number;
  paletteImageData: ImageData;
}

export interface BrowserCompositeMultiscaleImage extends BrowserMultiscaleImageBase {
  renderKind: "composite";
  bands: [BrowserMultiscaleBandPlane, BrowserMultiscaleBandPlane, BrowserMultiscaleBandPlane];
}

export type BrowserMultiscaleImage = BrowserSingleBandMultiscaleImage | BrowserCompositeMultiscaleImage;

export interface BrowserMultiscaleResult {
  image: BrowserMultiscaleImage | null;
  status: "native" | "native-loading" | "fallback";
  reason: string;
  debug: BrowserMultiscaleDebug;
}

interface BrowserMultiscaleDebug {
  mode: "none" | "full-level" | "viewport-window";
  status: "native" | "native-loading" | "fallback";
  reason: string;
  levelPath: string | null;
  browseZoom: number | null;
  pixelCount: number;
  chunkCount: number;
  loadedBytes: number;
  estimatedChunkBytes: number;
  maxPixels: number;
  maxChunks: number;
  maxChunkBytes: number;
  maxConcurrentChunkLoads: number;
}

export function useBrowserMultiscale({
  profile,
  variable,
  compositeBands = null,
  timeIndex,
  colormap,
  vmin,
  vmax,
  zoom,
  viewportBounds
}: UseBrowserMultiscaleOptions): BrowserMultiscaleResult {
  const requestedVariables = useMemo(
    () => compositeBands?.map((band) => band.variable) ?? (variable ? [variable] : []),
    [compositeBands, variable]
  );
  const renderKind: BrowserMultiscaleImage["renderKind"] =
    compositeBands && compositeBands.length > 0 ? "composite" : "single-band";
  const gate = useMemo(() => getBrowserNativeGate(profile, requestedVariables), [profile, requestedVariables]);
  const needsPalette = renderKind === "single-band";
  const { data: palette } = useColormapPalette(gate.enabled && needsPalette ? colormap : null);

  const metadataQuery = useQuery({
    queryKey: [
      "browser-multiscale-metadata",
      profile?.dataset_id,
      profile?.multiscale_proxy_root,
      profile?.data_array_name
    ],
    queryFn: ({ signal }) =>
      loadMultiscaleMetadata(profile?.multiscale_proxy_root ?? "", profile?.data_array_name ?? "", { signal }),
    enabled: gate.enabled,
    staleTime: 300_000
  });

  const imageQuery = useQuery({
    queryKey: [
      "browser-multiscale-image",
      profile?.dataset_id,
      renderKind,
      requestedVariables.join(","),
      timeIndex,
      colormap,
      vmin,
      vmax,
      compositeBands?.map((band) => `${band.variable}:${band.vmin}:${band.vmax}`).join(",") ?? "no-composite",
      Math.floor(zoom),
      viewportBounds?.map((value) => value.toFixed(5)).join(",") ?? "no-viewport",
      metadataQuery.data?.levels.map((level) => `${level.path}:${level.browseZoom}`).join(",")
    ],
    queryFn: async ({ signal }): Promise<BrowserMultiscaleImage> => {
      if (!metadataQuery.data || !profile) {
        throw new Error("Browser-native rendering inputs are incomplete");
      }
      const selectedLevel = selectMultiscaleLevel(metadataQuery.data, zoom);
      if (!selectedLevel) {
        throw new Error("No multiscale level is available");
      }
      const unsupported = explainUnsupportedLevel(selectedLevel);
      if (unsupported.length > 0) {
        throw new Error(`Selected multiscale level is unsupported: ${unsupported.join("; ")}`);
      }
      const [timeCount, bandCount] = selectedLevel.shape;
      const [timeChunkSize, bandChunkSize] = selectedLevel.chunks;
      if (timeIndex >= timeCount) {
        throw new Error("Requested time index is outside the selected multiscale level");
      }
      if (timeChunkSize !== 1 || bandChunkSize !== 1) {
        throw new Error("Only one-time, one-band browser chunks are supported");
      }

      const window = chooseReadWindow(selectedLevel, viewportBounds, BROWSER_NATIVE_BUDGET);
      if (renderKind === "composite") {
        if (!compositeBands || compositeBands.length !== 3) {
          throw new Error("Composite rendering requires exactly three bands");
        }
        const bandPlanes: BrowserMultiscaleBandPlane[] = [];
        let loadedBytes = 0;
        for (const band of compositeBands) {
          const bandIndex = profile.variable_ids.indexOf(band.variable);
          if (bandIndex < 0 || bandIndex >= bandCount) {
            throw new Error(`Composite band ${band.variable} is not present in the multiscale store`);
          }
          const plane = await loadLevelPlaneWindow(
            metadataQuery.data.proxyRoot,
            metadataQuery.data.dataArrayName,
            selectedLevel,
            {
              timeIndex,
              bandIndex,
              window,
              budget: BROWSER_NATIVE_BUDGET,
              signal
            }
          );
          loadedBytes += plane.loadedBytes;
          bandPlanes.push({
            variable: band.variable,
            rawValues: plane.values,
            width: plane.width,
            height: plane.height,
            vmin: band.vmin,
            vmax: band.vmax
          });
        }
        const [red, green, blue] = bandPlanes;
        if (!red || !green || !blue) {
          throw new Error("Composite band loading failed");
        }
        const rendered = renderCompositeMultiscaleRaster([red, green, blue]);
        return {
          renderKind: "composite",
          dataUrl: rendered.dataUrl,
          coordinates: bboxToImageCoordinates(window.bbox),
          levelPath: selectedLevel.path,
          browseZoom: selectedLevel.browseZoom,
          mode: window.mode,
          pixelCount: rendered.width * rendered.height,
          chunkCount: estimateCompositeChunkCount(selectedLevel, window, compositeBands.length),
          loadedBytes,
          estimatedChunkBytes: estimateCompositeBytes(selectedLevel, window, compositeBands.length),
          bands: [red, green, blue]
        };
      }

      if (!palette || !variable || vmin === null || vmax === null) {
        throw new Error("Single-band browser-native rendering inputs are incomplete");
      }
      const bandIndex = profile.variable_ids.indexOf(variable);
      if (bandIndex < 0 || bandIndex >= bandCount) {
        throw new Error("Selected variable is not present in the multiscale store");
      }
      const plane = await loadLevelPlaneWindow(metadataQuery.data.proxyRoot, metadataQuery.data.dataArrayName, selectedLevel, {
        timeIndex,
        bandIndex,
        window,
        budget: BROWSER_NATIVE_BUDGET,
        signal
      });
      const rendered = renderMultiscaleRaster(plane.values, {
        width: plane.width,
        height: plane.height,
        palette,
        vmin,
        vmax
      });

      return {
        renderKind: "single-band",
        dataUrl: rendered.dataUrl,
        coordinates: bboxToImageCoordinates(plane.bbox),
        levelPath: plane.levelPath,
        browseZoom: plane.browseZoom,
        mode: plane.mode,
        pixelCount: plane.pixelCount,
        chunkCount: plane.chunkCount,
        loadedBytes: plane.loadedBytes,
        estimatedChunkBytes: plane.estimatedChunkBytes,
        rawValues: plane.values,
        width: plane.width,
        height: plane.height,
        vmin,
        vmax,
        paletteImageData: rendered.paletteImageData
      };
    },
    enabled: gate.enabled && Boolean(
      metadataQuery.data &&
        (
          renderKind === "composite"
            ? compositeBands?.length === 3
            : palette && vmin !== null && vmax !== null
        )
    ),
    staleTime: 60_000,
    retry: false
  });

  if (!gate.enabled) {
    return result(null, "fallback", gate.reason);
  }
  if (metadataQuery.isError) {
    return result(null, "fallback", errorToMessage(metadataQuery.error));
  }
  if (imageQuery.isError) {
    return result(null, "fallback", errorToMessage(imageQuery.error));
  }
  if (imageQuery.data) {
    const reason = imageQuery.data.browseZoom === null
      ? `browser-native ${imageQuery.data.mode} ${imageQuery.data.levelPath}`
      : `browser-native ${imageQuery.data.mode} ${imageQuery.data.levelPath} at z${imageQuery.data.browseZoom}`;
    return {
      image: imageQuery.data,
      status: "native",
      reason,
      debug: debugFromImage(imageQuery.data, "native", reason)
    };
  }
  return result(null, "native-loading", "loading browser-native multiscale data");
}

function getBrowserNativeGate(
  profile: DatasetServingProfile | undefined,
  variables: string[]
): { enabled: boolean; reason: string } {
  if (!profile) {
    return { enabled: false, reason: "serving profile unavailable" };
  }
  if (variables.length === 0) {
    return { enabled: false, reason: "no variable selected" };
  }
  if (!profile.browser_multiscale_ready) {
    return {
      enabled: false,
      reason: profile.seamless_rendering_gaps.length > 0
        ? `server tiles: ${profile.seamless_rendering_gaps.join(", ")}`
        : "server tiles: browser multiscale not ready"
    };
  }
  if (!profile.supported_rendering_modes.includes("multiscale_proxy")) {
    return { enabled: false, reason: "server tiles: missing multiscale proxy mode" };
  }
  if (!profile.multiscale_proxy_root || !profile.data_array_name) {
    return { enabled: false, reason: "server tiles: missing multiscale proxy metadata" };
  }
  if (profile.multiscale_zarr_format !== 2 || profile.multiscale_zarr_consolidated !== true) {
    return { enabled: false, reason: "server tiles: unsupported multiscale Zarr metadata" };
  }
  if (profile.zarr_format === null || profile.zarr_format === undefined) {
    return { enabled: false, reason: "server tiles: source Zarr format unknown" };
  }
  if (!profile.chunk_layout?.inner_chunk_shape || profile.chunk_layout.inner_chunk_shape.length < 4) {
    return { enabled: false, reason: "server tiles: source chunk layout unknown" };
  }
  const missingVariable = variables.find((item) => !profile.variable_ids.includes(item));
  if (missingVariable) {
    return { enabled: false, reason: `server tiles: variable ${missingVariable} missing from serving profile` };
  }
  return { enabled: true, reason: "browser-native eligible" };
}

function bboxToImageCoordinates(
  bbox: [number, number, number, number]
): [[number, number], [number, number], [number, number], [number, number]] {
  const [west, south, east, north] = bbox;
  return [
    [west, north],
    [east, north],
    [east, south],
    [west, south]
  ];
}

function errorToMessage(error: unknown): string {
  return error instanceof Error ? `server tiles: ${error.message}` : "server tiles: browser-native rendering failed";
}

function estimateCompositeChunkCount(
  level: MultiscaleLevelDescriptor,
  window: MultiscaleReadWindow,
  bandCount: number
): number {
  return estimateReadWindow(level, window).chunkCount * bandCount;
}

function estimateCompositeBytes(
  level: MultiscaleLevelDescriptor,
  window: MultiscaleReadWindow,
  bandCount: number
): number {
  return estimateReadWindow(level, window).estimatedChunkBytes * bandCount;
}

function result(
  image: BrowserMultiscaleImage | null,
  status: "native" | "native-loading" | "fallback",
  reason: string
): BrowserMultiscaleResult {
  return {
    image,
    status,
    reason,
    debug: image ? debugFromImage(image, status, reason) : emptyDebug(status, reason)
  };
}

function debugFromImage(
  image: BrowserMultiscaleImage,
  status: "native" | "native-loading" | "fallback",
  reason: string
): BrowserMultiscaleDebug {
  return {
    mode: image.mode,
    status,
    reason,
    levelPath: image.levelPath,
    browseZoom: image.browseZoom,
    pixelCount: image.pixelCount,
    chunkCount: image.chunkCount,
    loadedBytes: image.loadedBytes,
    estimatedChunkBytes: image.estimatedChunkBytes,
    maxPixels: BROWSER_NATIVE_BUDGET.maxPixels,
    maxChunks: BROWSER_NATIVE_BUDGET.maxChunks,
    maxChunkBytes: BROWSER_NATIVE_BUDGET.maxChunkBytes,
    maxConcurrentChunkLoads: BROWSER_NATIVE_BUDGET.maxConcurrentChunkLoads
  };
}

function emptyDebug(
  status: "native" | "native-loading" | "fallback",
  reason: string
): BrowserMultiscaleDebug {
  return {
    mode: "none",
    status,
    reason,
    levelPath: null,
    browseZoom: null,
    pixelCount: 0,
    chunkCount: 0,
    loadedBytes: 0,
    estimatedChunkBytes: 0,
    maxPixels: BROWSER_NATIVE_BUDGET.maxPixels,
    maxChunks: BROWSER_NATIVE_BUDGET.maxChunks,
    maxChunkBytes: BROWSER_NATIVE_BUDGET.maxChunkBytes,
    maxConcurrentChunkLoads: BROWSER_NATIVE_BUDGET.maxConcurrentChunkLoads
  };
}
