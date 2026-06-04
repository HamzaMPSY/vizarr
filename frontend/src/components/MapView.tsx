import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import maplibregl from "maplibre-gl";
import Map, { Layer, Source } from "react-map-gl/maplibre";
import type { ErrorEvent as MapLibreErrorEvent } from "react-map-gl/maplibre";
import type { StyleSpecification } from "maplibre-gl";

import type { BrowserGpuOverlayState } from "./BrowserGpuOverlay";
import { TileDebugOverlay, TileIssueBanner } from "./TileDebugOverlay";
import type { BrowserRenderDebug, TileDebugRecord, TileIssue } from "./TileDebugOverlay";
import type { BrowserMultiscaleBandInput, BrowserMultiscaleImage } from "../hooks/useBrowserMultiscale";
import { useBrowserMultiscale } from "../hooks/useBrowserMultiscale";
import type { DeckZarrRasterDebug } from "../hooks/useDeckZarrRaster";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useColormapPalette, useDatasets, useServingProfile, useTileJson, useVariables } from "../hooks/useDatasets";
import { useTilePrefetch } from "../hooks/useTilePrefetch";
import type { TilePrefetchDiagnostic } from "../hooks/useTilePrefetch";
import { formatRangeLabel, getEffectiveDisplayRange, paletteToCssGradient } from "../lib/displayRange";
import { buildTimeIndexedTileTemplate, canPrefetchTimeStep, getNextTimeIndex, getTimeStepCount } from "../lib/temporal";
import { useMapStore } from "../store/mapStore";
import type { BBox, CompositeStyle, VariableMeta } from "../types";

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    "esri-world-imagery": {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
      ],
      tileSize: 256,
      attribution: "Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"
    }
  },
  layers: [
    {
      id: "esri-world-imagery",
      type: "raster",
      source: "esri-world-imagery"
    }
  ]
};

const COUNTRY_BORDERS_SOURCE_URL =
  "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_boundary_lines_land.geojson";
const MAX_TILE_DIAGNOSTICS = 24;
const DIAGNOSTIC_REPEAT_WINDOW_MS = 10_000;
const ISSUE_LOOKBACK_MS = 60_000;
const BROWSER_GPU_MAX_TEXTURE_DIMENSION = 4096;
const BROWSER_GPU_FAILURE_FALLBACK_THRESHOLD = 1;

const BrowserGpuOverlay = lazy(() =>
  import("./BrowserGpuOverlay").then((module) => ({
    default: module.BrowserGpuOverlay
  }))
);

interface BrowserGpuFailureState {
  key: string | null;
  count: number;
  reason: string | null;
}

