import { useEffect, useRef, useState } from "react";

import maplibregl from "maplibre-gl";
import Map, { Layer, Source } from "react-map-gl/maplibre";
import type { StyleSpecification } from "maplibre-gl";

import { useBrowserMultiscale } from "../hooks/useBrowserMultiscale";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useServingProfile, useTileJson, useVariables } from "../hooks/useDatasets";
import { useTilePrefetch } from "../hooks/useTilePrefetch";
import { useMapStore } from "../store/mapStore";

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    esri: {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
      ],
      tileSize: 256,
      attribution: "Tiles © Esri"
    }
  },
  layers: [
    {
      id: "esri",
      type: "raster",
      source: "esri"
    }
  ]
};

export function MapView() {
  const { datasetId, variable, renderMode, compositeStyle, timeIndex, colormap, vmin, vmax, viewState, setViewState } = useMapStore();
  const tileVariable = renderMode === "composite" ? compositeStyle : variable;
  const { data: tileJson } = useTileJson(datasetId, tileVariable, timeIndex, colormap, vmin, vmax);
  const { data: servingProfile } = useServingProfile(datasetId);
  const { data: variables } = useVariables(datasetId);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const lastFittedBoundsKeyRef = useRef<string | null>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [tileLoading, setTileLoading] = useState(false);
  const debouncedViewState = useDebouncedValue(viewState, 180);

  const detailMinZoom = tileJson?.detail_minzoom ?? null;
  const minZoom = tileJson?.minzoom ?? null;
  const maxZoom = tileJson?.maxzoom ?? null;
  const tileTemplate = tileJson?.tiles[0] ?? null;
  const selectedVariable = variables?.find((item) => item.id === variable) ?? null;
  const nativeVmin = vmin ?? selectedVariable?.display_vmin ?? selectedVariable?.stats.p02 ?? null;
  const nativeVmax = vmax ?? selectedVariable?.display_vmax ?? selectedVariable?.stats.p98 ?? null;
  const browserMultiscale = useBrowserMultiscale({
    profile: servingProfile,
    variable: renderMode === "band" ? variable : null,
    timeIndex,
    colormap,
    vmin: nativeVmin,
    vmax: nativeVmax,
    zoom: debouncedViewState.zoom
  });
  const tileSourceId =
    datasetId && tileVariable
      ? `vizarr-tiles:${datasetId}:${tileVariable}:${timeIndex}:${colormap}:${vmin ?? "auto"}:${vmax ?? "auto"}`
      : null;
  const tileSourceKey = tileSourceId && tileTemplate ? `${tileSourceId}:${tileTemplate}` : tileSourceId;
  const tileLayerId = tileSourceId ? `${tileSourceId}:layer` : null;
  const nativeSourceId =
    datasetId && variable && renderMode === "band" && browserMultiscale.image
      ? `vizarr-native:${datasetId}:${variable}:${timeIndex}:${colormap}:${nativeVmin}:${nativeVmax}`
      : null;
  const nativeLayerId = nativeSourceId ? `${nativeSourceId}:layer` : null;

  useTilePrefetch({
    tileTemplate,
    viewState: debouncedViewState,
    minZoom,
    maxZoom,
    enabled: browserMultiscale.status !== "native"
  });

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
    map.fitBounds(
      [
        [tileJson.bounds[0], tileJson.bounds[1]],
        [tileJson.bounds[2], tileJson.bounds[3]]
      ],
      {
        padding: 40,
        duration: 0
      }
    );

    if (tileJson.has_coarse_fallback !== true && minZoom !== null && map.getZoom() < minZoom) {
      map.jumpTo({
        center: map.getCenter(),
        zoom: minZoom,
        bearing: 0,
        pitch: 0
      });
    }

    const center = map.getCenter();
    setViewState({
      longitude: center.lng,
      latitude: center.lat,
      zoom: map.getZoom(),
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
    tileJson?.has_coarse_fallback,
    tileJson?.minzoom,
    mapLoaded,
    setViewState
  ]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const onDataLoading = () => {
      if (!tileTemplate) {
        return;
      }
      if (minZoom !== null && map.getZoom() < minZoom) {
        return;
      }
      setTileLoading(true);
    };

    const onIdle = () => {
      setTileLoading(false);
    };

    map.on("dataloading", onDataLoading);
    map.on("idle", onIdle);
    map.on("error", onIdle);

    return () => {
      map.off("dataloading", onDataLoading);
      map.off("idle", onIdle);
      map.off("error", onIdle);
    };
  }, [minZoom, tileTemplate]);

  const showZoomGuard = tileJson !== undefined && minZoom !== null && viewState.zoom < minZoom;
  const showOverviewGuard =
    tileJson?.has_coarse_fallback === true
    && detailMinZoom !== null
    && viewState.zoom < detailMinZoom;

  return (
    <div className="map-shell">
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
        onLoad={() => setMapLoaded(true)}
        onMove={(event) =>
          setViewState({
            longitude: event.viewState.longitude,
            latitude: event.viewState.latitude,
            zoom: event.viewState.zoom,
            pitch: event.viewState.pitch,
            bearing: event.viewState.bearing
          })
        }
      >
        {tileTemplate && tileSourceId && tileLayerId && !browserMultiscale.image ? (
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
        {browserMultiscale.image && nativeSourceId && nativeLayerId ? (
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
      </Map>
      {!datasetId || !tileVariable ? (
        <div className="map-overlay">
          <h2>Select a dataset and layer</h2>
          <p>The POC will render synthetic Zarr-like tiles once a variable is active.</p>
        </div>
      ) : null}
      {datasetId && tileVariable && showOverviewGuard ? (
        <div className="map-overlay map-overlay--top-left">
          <h2>Overview Mode</h2>
          <p>Full-resolution tiles start at zoom {detailMinZoom}. Zoom in to switch from browse overviews to native-detail rendering.</p>
        </div>
      ) : null}
      {datasetId && tileVariable && !tileJson?.has_coarse_fallback && showZoomGuard ? (
        <div className="map-overlay map-overlay--top-left">
          <h2>Zoom In To Load Data</h2>
          <p>This dataset does not expose a useful coarse browse layer. Dynamic detail tiles start at zoom {minZoom}.</p>
        </div>
      ) : null}
      {tileLoading ? (
        <div className="map-overlay map-overlay--top-right map-overlay--compact">
          <h2>Loading Tiles</h2>
          <p>The backend is rendering on-demand tiles for this view.</p>
        </div>
      ) : null}
      {datasetId && tileVariable && renderMode === "band" ? (
        <div className="map-overlay map-overlay--bottom-right map-overlay--compact">
          <h2>{browserMultiscale.status === "native" ? "Browser Native" : "Server Tiles"}</h2>
          <p>{browserMultiscale.reason}</p>
        </div>
      ) : null}
    </div>
  );
}
