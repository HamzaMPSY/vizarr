import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getTimeStepCount } from "../lib/temporal";
import { useMapStore } from "../store/mapStore";
import type { DisplayRangeMode, MapViewState, RenderMode, UrlHydrationPatch } from "../store/mapStore";
import type { DatasetMeta, VariableMeta } from "../types";
import { useColormaps } from "./useDatasets";
import { useDebouncedValue } from "./useDebouncedValue";

const SHARE_API_KEY_IN_URL = import.meta.env.VITE_SHARE_API_KEY_IN_URL === "true";
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

interface UseShareableUrlStateParams {
  datasets: DatasetMeta[] | undefined;
  variables: VariableMeta[] | undefined;
}

interface UrlRequest {
  datasetId: string | null;
  variable: string | null;
  renderMode: RenderMode | null;
  compositeStyle: string | null;
  timeIndex: number | null;
  colormap: string | null;
  rangeMode: DisplayRangeMode | null;
  hasDisplayRange: boolean;
  hasCamera: boolean;
}

interface ParsedShareUrl {
  patch: UrlHydrationPatch;
  request: UrlRequest;
  warnings: string[];
}

export type ShareCopyStatus = "idle" | "copied" | "failed";

interface ShareableStateSnapshot {
  datasetId: string | null;
  variable: string | null;
  renderMode: RenderMode;
  compositeStyle: string | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  rangeMode: DisplayRangeMode;
  viewState: MapViewState;
}