export function MapView() {
  const datasetId = useMapStore((state) => state.datasetId);
  const variable = useMapStore((state) => state.variable);
  const renderMode = useMapStore((state) => state.renderMode);
  const compositeStyle = useMapStore((state) => state.compositeStyle);
  const timeIndex = useMapStore((state) => state.timeIndex);
  const colormap = useMapStore((state) => state.colormap);
  const vmin = useMapStore((state) => state.vmin);
  const vmax = useMapStore((state) => state.vmax);
  const rangeMode = useMapStore((state) => state.rangeMode);
  const timeAnimationPlaying = useMapStore((state) => state.timeAnimationPlaying);
  const timeAnimationLoop = useMapStore((state) => state.timeAnimationLoop);
  const countryBordersEnabled = useMapStore((state) => state.countryBordersEnabled);
  const viewportBounds = useMapStore((state) => state.viewportBounds);
  const viewState = useMapStore((state) => state.viewState);
  const urlCameraRestored = useMapStore((state) => state.urlCameraRestored);
  const setViewportBounds = useMapStore((state) => state.setViewportBounds);
  const setViewState = useMapStore((state) => state.setViewState);
  const setRangeFromTileHeaders = useMapStore((state) => state.setRangeFromTileHeaders);
  const tileVariable = renderMode === "composite" ? compositeStyle : variable;
  const { data: tileJson } = useTileJson(datasetId, tileVariable, timeIndex, colormap, vmin, vmax);
  const { data: datasets } = useDatasets();
  const { data: servingProfile } = useServingProfile(datasetId);
  const { data: variables } = useVariables(datasetId);
  const { data: legendPalette } = useColormapPalette(renderMode === "band" && variable ? colormap : null, 64);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const lastFittedBoundsKeyRef = useRef<string | null>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [deckBeforeId, setDeckBeforeId] = useState<string | undefined>(undefined);
  const [browserGpuFailure, setBrowserGpuFailure] = useState<BrowserGpuFailureState>({
    key: null,
    count: 0,
    reason: null
  });
  const [browserGpuOverlayState, setBrowserGpuOverlayState] = useState<BrowserGpuOverlayState | null>(null);
  const [tileDiagnostics, setTileDiagnostics] = useState<TileDebugRecord[]>([]);
  const [dismissedIssueKey, setDismissedIssueKey] = useState<string | null>(null);
  const [debugOverlayEnabled, setDebugOverlayEnabled] = useState(() => isDebugOverlayEnabled());
  const debouncedViewState = useDebouncedValue(viewState, 180);
  const debouncedViewportBounds = useDebouncedValue(viewportBounds, 180);

  const minZoom = tileJson?.minzoom ?? null;
  const maxZoom = tileJson?.maxzoom ?? null;
  const tileTemplate = tileJson?.tiles[0] ?? null;
  const selectedDataset = datasets?.find((item) => item.id === datasetId) ?? null;
  const selectedComposite = selectedDataset?.composite_styles.find((item) => item.id === compositeStyle) ?? null;
  const selectedVariable = variables?.find((item) => item.id === variable) ?? null;
  const timeStepCount = getTimeStepCount({
    dataset: selectedDataset,
    variable: selectedVariable,
    variables: variables ?? [],
    renderMode,
    composite: selectedComposite ?? null
  });
  const nextTimeIndex = getNextTimeIndex(timeIndex, timeStepCount);
  const effectiveDisplayRange = getEffectiveDisplayRange(selectedVariable, vmin, vmax);
  const nativeVmin = effectiveDisplayRange?.min ?? null;
  const nativeVmax = effectiveDisplayRange?.max ?? null;
  const browserGpuReady = servingProfile?.browser_gpu_ready === true;
  const browserGpuOverlayEligible = Boolean(
    browserGpuReady &&
      servingProfile?.supported_rendering_modes.includes("browser_gpu") &&
      (renderMode === "band" || renderMode === "composite")
  );
  const compositeBands = useMemo(
    () =>
      renderMode === "composite" && browserGpuReady
        ? buildCompositeBandInputs(selectedComposite, variables ?? [])
        : null,
    [browserGpuReady, renderMode, selectedComposite, variables]
  );
  const browserMultiscale = useBrowserMultiscale({
    profile: servingProfile,
    variable: renderMode === "band" ? variable : null,
    compositeBands,
    timeIndex,
    colormap,
    vmin: nativeVmin,
    vmax: nativeVmax,
    zoom: debouncedViewState.zoom,
    viewportBounds: debouncedViewportBounds
  });
  const browserGpuAttemptKey = buildBrowserGpuAttemptKey({
    datasetId,
    tileVariable,
    renderMode,
    timeIndex,
    colormap,
    vmin: nativeVmin,
    vmax: nativeVmax,
    image: browserMultiscale.image
  });
  const activeBrowserGpuFailure =
    browserGpuFailure.key === browserGpuAttemptKey
      ? browserGpuFailure
      : { key: browserGpuAttemptKey, count: 0, reason: null };
  const currentBrowserGpuOverlayState =
    browserGpuOverlayEligible && browserGpuOverlayState?.attemptKey === browserGpuAttemptKey
      ? browserGpuOverlayState
      : null;
  const browserGpuDebug = currentBrowserGpuOverlayState?.debug ?? buildInactiveBrowserGpuDebug({
    profile: servingProfile,
    eligible: browserGpuOverlayEligible,
    failureCount: activeBrowserGpuFailure.count,
    lastFailureReason: activeBrowserGpuFailure.reason
  });
  const browserGpuActive = Boolean(browserGpuOverlayEligible && currentBrowserGpuOverlayState?.active);
  const tileSourceId =
    datasetId && tileVariable
      ? `vizarr-tiles:${datasetId}:${tileVariable}:${timeIndex}:${colormap}:${vmin ?? "auto"}:${vmax ?? "auto"}`
      : null;
  const tileSourceKey = tileSourceId && tileTemplate ? `${tileSourceId}:${tileTemplate}` : tileSourceId;
  const tileLayerId = tileSourceId ? `${tileSourceId}:layer` : null;
  const nativeSourceId =
    datasetId && variable && renderMode === "band" && browserMultiscale.image && !browserGpuActive
      ? `vizarr-native:${datasetId}:${variable}:${timeIndex}:${colormap}:${nativeVmin}:${nativeVmax}`
      : null;
  const nativeLayerId = nativeSourceId ? `${nativeSourceId}:layer` : null;
  const nativeSourceActive = Boolean(
    nativeSourceId &&
      nativeLayerId &&
      browserMultiscale.image?.renderKind === "single-band" &&
      !browserGpuActive
  );
  const browserRenderActive = browserGpuActive || nativeSourceActive;
  const adjacentTimePrefetchEnabled =
    timeAnimationPlaying &&
    timeStepCount > 1 &&
    (timeAnimationLoop || timeIndex < timeStepCount - 1) &&
    canPrefetchTimeStep({
      dataset: selectedDataset,
      profile: servingProfile ?? null,
      renderMode,
      variable: selectedVariable,
      composite: selectedComposite ?? null,
      timeIndex: nextTimeIndex
    });
  const adjacentTimeTileTemplate = adjacentTimePrefetchEnabled
    ? buildTimeIndexedTileTemplate(tileTemplate, nextTimeIndex)
    : null;
  const browserRenderDebug: BrowserRenderDebug = {
    renderMode: browserGpuActive
      ? "browser-gpu"
      : browserMultiscale.status === "native"
        ? "browser-native"
        : "server-tiles",
    nativeStatus: browserMultiscale.debug.status,
    nativeReason: browserMultiscale.debug.reason,
    nativeMode: browserMultiscale.debug.mode,
    nativeChunks: browserMultiscale.debug.chunkCount,
    nativeBytes: browserMultiscale.debug.loadedBytes,
    gpuStatus: browserGpuDebug.status,
    gpuReason: browserGpuDebug.reason,
    gpuRenderer: browserGpuDebug.renderer,
    gpuFailureCount: browserGpuDebug.failureCount,
    gpuLastError: browserGpuDebug.lastFailureReason
  };

  const recordTileDiagnostic = useCallback((diagnostic: TilePrefetchDiagnostic) => {
    setTileDiagnostics((current) => {
      const signature = diagnosticSignature(diagnostic);
      const [first, ...rest] = current;
      if (first && diagnosticSignature(first) === signature && diagnostic.recordedAt - first.recordedAt < DIAGNOSTIC_REPEAT_WINDOW_MS) {
        return [
          {
            ...first,
            ...diagnostic,
            id: first.id,
            count: first.count + 1
          },
          ...rest
        ];
      }
      return [
        {
          ...diagnostic,
          id: `${signature}:${diagnostic.recordedAt}`,
          count: 1
        },
        ...current
      ].slice(0, MAX_TILE_DIAGNOSTICS);
    });
  }, []);

  const activeIssue = useMemo(
    () =>
      selectTileIssue({
        records: tileDiagnostics,
        servingGaps: servingProfile?.seamless_rendering_gaps ?? [],
        browser: browserRenderDebug
      }),
    [browserRenderDebug, servingProfile?.seamless_rendering_gaps, tileDiagnostics]
  );
  const visibleIssue = activeIssue && activeIssue.key !== dismissedIssueKey ? activeIssue : null;

  useTilePrefetch({
    tileTemplate,
    adjacentTimeTileTemplate,
    adjacentTimePrefetchEnabled,
    viewState: debouncedViewState,
    minZoom,
    maxZoom,
    vmin,
    vmax,
    setRangeFromTileHeaders,
    onTileDiagnostic: recordTileDiagnostic,
    enabled: browserMultiscale.status !== "native"
  });

  const updateViewportBounds = useCallback(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const bounds = map.getBounds();
    const nextBounds: BBox = [
      normalizeLongitude(bounds.getWest()),
      clampLatitude(bounds.getSouth()),
      normalizeLongitude(bounds.getEast()),
      clampLatitude(bounds.getNorth())
    ];
    setViewportBounds(nextBounds);
  }, [setViewportBounds]);

  const updateDeckInsertionPoint = useCallback(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const firstSymbolLayer = map.getStyle().layers?.find((layer) => layer.type === "symbol");
    setDeckBeforeId(firstSymbolLayer?.id);
  }, []);

  const handleBrowserGpuOverlayStateChange = useCallback((nextState: BrowserGpuOverlayState) => {
    setBrowserGpuOverlayState((current) => {
      if (current && browserGpuOverlayStatesEqual(current, nextState)) {
        return current;
      }
      return nextState;
    });
  }, []);

  const handleDeckRasterError = useCallback(
    (error: unknown) => {
      if (!browserGpuAttemptKey) {
        return;
      }
      const reason = errorToMessage(error);
      recordTileDiagnostic({
        source: "browser-gpu",
        path: "browser-gpu",
        z: null,
        x: null,
        y: null,
        status: null,
        ok: false,
        errorMessage: reason,
        headers: {},
        recordedAt: Date.now()
      });
      setBrowserGpuFailure((current) => {
        if (current.key !== browserGpuAttemptKey) {
          return { key: browserGpuAttemptKey, count: 1, reason };
        }
        return {
          key: browserGpuAttemptKey,
          count: current.count + 1,
          reason
        };
      });
    },
    [browserGpuAttemptKey, recordTileDiagnostic]
  );

  const handleMapError = useCallback(
    (event: MapLibreErrorEvent) => {
      recordTileDiagnostic({
        source: "maplibre",
        path: "maplibre",
        z: null,
        x: null,
        y: null,
        status: null,
        ok: false,
        errorMessage: errorToMessage(event.error),
        headers: {},
        recordedAt: Date.now()
      });
    },
    [recordTileDiagnostic]
  );

  useEffect(() => {
    const handlePopState = () => {
      setDebugOverlayEnabled(isDebugOverlayEnabled());
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!tileJson?.bounds || !mapRef.current || !mapLoaded) {
      return;
    }

    const boundsKey = [
      datasetId,
      tileVariable,
      renderMode,
      timeIndex,
      tileJson.name,
      tileJson.bounds[0],
      tileJson.bounds[1],
      tileJson.bounds[2],
      tileJson.bounds[3],
      tileJson.minzoom ?? "none",
      tileJson.maxzoom ?? "none",
      tileJson.center?.join(",") ?? "no-center"
    ].join(":");

    if (lastFittedBoundsKeyRef.current === boundsKey) {
      return;
    }

    if (urlCameraRestored) {
      lastFittedBoundsKeyRef.current = boundsKey;
      updateViewportBounds();
      return;
    }

    lastFittedBoundsKeyRef.current = boundsKey;
    const map = mapRef.current;
    map.resize();
    const camera = map.cameraForBounds(
      [
        [tileJson.bounds[0], tileJson.bounds[1]],
        [tileJson.bounds[2], tileJson.bounds[3]]
      ],
      {
        padding: 40
      }
    );

    const cameraCenter = camera?.center ? maplibregl.LngLat.convert(camera.center) : map.getCenter();
    const fitZoom = camera?.zoom ?? map.getZoom();
    const zoomFloor = tileJson.has_coarse_fallback !== true && minZoom !== null ? minZoom : fitZoom;
    const zoomCeiling = maxZoom ?? Math.max(fitZoom, zoomFloor);
    const nextZoom = Math.min(Math.max(fitZoom, zoomFloor), zoomCeiling);
    const centerHint = tileJson.center;
    const nextCenter =
      centerHint && nextZoom > fitZoom + 0.01
        ? new maplibregl.LngLat(centerHint[0], centerHint[1])
        : cameraCenter;
    map.jumpTo({
      center: nextCenter,
      zoom: centerHint ? Math.min(Math.max(centerHint[2] ?? nextZoom, nextZoom), zoomCeiling) : nextZoom,
      bearing: 0,
      pitch: 0
    });
    updateViewportBounds();
    setViewState({
      longitude: nextCenter.lng,
      latitude: nextCenter.lat,
      zoom: centerHint ? Math.min(Math.max(centerHint[2] ?? nextZoom, nextZoom), zoomCeiling) : nextZoom,
      pitch: 0,
      bearing: 0
    });
  }, [
    datasetId,
    tileVariable,
    renderMode,
    timeIndex,
    minZoom,
    maxZoom,
    tileJson?.name,
    tileJson?.bounds,
    tileJson?.center,
    tileJson?.has_coarse_fallback,
    tileJson?.minzoom,
    urlCameraRestored,
    mapLoaded,
    setViewState,
    updateViewportBounds
  ]);

  return (
    <div
      className="map-shell"
      data-render-mode={
        browserGpuActive
          ? "browser-gpu"
          : browserMultiscale.status === "native"
            ? "browser-native"
            : "server-tiles"
      }
      data-browser-native-status={browserMultiscale.debug.status}
      data-browser-native-mode={browserMultiscale.debug.mode}
      data-browser-native-reason={browserMultiscale.debug.reason}
      data-browser-native-level={browserMultiscale.debug.levelPath ?? ""}
      data-browser-native-pixels={browserMultiscale.debug.pixelCount}
      data-browser-native-chunks={browserMultiscale.debug.chunkCount}
      data-browser-native-loaded-bytes={browserMultiscale.debug.loadedBytes}
      data-browser-native-estimated-bytes={browserMultiscale.debug.estimatedChunkBytes}
      data-browser-native-max-pixels={browserMultiscale.debug.maxPixels}
      data-browser-native-max-chunks={browserMultiscale.debug.maxChunks}
      data-browser-native-max-bytes={browserMultiscale.debug.maxChunkBytes}
      data-browser-native-max-concurrency={browserMultiscale.debug.maxConcurrentChunkLoads}
      data-browser-gpu-status={browserGpuDebug.status}
      data-browser-gpu-ready={browserGpuReady ? "true" : "false"}
      data-browser-gpu-reason={browserGpuDebug.reason}
      data-browser-gpu-level={browserGpuDebug.levelPath ?? ""}
      data-browser-gpu-mode={browserGpuDebug.mode}
      data-browser-gpu-renderer={browserGpuDebug.renderer}
      data-browser-gpu-max-texture-dimension={browserGpuDebug.maxTextureDimension}
      data-browser-gpu-failure-fallback-threshold={browserGpuDebug.failureFallbackThreshold}
      data-browser-gpu-failure-count={browserGpuDebug.failureCount}
      data-browser-gpu-last-error={browserGpuDebug.lastFailureReason ?? ""}
      data-selected-dataset-id={datasetId ?? ""}
      data-selected-variable-id={variable ?? ""}
      data-selected-render-kind={renderMode}
      data-selected-composite-style-id={compositeStyle ?? ""}
      data-selected-tile-variable-id={tileVariable ?? ""}
      data-selected-time-index={timeIndex}
      data-time-step-count={timeStepCount}
      data-time-animation-playing={timeAnimationPlaying ? "true" : "false"}
      data-adjacent-time-prefetch={adjacentTimePrefetchEnabled ? "enabled" : "disabled"}
      data-adjacent-time-index={adjacentTimePrefetchEnabled ? nextTimeIndex : ""}
      data-map-zoom={viewState.zoom.toFixed(3)}
      data-display-range-mode={rangeMode}
      data-country-borders-enabled={countryBordersEnabled ? "true" : "false"}
    >
      <Map
        mapLib={maplibregl}
        ref={(instance) => {
          mapRef.current = instance?.getMap() ?? null;
        }}
        mapStyle={BASE_STYLE}
        longitude={viewState.longitude}
        latitude={viewState.latitude}
        zoom={viewState.zoom}
        pitch={viewState.pitch}
        bearing={viewState.bearing}
        onLoad={() => {
          setMapLoaded(true);
          updateDeckInsertionPoint();
          updateViewportBounds();
        }}
        onError={handleMapError}
        onMove={(event) =>
          setViewState({
            longitude: event.viewState.longitude,
            latitude: event.viewState.latitude,
            zoom: event.viewState.zoom,
            pitch: event.viewState.pitch,
            bearing: event.viewState.bearing
          })
        }
        onMoveEnd={updateViewportBounds}
      >
        {browserGpuOverlayEligible ? (
          <Suspense fallback={null}>
            <BrowserGpuOverlay
              attemptKey={browserGpuAttemptKey}
              profile={servingProfile}
              image={browserMultiscale.image}
              browserMultiscaleStatus={browserMultiscale.status}
              browserMultiscaleReason={browserMultiscale.reason}
              enabled={renderMode === "band" || renderMode === "composite"}
              failureCount={activeBrowserGpuFailure.count}
              lastFailureReason={activeBrowserGpuFailure.reason}
              beforeId={deckBeforeId}
              onError={handleDeckRasterError}
              onStateChange={handleBrowserGpuOverlayStateChange}
            />
          </Suspense>
        ) : null}
        {tileTemplate && tileSourceId && tileLayerId && !browserRenderActive ? (
          <Source
            key={tileSourceKey}
            id={tileSourceId}
            type="raster"
            tiles={[tileTemplate]}
            tileSize={256}
            scheme="xyz"
          >
            <Layer
              key={`${tileLayerId}:${tileTemplate}:${tileJson?.minzoom ?? "none"}:${tileJson?.maxzoom ?? "none"}`}
              id={tileLayerId}
              type="raster"
              minzoom={tileJson?.minzoom}
              maxzoom={tileJson?.maxzoom}
              paint={{
                "raster-opacity": 0.85,
                "raster-fade-duration": 0
              }}
            />
          </Source>
        ) : null}
        {browserMultiscale.image?.renderKind === "single-band" && nativeSourceId && nativeLayerId ? (
          <Source
            key={`${nativeSourceId}:${browserMultiscale.image.dataUrl}:${browserMultiscale.image.levelPath}`}
            id={nativeSourceId}
            type="image"
            url={browserMultiscale.image.dataUrl}
            coordinates={browserMultiscale.image.coordinates}
          >
            <Layer
              id={nativeLayerId}
              type="raster"
              paint={{
                "raster-opacity": 0.9,
                "raster-fade-duration": 0
              }}
            />
          </Source>
        ) : null}
        {countryBordersEnabled ? (
          <Source id="country-borders" type="geojson" data={COUNTRY_BORDERS_SOURCE_URL}>
            <Layer
              id="country-borders-casing"
              type="line"
              paint={{
                "line-color": "rgba(5, 11, 18, 0.82)",
                "line-width": [
                  "interpolate",
                  ["linear"],
                  ["zoom"],
                  0,
                  1.2,
                  5,
                  2.2,
                  9,
                  3
                ],
                "line-opacity": 0.78
              }}
            />
            <Layer
              id="country-borders-line"
              type="line"
              paint={{
                "line-color": "rgba(240, 248, 255, 0.9)",
                "line-width": [
                  "interpolate",
                  ["linear"],
                  ["zoom"],
                  0,
                  0.55,
                  5,
                  1,
                  9,
                  1.4
                ],
                "line-opacity": 0.92
              }}
            />
          </Source>
        ) : null}
      </Map>
      {renderMode === "band" && selectedVariable && effectiveDisplayRange ? (
        <ColorbarLegend
          variable={selectedVariable}
          colormap={colormap}
          palette={legendPalette}
          vmin={effectiveDisplayRange.min}
          vmax={effectiveDisplayRange.max}
        />
      ) : null}
      <TileIssueBanner issue={visibleIssue} onDismiss={setDismissedIssueKey} />
      {debugOverlayEnabled ? <TileDebugOverlay records={tileDiagnostics} browser={browserRenderDebug} /> : null}
    </div>
  );
}

