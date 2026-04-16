import { useEffect, useRef, useState } from "react";

import maplibregl from "maplibre-gl";
import Map, { Layer, Source } from "react-map-gl/maplibre";
import type { StyleSpecification } from "maplibre-gl";

import { buildTileUrl } from "../api/endpoints";
import { useDataset } from "../hooks/useDatasets";
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
  const { datasetId, variable, timeIndex, colormap, vmin, vmax, viewState, setViewState } = useMapStore();
  const { data: dataset } = useDataset(datasetId);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const tileTemplate =
    datasetId && variable
      ? buildTileUrl({ datasetId, variable, timeIndex, colormap, vmin, vmax })
      : null;
  const tileSourceId =
    datasetId && variable
      ? `vizarr-tiles:${datasetId}:${variable}:${timeIndex}:${colormap}:${vmin ?? "auto"}:${vmax ?? "auto"}`
      : null;
  const tileLayerId = tileSourceId ? `${tileSourceId}:layer` : null;

  useEffect(() => {
    if (!dataset?.bounds || !mapRef.current || !mapLoaded) {
      return;
    }

    const camera = mapRef.current.cameraForBounds(
      [
        [dataset.bounds.west, dataset.bounds.south],
        [dataset.bounds.east, dataset.bounds.north]
      ],
      {
        padding: 40,
        maxZoom: 12
      }
    );

    if (!camera) {
      return;
    }

    if (!camera.center) {
      return;
    }

    const center = maplibregl.LngLat.convert(camera.center);
    setViewState({
      longitude: center.lng,
      latitude: center.lat,
      zoom: camera.zoom ?? viewState.zoom,
      pitch: 0,
      bearing: 0
    });
  }, [
    dataset?.id,
    dataset?.bounds?.west,
    dataset?.bounds?.south,
    dataset?.bounds?.east,
    dataset?.bounds?.north,
    mapLoaded,
    viewState.zoom,
    setViewState
  ]);

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
        {tileTemplate && tileSourceId && tileLayerId ? (
          <Source
            key={tileSourceId}
            id={tileSourceId}
            type="raster"
            tiles={[tileTemplate]}
            tileSize={256}
            scheme="xyz"
          >
            <Layer
              key={tileLayerId}
              id={tileLayerId}
              type="raster"
              paint={{
                "raster-opacity": 0.85,
                "raster-fade-duration": 0
              }}
            />
          </Source>
        ) : null}
      </Map>
      {!datasetId || !variable ? (
        <div className="map-overlay">
          <h2>Select a dataset and variable</h2>
          <p>The POC will render synthetic Zarr-like tiles once a variable is active.</p>
        </div>
      ) : null}
    </div>
  );
}
