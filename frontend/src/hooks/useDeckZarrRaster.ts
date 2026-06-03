import { useMemo } from "react";

import type { Layer as DeckLayer } from "@deck.gl/core";

import type { BrowserMultiscaleImage, BrowserMultiscaleResult } from "./useBrowserMultiscale";
import { ZarrColormapBitmapLayer, ZarrCompositeBitmapLayer } from "../lib/gpuRaster";
import type { DatasetServingProfile } from "../types";

const BROWSER_GPU_TEXTURE_BUDGET = {
  maxTextureDimension: 4096,
  failureFallbackThreshold: 1
};

interface UseDeckZarrRasterOptions {
  profile: DatasetServingProfile | undefined;
  image: BrowserMultiscaleImage | null;
  browserMultiscaleStatus: BrowserMultiscaleResult["status"];
  browserMultiscaleReason: string;
  enabled: boolean;
  failureCount?: number;
  lastFailureReason?: string | null;
}

interface DeckZarrRasterDebug {
  status: "native" | "native-loading" | "fallback";
  reason: string;
  levelPath: string | null;
  mode: "none" | "full-level" | "viewport-window";
  renderer: "none" | "raw-float-shader-colormap" | "raw-float-composite";
  maxTextureDimension: number;
  failureFallbackThreshold: number;
  failureCount: number;
  lastFailureReason: string | null;
}

interface DeckZarrRasterResult {
  layers: DeckLayer[];
  active: boolean;
  debug: DeckZarrRasterDebug;
}

export function useDeckZarrRaster({
  profile,
  image,
  browserMultiscaleStatus,
  browserMultiscaleReason,
  enabled,
  failureCount = 0,
  lastFailureReason = null
}: UseDeckZarrRasterOptions): DeckZarrRasterResult {
  return useMemo(() => {
    const gate = getBrowserGpuGate(profile, enabled);
    if (!gate.enabled) {
      return emptyResult("fallback", gate.reason, { failureCount, lastFailureReason });
    }
    if (failureCount >= BROWSER_GPU_TEXTURE_BUDGET.failureFallbackThreshold) {
      return emptyResult(
        "fallback",
        `server tiles: browser GPU failed ${failureCount} time(s)${
          lastFailureReason ? `: ${lastFailureReason}` : ""
        }`,
        { failureCount, lastFailureReason }
      );
    }
    if (!image) {
      if (browserMultiscaleStatus === "fallback") {
        return emptyResult("fallback", browserMultiscaleReason, { failureCount, lastFailureReason });
      }
      return emptyResult("native-loading", "loading browser-GPU raster source", { failureCount, lastFailureReason });
    }
    const textureGate = getTextureGate(image);
    if (!textureGate.enabled) {
      return emptyResult("fallback", textureGate.reason, { failureCount, lastFailureReason });
    }

    if (image.renderKind === "composite") {
      const reason = image.browseZoom === null
        ? `browser-gpu raw-float composite ${image.mode} ${image.levelPath}`
        : `browser-gpu raw-float composite ${image.mode} ${image.levelPath} at z${image.browseZoom}`;
      const [red, green, blue] = image.bands;
      const layer = new ZarrCompositeBitmapLayer({
        id: `vizarr-browser-gpu:${profile?.dataset_id ?? "dataset"}:${image.levelPath}:composite`,
        redValues: {
          data: red.rawValues,
          width: red.width,
          height: red.height
        },
        greenValues: {
          data: green.rawValues,
          width: green.width,
          height: green.height
        },
        blueValues: {
          data: blue.rawValues,
          width: blue.width,
          height: blue.height
        },
        redRange: [red.vmin, red.vmax],
        greenRange: [green.vmin, green.vmax],
        blueRange: [blue.vmin, blue.vmax],
        bounds: imageCoordinatesToBounds(image.coordinates),
        opacity: 0.9,
        pickable: false,
        textureParameters: {
          minFilter: "nearest",
          magFilter: "nearest",
          mipmapFilter: "nearest"
        }
      });

      return {
        layers: [layer],
        active: true,
        debug: {
          status: "native",
          reason,
          levelPath: image.levelPath,
          mode: image.mode,
          renderer: "raw-float-composite",
          maxTextureDimension: BROWSER_GPU_TEXTURE_BUDGET.maxTextureDimension,
          failureFallbackThreshold: BROWSER_GPU_TEXTURE_BUDGET.failureFallbackThreshold,
          failureCount,
          lastFailureReason
        }
      };
    }

    const reason = image.browseZoom === null
      ? `browser-gpu raw-float shader-colormap ${image.mode} ${image.levelPath}`
      : `browser-gpu raw-float shader-colormap ${image.mode} ${image.levelPath} at z${image.browseZoom}`;
    const layer = new ZarrColormapBitmapLayer({
      id: `vizarr-browser-gpu:${profile?.dataset_id ?? "dataset"}:${image.levelPath}`,
      rawValues: {
        data: image.rawValues,
        width: image.width,
        height: image.height
      },
      paletteTexture: image.paletteImageData,
      scalarRange: [image.vmin, image.vmax],
      bounds: imageCoordinatesToBounds(image.coordinates),
      opacity: 0.9,
      pickable: false,
      textureParameters: {
        minFilter: "nearest",
        magFilter: "nearest",
        mipmapFilter: "nearest"
      }
    });

    return {
      layers: [layer],
      active: true,
      debug: {
        status: "native",
        reason,
        levelPath: image.levelPath,
        mode: image.mode,
        renderer: "raw-float-shader-colormap",
        maxTextureDimension: BROWSER_GPU_TEXTURE_BUDGET.maxTextureDimension,
        failureFallbackThreshold: BROWSER_GPU_TEXTURE_BUDGET.failureFallbackThreshold,
        failureCount,
        lastFailureReason
      }
    };
  }, [
    browserMultiscaleReason,
    browserMultiscaleStatus,
    enabled,
    failureCount,
    image,
    lastFailureReason,
    profile
  ]);
}

