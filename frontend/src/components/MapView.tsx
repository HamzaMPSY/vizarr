import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import maplibregl from "maplibre-gl";
import Map, { Layer, Source } from "react-map-gl/maplibre";
import type { StyleSpecification } from "maplibre-gl";

import { DeckRasterOverlay } from "./DeckRasterOverlay";
import type { BrowserMultiscaleBandInput, BrowserMultiscaleImage } from "../hooks/useBrowserMultiscale";
import { useBrowserMultiscale } from "../hooks/useBrowserMultiscale";
import { useDeckZarrRaster } from "../hooks/useDeckZarrRaster";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useDatasets, useServingProfile, useTileJson, useVariables } from "../hooks/useDatasets";
import { useTilePrefetch } from "../hooks/useTilePrefetch";
import { useMapStore } from "../store/mapStore";
import type { CompositeStyle, VariableMeta } from "../types";

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
  const countryBordersEnabled = useMapStore((state) => state.countryBordersEnabled);
  const viewState = useMapStore((state) => state.viewState);
  const setViewState = useMapStore((state) => state.setViewState);
  const setRangeFromTileHeaders = useMapStore((state) => state.setRangeFromTileHeaders);
  const tileVariable = renderMode === "composite" ? compositeStyle : variable;
  const { data: tileJson } = useTileJson(datasetId, tileVariable, timeIndex, colormap, vmin, vmax);
  const { data: datasets } = useDatasets();
  const { data: servingProfile } = useServingProfile(datasetId);
  const { data: variables } = useVariables(datasetId);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const lastFittedBoundsKeyRef = useRef<string | null>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [deckBeforeId, setDeckBeforeId] = useState<string | undefined>(undefined);
  const [viewportBounds, setViewportBounds] = useState<[number, number, number, number] | null>(null);
  const [browserGpuFailure, setBrowserGpuFailure] = useState<BrowserGpuFailureState>({
    key: null,
    count: 0,
    reason: null
  });
  const debouncedViewState = useDebouncedValue(viewState, 180);
  const debouncedViewportBounds = useDebouncedValue(viewportBounds, 180);

  const minZoom = tileJson?.minzoom ?? null;
  const maxZoom = tileJson?.maxzoom ?? null;
  const tileTemplate = tileJson?.tiles[0] ?? null;
  const selectedDataset = datasets?.find((item) => item.id === datasetId) ?? null;
  const selectedComposite = selectedDataset?.composite_styles.find((item) => item.id === compositeStyle) ?? null;
  const selectedVariable = variables?.find((item) => item.id === variable) ?? null;
  const nativeVmin = vmin ?? selectedVariable?.display_vmin ?? selectedVariable?.stats.p02 ?? null;
  const nativeVmax = vmax ?? selectedVariable?.display_vmax ?? selectedVariable?.stats.p98 ?? null;
  const browserGpuReady = servingProfile?.browser_gpu_ready === true;
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
  const browserGpuRaster = useDeckZarrRaster({
    profile: servingProfile,
    image: browserMultiscale.image,
    browserMultiscaleStatus: browserMultiscale.status,
    browserMultiscaleReason: browserMultiscale.reason,
    enabled: renderMode === "band" || renderMode === "composite",
    failureCount: activeBrowserGpuFailure.count,
    lastFailureReason: activeBrowserGpuFailure.reason
  });
  const browserGpuActive = browserGpuRaster.active;
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

  useTilePrefetch({
    tileTemplate,
    viewState: debouncedViewState,
    minZoom,
    maxZoom,
    vmin,
    vmax,
    setRangeFromTileHeaders,
    enabled: browserMultiscale.status !== "native"
  });

  const updateViewportBounds = useCallback(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const bounds = map.getBounds();
    setViewportBounds([
      normalizeLongitude(bounds.getWest()),
      clampLatitude(bounds.getSouth()),
      normalizeLongitude(bounds.getEast()),
      clampLatitude(bounds.getNorth())
    ]);
  }, []);

  const updateDeckInsertionPoint = useCallback(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const firstSymbolLayer = map.getStyle().layers?.find((layer) => layer.type === "symbol");
    setDeckBeforeId(firstSymbolLayer?.id);
  }, []);

  const handleDeckRasterError = useCallback(
    (error: unknown) => {
      if (!browserGpuAttemptKey) {
        return;
      }
      const reason = errorToMessage(error);
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
    [browserGpuAttemptKey]
  );

  useEffect(() => {
    if (!tileJson?.bounds || !mapRef.current || !mapLoaded) {
      return;
    }

    const boundsKey = [
      datasetId,
      variable,
      timeIndex,
      colormap,
      vmin ?? "auto",
      vmax ?? "auto",
      tileJson.name,
      tileJson.bounds[0],
      tileJson.bounds[1],
      tileJson.bounds[2],
      tileJson.bounds[3],
      tileJson.minzoom ?? "none",
      tileTemplate ?? "no-tiles"
    ].join(":");

    if (lastFittedBoundsKeyRef.current === boundsKey) {
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
    variable,
    timeIndex,
    colormap,
    vmin,
    vmax,
    minZoom,
    tileTemplate,
    tileJson?.name,
    tileJson?.bounds,
    tileJson?.center,
    tileJson?.has_coarse_fallback,
    tileJson?.minzoom,
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
      data-browser-gpu-status={browserGpuRaster.debug.status}
      data-browser-gpu-ready={browserGpuReady ? "true" : "false"}
      data-browser-gpu-reason={browserGpuRaster.debug.reason}
      data-browser-gpu-level={browserGpuRaster.debug.levelPath ?? ""}
      data-browser-gpu-mode={browserGpuRaster.debug.mode}
      data-browser-gpu-renderer={browserGpuRaster.debug.renderer}
      data-browser-gpu-max-texture-dimension={browserGpuRaster.debug.maxTextureDimension}
      data-browser-gpu-failure-fallback-threshold={browserGpuRaster.debug.failureFallbackThreshold}
      data-browser-gpu-failure-count={browserGpuRaster.debug.failureCount}
      data-browser-gpu-last-error={browserGpuRaster.debug.lastFailureReason ?? ""}
      data-selected-dataset-id={datasetId ?? ""}
      data-selected-variable-id={variable ?? ""}
      data-selected-render-kind={renderMode}
      data-selected-composite-style-id={compositeStyle ?? ""}
      data-selected-tile-variable-id={tileVariable ?? ""}
      data-selected-time-index={timeIndex}
      data-map-zoom={viewState.zoom.toFixed(3)}
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
        <DeckRasterOverlay layers={browserGpuRaster.layers} beforeId={deckBeforeId} onError={handleDeckRasterError} />
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
    </div>
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

function errorToMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message || error.name;
  }
  if (typeof error === "string") {
    return error;
  }
  return "deck.gl render error";
}
