import { useEffect, useRef } from "react";

import maplibregl from "maplibre-gl";
import Map, { Layer, Source } from "react-map-gl/maplibre";
import type { StyleSpecification } from "maplibre-gl";

import { buildTileUrl } from "../api/endpoints";
import { useDataset } from "../hooks/useDatasets";
import { useMapStore } from "../store/mapStore";

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors"
    }
  },
  layers: [
    {
      id: "osm",
      type: "raster",
      source: "osm"
    }
  ]
};

export function MapView() {
  const { datasetId, variable, timeIndex, colormap, vmin, vmax, viewState, setViewState } = useMapStore();
  const { data: dataset } = useDataset(datasetId);
  const mapRef = useRef<maplibregl.Map | null>(null);
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
    if (!dataset?.bounds || !mapRef.current) {
      return;
    }

    mapRef.current.fitBounds(
      [
        [dataset.bounds.west, dataset.bounds.south],
        [dataset.bounds.east, dataset.bounds.north]
      ],
      {
        padding: 40,
        duration: 0
      }
    );
  }, [dataset?.bounds]);

  return (
    <div className="map-shell">
      <Map
        key={tileSourceId ?? "vizarr-map"}
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