function ColorbarLegend({
  variable,
  colormap,
  palette,
  vmin,
  vmax
}: {
  variable: VariableMeta;
  colormap: string;
  palette: number[][] | undefined;
  vmin: number;
  vmax: number;
}) {
  const gradient = paletteToCssGradient(palette);
  return (
    <aside className="colorbar-legend" aria-label={`Colorbar legend for ${variable.name}`}>
      <div className="colorbar-legend__header">
        <span>{variable.name}</span>
        <span>{colormap}</span>
      </div>
      <div className="colorbar-legend__strip" style={{ background: gradient }} />
      <div className="colorbar-legend__labels">
        <span>{formatRangeLabel(vmin)}</span>
        <span>{variable.unit}</span>
        <span>{formatRangeLabel(vmax)}</span>
      </div>
    </aside>
  );
}

function normalizeLongitude(value: number): number {
  if (value < -180) {
    return -180;
  }
  if (value > 180) {
    return 180;
  }
  return value;
}

function clampLatitude(value: number): number {
  if (value < -85.05112878) {
    return -85.05112878;
  }
  if (value > 85.05112878) {
    return 85.05112878;
  }
  return value;
}

function buildCompositeBandInputs(
  composite: CompositeStyle | null | undefined,
  variables: VariableMeta[]
): [BrowserMultiscaleBandInput, BrowserMultiscaleBandInput, BrowserMultiscaleBandInput] | null {
  if (!composite || composite.bands.length !== 3) {
    return null;
  }
  const inputs = composite.bands.map((bandId) => {
    const variable = variables.find((item) => item.id === bandId);
    if (!variable) {
      return null;
    }
    return {
      variable: bandId,
      vmin: variable.display_vmin ?? variable.stats.p02,
      vmax: variable.display_vmax ?? variable.stats.p98
    };
  });
  const [red, green, blue] = inputs;
  return red && green && blue ? [red, green, blue] : null;
}

