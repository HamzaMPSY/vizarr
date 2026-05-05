import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { useColormapPalette } from "./useDatasets";
import {
  levelSupportsDirectChunkRead,
  loadLevelPlane,
  loadMultiscaleMetadata,
  renderChunkToDataUrl,
  selectMultiscaleLevel
} from "../lib/multiscale";
import type { DatasetServingProfile } from "../types";

const MAX_BROWSER_NATIVE_PIXELS = 1024 * 1024;
const MAX_BROWSER_NATIVE_CHUNKS = 64;

interface UseBrowserMultiscaleOptions {
  profile: DatasetServingProfile | undefined;
  variable: string | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  zoom: number;
}

interface BrowserMultiscaleImage {
  dataUrl: string;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  levelPath: string;
  browseZoom: number | null;
}

interface BrowserMultiscaleResult {
  image: BrowserMultiscaleImage | null;
  status: "native" | "native-loading" | "fallback";
  reason: string;
}

export function useBrowserMultiscale({
  profile,
  variable,
  timeIndex,
  colormap,
  vmin,
  vmax,
  zoom
}: UseBrowserMultiscaleOptions): BrowserMultiscaleResult {
  const gate = useMemo(() => getBrowserNativeGate(profile, variable), [profile, variable]);
  const { data: palette } = useColormapPalette(gate.enabled ? colormap : null);

  const metadataQuery = useQuery({
    queryKey: [
      "browser-multiscale-metadata",
      profile?.dataset_id,
      profile?.multiscale_proxy_root,
      profile?.data_array_name
    ],
    queryFn: () => loadMultiscaleMetadata(profile?.multiscale_proxy_root ?? "", profile?.data_array_name ?? ""),
    enabled: gate.enabled,
    staleTime: 300_000
  });

  const imageQuery = useQuery({
    queryKey: [
      "browser-multiscale-image",
      profile?.dataset_id,
      variable,
      timeIndex,
      colormap,
      vmin,
      vmax,
      Math.floor(zoom),
      metadataQuery.data?.levels.map((level) => `${level.path}:${level.browseZoom}`).join(",")
    ],
    queryFn: async (): Promise<BrowserMultiscaleImage> => {
      if (!metadataQuery.data || !palette || !profile || !variable || vmin === null || vmax === null) {
        throw new Error("Browser-native rendering inputs are incomplete");
      }
      const selectedLevel = selectMultiscaleLevel(metadataQuery.data, zoom);
      if (!selectedLevel) {
        throw new Error("No multiscale level is available");
      }
      if (!levelSupportsDirectChunkRead(selectedLevel)) {
        throw new Error("Selected multiscale level uses unsupported compressor, filters, dtype, order, or chunk shape");
      }
      const [timeCount, bandCount, height, width] = selectedLevel.shape;
      const [timeChunkSize, bandChunkSize, chunkHeight, chunkWidth] = selectedLevel.chunks;
      const chunkCount = Math.ceil(height / chunkHeight) * Math.ceil(width / chunkWidth);
      if (height * width > MAX_BROWSER_NATIVE_PIXELS || chunkCount > MAX_BROWSER_NATIVE_CHUNKS) {
        throw new Error("Selected multiscale level is too large for browser-native full-plane rendering");
      }
      if (timeIndex >= timeCount) {
        throw new Error("Requested time index is outside the selected multiscale level");
      }
      if (timeChunkSize !== 1 || bandChunkSize !== 1) {
        throw new Error("Only one-time, one-band browser chunks are supported");
      }

      const bandIndex = profile.variable_ids.indexOf(variable);
      if (bandIndex < 0 || bandIndex >= bandCount) {
        throw new Error("Selected variable is not present in the multiscale store");
      }

      const values = await loadLevelPlane(metadataQuery.data.proxyRoot, metadataQuery.data.dataArrayName, selectedLevel, {
        timeIndex,
        bandIndex
      });
      const dataUrl = renderChunkToDataUrl(values, {
        width,
        height,
        palette,
        vmin,
        vmax
      });

      return {
        dataUrl,
        coordinates: bboxToImageCoordinates(selectedLevel.bbox),
        levelPath: selectedLevel.path,
        browseZoom: selectedLevel.browseZoom
      };
    },
    enabled: gate.enabled && Boolean(metadataQuery.data && palette && vmin !== null && vmax !== null),
    staleTime: 60_000,
    retry: false
  });

  if (!gate.enabled) {
    return { image: null, status: "fallback", reason: gate.reason };
  }
  if (metadataQuery.isError) {
    return { image: null, status: "fallback", reason: errorToMessage(metadataQuery.error) };
  }
  if (imageQuery.isError) {
    return { image: null, status: "fallback", reason: errorToMessage(imageQuery.error) };
  }
  if (imageQuery.data) {
    return {
      image: imageQuery.data,
      status: "native",
      reason: imageQuery.data.browseZoom === null
        ? `browser-native ${imageQuery.data.levelPath}`
        : `browser-native ${imageQuery.data.levelPath} at z${imageQuery.data.browseZoom}`
    };
  }
  return { image: null, status: "native-loading", reason: "loading browser-native multiscale data" };
}

function getBrowserNativeGate(
  profile: DatasetServingProfile | undefined,
  variable: string | null
): { enabled: boolean; reason: string } {
  if (!profile) {
    return { enabled: false, reason: "serving profile unavailable" };
  }
  if (!variable) {
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
  if (!profile.variable_ids.includes(variable)) {
    return { enabled: false, reason: "server tiles: variable missing from serving profile" };
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