export function useShareableUrlState({ datasets, variables }: UseShareableUrlStateParams) {
  const [parsedUrl, setParsedUrl] = useState<ParsedShareUrl>(() => parseCurrentUrl());
  const [hasHydratedUrl, setHasHydratedUrl] = useState(false);
  const [semanticWarnings, setSemanticWarnings] = useState<string[]>([]);
  const [copyStatus, setCopyStatus] = useState<ShareCopyStatus>("idle");
  const copyResetTimeoutRef = useRef<number | null>(null);

  const datasetId = useMapStore((state) => state.datasetId);
  const variable = useMapStore((state) => state.variable);
  const renderMode = useMapStore((state) => state.renderMode);
  const compositeStyle = useMapStore((state) => state.compositeStyle);
  const timeIndex = useMapStore((state) => state.timeIndex);
  const colormap = useMapStore((state) => state.colormap);
  const vmin = useMapStore((state) => state.vmin);
  const vmax = useMapStore((state) => state.vmax);
  const rangeMode = useMapStore((state) => state.rangeMode);
  const viewState = useMapStore((state) => state.viewState);
  const hydrateFromUrl = useMapStore((state) => state.hydrateFromUrl);
  const setRenderMode = useMapStore((state) => state.setRenderMode);
  const setColormap = useMapStore((state) => state.setColormap);
  const setTimeIndex = useMapStore((state) => state.setTimeIndex);
  const debouncedViewState = useDebouncedValue(viewState, 180);
  const { data: colormaps } = useColormaps();

  useEffect(() => {
    hydrateFromUrl(parsedUrl.patch);
    setHasHydratedUrl(true);
  }, [hydrateFromUrl, parsedUrl]);

  useEffect(() => {
    const handlePopState = () => {
      setParsedUrl(parseCurrentUrl());
    };

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!hasHydratedUrl || typeof window === "undefined") {
      return;
    }

    const nextUrl = buildShareUrl(
      {
        datasetId,
        variable,
        renderMode,
        compositeStyle,
        timeIndex,
        colormap,
        vmin,
        vmax,
        rangeMode,
        viewState: debouncedViewState
      },
      { includeApiKey: SHARE_API_KEY_IN_URL }
    );

    if (nextUrl !== window.location.href) {
      window.history.replaceState(window.history.state, "", nextUrl);
    }
  }, [
    colormap,
    compositeStyle,
    datasetId,
    debouncedViewState,
    hasHydratedUrl,
    rangeMode,
    renderMode,
    timeIndex,
    variable,
    vmax,
    vmin
  ]);

  useEffect(() => {
    if (!hasHydratedUrl) {
      return;
    }

    const warnings: string[] = [];
    const request = parsedUrl.request;
    const selectedDataset = datasets?.find((item) => item.id === datasetId) ?? null;
    const selectedVariable = variables?.find((item) => item.id === variable) ?? null;
    const compositeStyles = selectedDataset?.composite_styles ?? [];
    const selectedComposite = compositeStyles.find((item) => item.id === compositeStyle) ?? null;

    if (request.datasetId && datasets && !datasets.some((item) => item.id === request.datasetId)) {
      warnings.push(`Shared dataset "${request.datasetId}" is not available. Showing the default dataset.`);
    }

    if (request.variable && variables && !variables.some((item) => item.id === request.variable)) {
      warnings.push(`Shared variable "${request.variable}" is not available for this dataset. Showing the default variable.`);
    }

    if (request.renderMode === "composite" && selectedDataset && compositeStyles.length === 0) {
      warnings.push("Shared composite mode is not available for this dataset. Showing a single band instead.");
      if (renderMode === "composite") {
        setRenderMode("band");
      }
    }

    if (
      request.compositeStyle &&
      selectedDataset &&
      compositeStyles.length > 0 &&
      !compositeStyles.some((item) => item.id === request.compositeStyle)
    ) {
      warnings.push(`Shared composite "${request.compositeStyle}" is not available. Showing the default composite.`);
    }

    const timeStepCount =
      selectedDataset && variables
        ? getTimeStepCount({
            dataset: selectedDataset,
            variable: selectedVariable,
            variables,
            renderMode,
            composite: selectedComposite
          })
        : null;

    if (request.timeIndex !== null && timeStepCount !== null && request.timeIndex >= timeStepCount) {
      warnings.push(`Shared time step ${request.timeIndex + 1} is outside this dataset. Showing the first step.`);
      if (timeIndex === request.timeIndex) {
        setTimeIndex(0);
      }
    }

    if (request.colormap && colormaps && !colormaps.includes(request.colormap)) {
      warnings.push(`Shared colormap "${request.colormap}" is not available. Showing the dataset default.`);
      if (colormap === request.colormap) {
        setColormap(selectedVariable?.default_colormap ?? "viridis");
      }
    }

    setSemanticWarnings((current) => (sameStringArray(current, warnings) ? current : warnings));
  }, [
    colormap,
    colormaps,
    compositeStyle,
    datasetId,
    datasets,
    hasHydratedUrl,
    parsedUrl.request,
    renderMode,
    setColormap,
    setRenderMode,
    setTimeIndex,
    timeIndex,
    variable,
    variables
  ]);

  useEffect(
    () => () => {
      if (copyResetTimeoutRef.current !== null) {
        window.clearTimeout(copyResetTimeoutRef.current);
      }
    },
    []
  );

  const warnings = useMemo(
    () => uniqueStrings([...parsedUrl.warnings, ...semanticWarnings]),
    [parsedUrl.warnings, semanticWarnings]
  );

  const copyShareLink = useCallback(async () => {
    if (copyResetTimeoutRef.current !== null) {
      window.clearTimeout(copyResetTimeoutRef.current);
    }

    const url = buildShareUrl(useMapStore.getState(), { includeApiKey: SHARE_API_KEY_IN_URL });
    try {
      await writeClipboardText(url);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }

    copyResetTimeoutRef.current = window.setTimeout(() => {
      setCopyStatus("idle");
      copyResetTimeoutRef.current = null;
    }, 1800);
  }, []);

  return {
    requested: parsedUrl.request,
    warnings,
    copyShareLink,
    copyStatus
  };
}