function isDebugOverlayEnabled(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return new URLSearchParams(window.location.search).get("debug") === "1";
}

function diagnosticSignature(diagnostic: Pick<TilePrefetchDiagnostic, "source" | "path" | "z" | "x" | "y" | "status" | "headers" | "errorMessage">): string {
  return [
    diagnostic.source,
    diagnostic.path,
    diagnostic.z ?? "z",
    diagnostic.x ?? "x",
    diagnostic.y ?? "y",
    diagnostic.status ?? "network",
    diagnostic.headers["X-Representation"] ?? "no-representation",
    diagnostic.headers["X-Tile-Budget-Status"] ?? "no-budget",
    diagnostic.errorMessage ?? "no-error"
  ].join(":");
}

function selectTileIssue({
  records,
  servingGaps,
  browser
}: {
  records: TileDebugRecord[];
  servingGaps: string[];
  browser: BrowserRenderDebug;
}): TileIssue | null {
  const recent = records.filter((record) => Date.now() - record.recordedAt <= ISSUE_LOOKBACK_MS);
  const budgetFailure = recent.find(isBudgetFailure);
  if (budgetFailure) {
    const metric = budgetFailure.headers["X-Tile-Budget-Metric"];
    const actual = budgetFailure.headers["X-Tile-Budget-Actual"];
    const limit = budgetFailure.headers["X-Tile-Budget-Limit"];
    return {
      key: "tile-budget-exceeded",
      title: "Tile render was too expensive",
      detail: metric && actual && limit
        ? `${metric} used ${actual}, limit ${limit}. Generate browse or multiscale artifacts, or zoom in before retrying.`
        : "The backend refused an expensive direct tile. Generate browse or multiscale artifacts, or zoom in before retrying.",
      tone: "bad"
    };
  }

  const authFailure = recent.find(isAuthFailure);
  if (authFailure) {
    return {
      key: "tile-auth-session",
      title: "Storage session needs attention",
      detail: "Tile requests are failing because the backend cannot authenticate to storage. Refresh the OCI session or check the API key.",
      tone: "bad"
    };
  }

  const failureCount = recent.reduce((total, record) => total + (record.ok ? 0 : record.count), 0);
  if (failureCount >= 3) {
    const latest = recent.find((record) => !record.ok);
    return {
      key: "repeated-tile-failures",
      title: "Several tiles failed to load",
      detail: latest?.errorMessage
        ? `${failureCount} recent failures. Latest: ${latest.errorMessage}`
        : `${failureCount} recent tile requests failed. The map may show gaps until the backend recovers.`,
      tone: "warn"
    };
  }

  if (browser.gpuFailureCount > 0 && browser.renderMode === "server-tiles") {
    return {
      key: "browser-gpu-fallback",
      title: "Browser GPU fell back to server tiles",
      detail: browser.gpuLastError ?? browser.gpuReason,
      tone: "warn"
    };
  }

  const missingArtifacts = servingGaps.filter((gap) =>
    ["missing_browse_overviews", "incomplete_browse_overview_coverage", "missing_multiscale_pyramid"].includes(gap)
  );
  if (browser.renderMode === "server-tiles" && missingArtifacts.length > 0) {
    return {
      key: `missing-optimized-artifacts:${missingArtifacts.join(",")}`,
      title: "Using slower server tiles",
      detail: `${missingArtifacts.map(formatGap).join("; ")}. The dataset can render, but optimized browse or multiscale artifacts are incomplete.`,
      tone: "warn"
    };
  }

  return null;
}

