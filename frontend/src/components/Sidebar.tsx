import { useMemo } from "react";

import { buildTileUrl } from "../api/endpoints";
import { useColormaps, useDatasets, useServingProfile, useVariables } from "../hooks/useDatasets";
import { useMapStore } from "../store/mapStore";

export function Sidebar() {
  const { data: datasets, isLoading: datasetsLoading } = useDatasets();
  const {
    datasetId,
    variable,
    renderMode,
    compositeStyle,
    timeIndex,
    colormap,
    vmin,
    vmax,
    countryBordersEnabled,
    setDataset,
    setVariable,
    setRenderMode,
    setCompositeStyle,
    setTimeIndex,
    setColormap,
    setCountryBordersEnabled
  } = useMapStore();
  const { data: variables, isLoading: variablesLoading } = useVariables(datasetId);
  const { data: colormaps } = useColormaps();
  const { data: servingProfile } = useServingProfile(datasetId);

  const selectedDataset = useMemo(
    () => datasets?.find((item) => item.id === datasetId) ?? null,
    [datasetId, datasets]
  );
  const selectedVariable = useMemo(
    () => variables?.find((item) => item.id === variable) ?? null,
    [variable, variables]
  );
  const compositeStyles = selectedDataset?.composite_styles ?? [];
  const selectedComposite = useMemo(
    () => compositeStyles.find((item) => item.id === compositeStyle) ?? null,
    [compositeStyle, compositeStyles]
  );
  const tileVariable = renderMode === "composite" ? compositeStyle : variable;
  const debugTileUrl =
    datasetId && tileVariable
      ? buildTileUrl({
          datasetId,
          variable: tileVariable,
          timeIndex,
          colormap,
          vmin: renderMode === "composite" ? null : vmin,
          vmax: renderMode === "composite" ? null : vmax
        })
          .replace("{z}", "1")
          .replace("{x}", "1")
          .replace("{y}", "1")
      : null;
  const selectedTimeLabel =
    selectedDataset?.time_values && timeIndex >= 0 && timeIndex < selectedDataset.time_values.length
      ? selectedDataset.time_values[timeIndex]
      : null;
  const browseCoverage = servingProfile?.browse_coverage ?? null;
  const browseStatus = browseCoverage?.generation_status ?? "missing";
  const browseNeedsAttention =
    browseCoverage !== null && (browseStatus === "missing" || browseStatus === "partial" || browseStatus === "failed");
  const browseCoverageLabel = browseCoverage
    ? `${browseCoverage.available_artifact_count}/${browseCoverage.expected_artifact_count} artifacts`
    : "not reported";

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
          disabled={!datasetId || variablesLoading || renderMode === "composite"}
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
              Range: {(vmin ?? selectedVariable.display_vmin ?? selectedVariable.stats.p02).toFixed(2)} to{" "}
              {(vmax ?? selectedVariable.display_vmax ?? selectedVariable.stats.p98).toFixed(2)}
            </span>
          </div>
        ) : null}
      </section>

      {compositeStyles.length > 0 ? (
        <section className="panel">
          <label htmlFor="render-mode">Render Mode</label>
          <select
            id="render-mode"
            value={renderMode}
            onChange={(event) => setRenderMode(event.target.value === "composite" ? "composite" : "band")}
          >
            <option value="band">Single band</option>
            <option value="composite">RGB composite</option>
          </select>

          {renderMode === "composite" ? (
            <>
              <label htmlFor="composite-style">Composite</label>
              <select
                id="composite-style"
                value={compositeStyle ?? ""}
                onChange={(event) => setCompositeStyle(event.target.value)}
              >
                {compositeStyles.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
              {selectedComposite ? (
                <p className="muted">
                  {selectedComposite.description} Bands: {selectedComposite.bands.join(", ")}.
                </p>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}

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
        <p className="muted">
          Current time: {selectedTimeLabel ?? `index ${timeIndex}`}
        </p>
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
        {renderMode === "composite" ? (
          <p className="muted">Composite tiles use RGB channels directly; colormap applies only to single-band rendering.</p>
        ) : null}
      </section>

      <section className="panel">
        <label className="checkbox-row" htmlFor="country-borders">
          <input
            id="country-borders"
            type="checkbox"
            checked={countryBordersEnabled}
            onChange={(event) => setCountryBordersEnabled(event.target.checked)}
          />
          <span>Country borders</span>
        </label>
        <p className="muted">Natural Earth Admin-0 boundary lines rendered above the raster layer.</p>
      </section>

      <section className="panel panel--status">
        <p className="eyebrow">POC Status</p>
        <ul>
          <li>Backend tiles: live</li>
          <li>Frontend map: live</li>
          <li>Redis cache: optional</li>
          <li>Cloud Zarr access: live</li>
          <li>RGB composites: {compositeStyles.length > 0 ? "available" : "not advertised"}</li>
          <li>Browse artifacts: {browseCoverage ? `${browseStatus} (${browseCoverageLabel})` : "not reported"}</li>
        </ul>
        {browseNeedsAttention ? (
          <p className="status-note">
            Browse coverage is {browseStatus}; first-view tiles may fall back to direct serving.
          </p>
        ) : null}
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