function parseCurrentUrl(): ParsedShareUrl {
  if (typeof window === "undefined") {
    return parseShareUrlSearch("");
  }
  return parseShareUrlSearch(window.location.search);
}

function parseShareUrlSearch(search: string): ParsedShareUrl {
  const params = new URLSearchParams(search);
  const patch: UrlHydrationPatch = {};
  const warnings: string[] = [];
  const request: UrlRequest = {
    datasetId: null,
    variable: null,
    renderMode: null,
    compositeStyle: null,
    timeIndex: null,
    colormap: null,
    rangeMode: null,
    hasDisplayRange: false,
    hasCamera: false
  };

  const datasetId = cleanString(params.get("dataset"));
  if (datasetId) {
    request.datasetId = datasetId;
    patch.datasetId = datasetId;
  }

  const variable = cleanString(params.get("variable"));
  if (variable) {
    request.variable = variable;
    patch.variable = variable;
  }

  const mode = cleanString(params.get("mode"));
  if (mode) {
    if (mode === "band" || mode === "composite") {
      request.renderMode = mode;
      patch.renderMode = mode;
    } else {
      warnings.push(`Shared render mode "${mode}" is not valid. Showing the default mode.`);
    }
  }

  const compositeStyle = cleanString(params.get("composite"));
  if (compositeStyle) {
    request.compositeStyle = compositeStyle;
    patch.compositeStyle = compositeStyle;
    patch.renderMode = "composite";
    request.renderMode = request.renderMode ?? "composite";
  }

  const parsedTime = parseIntegerParam(params, "time", { min: 0 });
  if (parsedTime.warning) {
    warnings.push(parsedTime.warning);
  }
  if (parsedTime.value !== null) {
    request.timeIndex = parsedTime.value;
    patch.timeIndex = parsedTime.value;
  }

  const colormap = cleanString(params.get("colormap"));
  if (colormap) {
    request.colormap = colormap;
    patch.colormap = colormap;
  }

  const rangeMode = cleanString(params.get("range"));
  if (rangeMode) {
    if (rangeMode === "auto" || rangeMode === "seeded" || rangeMode === "manual") {
      request.rangeMode = rangeMode;
      patch.rangeMode = rangeMode;
    } else {
      warnings.push(`Shared display range mode "${rangeMode}" is not valid. Showing automatic range.`);
    }
  }

  const vmin = parseNumberParam(params, "vmin");
  const vmax = parseNumberParam(params, "vmax");
  if (vmin.warning) {
    warnings.push(vmin.warning);
  }
  if (vmax.warning) {
    warnings.push(vmax.warning);
  }
  if (vmin.value !== null || vmax.value !== null) {
    request.hasDisplayRange = true;
    if (vmin.value === null || vmax.value === null) {
      warnings.push("Shared display range is incomplete. Showing automatic range.");
    } else if (vmin.value >= vmax.value) {
      warnings.push("Shared display range minimum must be less than maximum. Showing automatic range.");
    } else {
      patch.vmin = vmin.value;
      patch.vmax = vmax.value;
      patch.rangeMode = request.rangeMode ?? "manual";
      request.rangeMode = patch.rangeMode;
    }
  } else if (request.rangeMode === "auto") {
    patch.vmin = null;
    patch.vmax = null;
  }

  const camera = parseCameraParams(params, warnings);
  if (camera) {
    request.hasCamera = true;
    patch.viewState = camera;
    patch.urlCameraRestored = true;
  }

  return { patch, request, warnings };
}

