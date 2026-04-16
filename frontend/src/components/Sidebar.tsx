import { useMemo } from "react";

import { buildTileUrl } from "../api/endpoints";
import { useColormaps, useDatasets, useVariables } from "../hooks/useDatasets";
import { useMapStore } from "../store/mapStore";

export function Sidebar() {
  const { data: datasets, isLoading: datasetsLoading } = useDatasets();
  const {
    datasetId,
    variable,
    timeIndex,
    colormap,
    vmin,
    vmax,
    setDataset,
    setVariable,
    setTimeIndex,
    setColormap
  } = useMapStore();
  const { data: variables, isLoading: variablesLoading } = useVariables(datasetId);
  const { data: colormaps } = useColormaps();

  const selectedDataset = useMemo(
    () => datasets?.find((item) => item.id === datasetId) ?? null,
    [datasetId, datasets]
  );
  const selectedVariable = useMemo(
    () => variables?.find((item) => item.id === variable) ?? null,
    [variable, variables]
  );
  const debugTileUrl =
    datasetId && variable
      ? buildTileUrl({
          datasetId,
          variable,
          timeIndex,
          colormap,
          vmin: null,
          vmax: null
        })
          .replace("{z}", "1")
          .replace("{x}", "1")
          .replace("{y}", "1")
      : null;

  return (
    <aside className="sidebar">
      <div className="sidebar__header">
        <p className="eyebrow">Vizarr</p>
        <h1>Satellite Zarr Viewer POC</h1>
        <p className="muted">
          First runnable implementation from the docs. Synthetic data for now, full object-store path later.
        </p>
      </div>

      <section className="panel">
        <label htmlFor="dataset">Dataset</label>
        <select
          id="dataset"
          value={datasetId ?? ""}
          onChange={(event) => setDataset(event.target.value)}
          disabled={datasetsLoading}
        >
          <option value="" disabled>
            {datasetsLoading ? "Loading datasets..." : "Select a dataset"}
          </option>
          {datasets?.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        {selectedDataset ? <p className="muted">{selectedDataset.description}</p> : null}
      </section>

      <section className="panel">
        <label htmlFor="variable">Variable</label>
        <select
          id="variable"
          value={variable ?? ""}
          onChange={(event) => setVariable(event.target.value)}
          disabled={!datasetId || variablesLoading}
        >
          <option value="" disabled>
            {!datasetId ? "Select a dataset first" : variablesLoading ? "Loading variables..." : "Select a variable"}
          </option>
          {variables?.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        {selectedVariable ? (
          <div className="stats">
            <span>Unit: {selectedVariable.unit}</span>
            <span>
              Range: {(vmin ?? selectedVariable.stats.p02).toFixed(1)} to {(vmax ?? selectedVariable.stats.p98).toFixed(1)}
            </span>
          </div>
        ) : null}
      </section>

      <section className="panel">
        <label htmlFor="time-index">Time Step</label>
        <input
          id="time-index"
          type="range"
          min={0}
          max={Math.max((selectedVariable?.time_steps ?? 1) - 1, 0)}
          value={timeIndex}
          onChange={(event) => setTimeIndex(Number(event.target.value))}
          disabled={!selectedVariable}
        />
        <p className="muted">Current time index: {timeIndex}</p>
      </section>

      <section className="panel">
        <label htmlFor="colormap">Colormap</label>
        <select
          id="colormap"
          value={colormap}
          onChange={(event) => setColormap(event.target.value)}
        >
          {colormaps?.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </section>

      <section className="panel panel--status">
        <p className="eyebrow">POC Status</p>
        <ul>
          <li>Backend tiles: live</li>
          <li>Frontend map: live</li>
          <li>Redis cache: live</li>
          <li>Cloud Zarr access: pending</li>
        </ul>
      </section>

      {debugTileUrl ? (
        <section className="panel">
          <label>Tile Preview</label>
          <img className="tile-preview" src={debugTileUrl} alt="Synthetic tile preview" />
          <p className="muted tile-url">{debugTileUrl}</p>
        </section>
      ) : null}
    </aside>
  );
}