function isBudgetFailure(record: TileDebugRecord): boolean {
  return (
    record.status === 503 &&
    (record.headers["X-Tile-Budget-Status"] === "exceeded" ||
      record.errorMessage?.includes("direct_tile_compute_budget_exceeded") === true)
  );
}

function isAuthFailure(record: TileDebugRecord): boolean {
  if (record.status !== 401 && record.status !== 403 && record.status !== 503) {
    return false;
  }
  const message = `${record.errorMessage ?? ""} ${record.headers["X-Tile-Budget-Reason"] ?? ""}`.toLowerCase();
  return message.includes("auth") || message.includes("session") || message.includes("token") || message.includes("notauthenticated");
}

function formatGap(gap: string): string {
  const labels: Record<string, string> = {
    missing_browse_overviews: "browse overviews missing",
    incomplete_browse_overview_coverage: "browse overview coverage incomplete",
    missing_multiscale_pyramid: "multiscale pyramid missing"
  };
  return labels[gap] ?? gap.replace(/_/g, " ");
}

function buildBrowserGpuAttemptKey({
  datasetId,
  tileVariable,
  renderMode,
  timeIndex,
  colormap,
  vmin,
  vmax,
  image
}: {
  datasetId: string | null;
  tileVariable: string | null;
  renderMode: string;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  image: BrowserMultiscaleImage | null;
}): string | null {
  if (!datasetId || !tileVariable || !image) {
    return null;
  }
  const imageKey = image.renderKind === "composite"
    ? image.bands.map((band) => `${band.variable}:${band.vmin}:${band.vmax}`).join("|")
    : `${image.vmin}:${image.vmax}:${colormap}`;
  return [
    datasetId,
    tileVariable,
    renderMode,
    timeIndex,
    image.renderKind,
    image.levelPath,
    image.mode,
    image.browseZoom ?? "no-browse-zoom",
    image.pixelCount,
    image.chunkCount,
    vmin ?? "auto",
    vmax ?? "auto",
    imageKey
  ].join(":");
}