function parseCameraParams(params: URLSearchParams, warnings: string[]): MapViewState | null {
  const hasCoreCamera = params.has("lon") || params.has("lat") || params.has("zoom");
  if (!hasCoreCamera) {
    return null;
  }

  const lon = parseNumberParam(params, "lon", { min: -180, max: 180 });
  const lat = parseNumberParam(params, "lat", { min: -85.05112878, max: 85.05112878 });
  const zoom = parseNumberParam(params, "zoom", { min: 0, max: 24 });
  const pitch = parseNumberParam(params, "pitch", { min: 0, max: 85, optional: true });
  const bearing = parseNumberParam(params, "bearing", { min: -180, max: 180, optional: true });
  const parts = [lon, lat, zoom, pitch, bearing];

  for (const part of parts) {
    if (part.warning) {
      warnings.push(part.warning);
    }
  }

  if (lon.value === null || lat.value === null || zoom.value === null) {
    warnings.push("Shared map camera is incomplete. Fitting the selected dataset instead.");
    return null;
  }

  return {
    longitude: lon.value,
    latitude: lat.value,
    zoom: zoom.value,
    pitch: pitch.value ?? 0,
    bearing: bearing.value ?? 0
  };
}

function buildShareUrl(state: ShareableStateSnapshot, options: { includeApiKey: boolean }): string {
  const url = new URL(window.location.pathname || "/", window.location.origin);
  const params = url.searchParams;
  if (new URLSearchParams(window.location.search).get("debug") === "1") {
    params.set("debug", "1");
  }

  if (state.datasetId) {
    params.set("dataset", state.datasetId);
  }
  params.set("mode", state.renderMode);
  if (state.renderMode === "composite" && state.compositeStyle) {
    params.set("composite", state.compositeStyle);
  } else if (state.variable) {
    params.set("variable", state.variable);
  }
  params.set("time", String(state.timeIndex));
  params.set("colormap", state.colormap);
  params.set("range", state.rangeMode);
  if (state.vmin !== null && state.vmax !== null) {
    params.set("vmin", formatUrlNumber(state.vmin, 6));
    params.set("vmax", formatUrlNumber(state.vmax, 6));
  }
  params.set("lon", formatUrlNumber(state.viewState.longitude, 6));
  params.set("lat", formatUrlNumber(state.viewState.latitude, 6));
  params.set("zoom", formatUrlNumber(state.viewState.zoom, 3));
  params.set("pitch", formatUrlNumber(state.viewState.pitch, 2));
  params.set("bearing", formatUrlNumber(state.viewState.bearing, 2));

  if (options.includeApiKey && API_KEY) {
    params.set("api_key", API_KEY);
  }

  return url.toString();
}

function cleanString(value: string | null): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed.length > 0 ? trimmed : null;
}

function parseIntegerParam(
  params: URLSearchParams,
  name: string,
  options: { min?: number; max?: number } = {}
): { value: number | null; warning: string | null } {
  const parsed = parseNumberParam(params, name, options);
  if (parsed.value === null) {
    return parsed;
  }
  if (!Number.isInteger(parsed.value)) {
    return { value: null, warning: `Shared ${name} must be an integer. Showing the default value.` };
  }
  return { value: parsed.value, warning: null };
}

function parseNumberParam(
  params: URLSearchParams,
  name: string,
  options: { min?: number; max?: number; optional?: boolean } = {}
): { value: number | null; warning: string | null } {
  const raw = params.get(name);
  if (raw === null || raw.trim() === "") {
    return { value: null, warning: null };
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    return { value: null, warning: `Shared ${name} value "${raw}" is not a valid number. Showing the default value.` };
  }
  if (options.min !== undefined && value < options.min) {
    return {
      value: null,
      warning: `Shared ${name} value ${raw} is below the supported range. Showing the default value.`
    };
  }
  if (options.max !== undefined && value > options.max) {
    return {
      value: null,
      warning: `Shared ${name} value ${raw} is above the supported range. Showing the default value.`
    };
  }
  return { value, warning: null };
}

function formatUrlNumber(value: number, digits: number): string {
  return Number(value.toFixed(digits)).toString();
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

function sameStringArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

async function writeClipboardText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  if (copyTextFallback(text)) {
    return;
  }
  throw new Error("Clipboard is not available");
}

function copyTextFallback(text: string): boolean {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  return copied;
}
