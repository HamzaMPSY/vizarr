import { useEffect } from "react";

import { DeckRasterOverlay } from "./DeckRasterOverlay";
import type { BrowserMultiscaleImage, BrowserMultiscaleResult } from "../hooks/useBrowserMultiscale";
import { useDeckZarrRaster } from "../hooks/useDeckZarrRaster";
import type { DeckZarrRasterDebug } from "../hooks/useDeckZarrRaster";
import type { DatasetServingProfile } from "../types";

export interface BrowserGpuOverlayState {
  attemptKey: string | null;
  active: boolean;
  debug: DeckZarrRasterDebug;
}

interface BrowserGpuOverlayProps {
  attemptKey: string | null;
  profile: DatasetServingProfile | undefined;
  image: BrowserMultiscaleImage | null;
  browserMultiscaleStatus: BrowserMultiscaleResult["status"];
  browserMultiscaleReason: string;
  enabled: boolean;
  failureCount: number;
  lastFailureReason: string | null;
  beforeId?: string;
  onError: (error: unknown) => void;
  onStateChange: (state: BrowserGpuOverlayState) => void;
}

export function BrowserGpuOverlay({
  attemptKey,
  profile,
  image,
  browserMultiscaleStatus,
  browserMultiscaleReason,
  enabled,
  failureCount,
  lastFailureReason,
  beforeId,
  onError,
  onStateChange
}: BrowserGpuOverlayProps) {
  const raster = useDeckZarrRaster({
    profile,
    image,
    browserMultiscaleStatus,
    browserMultiscaleReason,
    enabled,
    failureCount,
    lastFailureReason
  });

  useEffect(() => {
    onStateChange({
      attemptKey,
      active: raster.active,
      debug: raster.debug
    });
  }, [attemptKey, onStateChange, raster.active, raster.debug]);

  return <DeckRasterOverlay layers={raster.layers} beforeId={beforeId} onError={onError} />;
}