function buildInactiveBrowserGpuDebug({
  profile,
  eligible,
  failureCount,
  lastFailureReason
}: {
  profile: { browser_gpu_reason?: string | null; browser_gpu_gaps?: string[]; seamless_rendering_gaps?: string[] } | undefined;
  eligible: boolean;
  failureCount: number;
  lastFailureReason: string | null;
}): DeckZarrRasterDebug {
  let status: DeckZarrRasterDebug["status"] = eligible ? "native-loading" : "fallback";
  let reason = eligible ? "loading browser GPU renderer" : browserGpuFallbackReason(profile);
  if (failureCount >= BROWSER_GPU_FAILURE_FALLBACK_THRESHOLD) {
    status = "fallback";
    reason = `server tiles: browser GPU failed ${failureCount} time(s)${lastFailureReason ? `: ${lastFailureReason}` : ""}`;
  }
  return {
    status,
    reason,
    levelPath: null,
    mode: "none",
    renderer: "none",
    maxTextureDimension: BROWSER_GPU_MAX_TEXTURE_DIMENSION,
    failureFallbackThreshold: BROWSER_GPU_FAILURE_FALLBACK_THRESHOLD,
    failureCount,
    lastFailureReason
  };
}

function browserGpuFallbackReason(
  profile: { browser_gpu_reason?: string | null; browser_gpu_gaps?: string[]; seamless_rendering_gaps?: string[] } | undefined
): string {
  if (!profile) {
    return "serving profile unavailable";
  }
  if (profile.browser_gpu_reason) {
    return `server tiles: ${profile.browser_gpu_reason}`;
  }
  if (profile.browser_gpu_gaps && profile.browser_gpu_gaps.length > 0) {
    return `server tiles: ${profile.browser_gpu_gaps.join(", ")}`;
  }
  if (profile.seamless_rendering_gaps && profile.seamless_rendering_gaps.length > 0) {
    return `server tiles: ${profile.seamless_rendering_gaps.join(", ")}`;
  }
  return "server tiles: browser GPU sidecar not ready";
}

function browserGpuOverlayStatesEqual(current: BrowserGpuOverlayState, next: BrowserGpuOverlayState): boolean {
  return (
    current.attemptKey === next.attemptKey &&
    current.active === next.active &&
    browserGpuDebugStatesEqual(current.debug, next.debug)
  );
}

function browserGpuDebugStatesEqual(current: DeckZarrRasterDebug, next: DeckZarrRasterDebug): boolean {
  return (
    current.status === next.status &&
    current.reason === next.reason &&
    current.levelPath === next.levelPath &&
    current.mode === next.mode &&
    current.renderer === next.renderer &&
    current.maxTextureDimension === next.maxTextureDimension &&
    current.failureFallbackThreshold === next.failureFallbackThreshold &&
    current.failureCount === next.failureCount &&
    current.lastFailureReason === next.lastFailureReason
  );
}

function errorToMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message || error.name;
  }
  if (typeof error === "string") {
    return error;
  }
  return "deck.gl render error";
}