function getBrowserGpuGate(
  profile: DatasetServingProfile | undefined,
  enabled: boolean
): { enabled: boolean; reason: string } {
  if (!enabled) {
    return { enabled: false, reason: "browser GPU is not enabled for this render mode" };
  }
  if (!profile) {
    return { enabled: false, reason: "serving profile unavailable" };
  }
  if (profile.browser_gpu_ready !== true) {
    return {
      enabled: false,
      reason: profile.browser_gpu_reason
        ? `server tiles: ${profile.browser_gpu_reason}`
        : profile.browser_gpu_gaps && profile.browser_gpu_gaps.length > 0
          ? `server tiles: ${profile.browser_gpu_gaps.join(", ")}`
          : profile.seamless_rendering_gaps.length > 0
            ? `server tiles: ${profile.seamless_rendering_gaps.join(", ")}`
            : "server tiles: browser GPU sidecar not ready"
    };
  }
  if (!profile.supported_rendering_modes.includes("browser_gpu")) {
    return { enabled: false, reason: "server tiles: missing browser_gpu rendering mode" };
  }
  return { enabled: true, reason: "browser GPU eligible" };
}

function emptyResult(
  status: DeckZarrRasterDebug["status"],
  reason: string,
  failureState: {
    failureCount?: number;
    lastFailureReason?: string | null;
  } = {}
): DeckZarrRasterResult {
  return {
    layers: [],
    active: false,
    debug: {
      status,
      reason,
      levelPath: null,
      mode: "none",
      renderer: "none",
      maxTextureDimension: BROWSER_GPU_TEXTURE_BUDGET.maxTextureDimension,
      failureFallbackThreshold: BROWSER_GPU_TEXTURE_BUDGET.failureFallbackThreshold,
      failureCount: failureState.failureCount ?? 0,
      lastFailureReason: failureState.lastFailureReason ?? null
    }
  };
}

function getTextureGate(image: BrowserMultiscaleImage): { enabled: boolean; reason: string } {
  const width = image.renderKind === "composite" ? image.bands[0].width : image.width;
  const height = image.renderKind === "composite" ? image.bands[0].height : image.height;
  const limit = BROWSER_GPU_TEXTURE_BUDGET.maxTextureDimension;
  if (width > limit || height > limit) {
    return {
      enabled: false,
      reason: `server tiles: browser GPU texture ${width}x${height} exceeds ${limit}px limit`
    };
  }
  return { enabled: true, reason: "browser GPU texture budget passed" };
}

function imageCoordinatesToBounds(
  coordinates: BrowserMultiscaleImage["coordinates"]
): [number, number, number, number] {
  const west = coordinates[0][0];
  const north = coordinates[0][1];
  const east = coordinates[2][0];
  const south = coordinates[2][1];
  return [west, south, east, north];
}
