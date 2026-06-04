import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, buildTileUrl } from "../api/endpoints";
import { useColormaps, useDatasets, useServingProfile, useVariables } from "../hooks/useDatasets";
import type { ShareCopyStatus } from "../hooks/useShareableUrlState";
import { useTemporalAnimation } from "../hooks/useTemporalAnimation";
import {
  formatRangeDraftValue,
  formatRangeLabel,
  getAutoDisplayRange,
  getEffectiveDisplayRange,
  parseDisplayRangeDraft
} from "../lib/displayRange";
import { getNextTimeIndex, getPreviousTimeIndex, getTemporalPlaybackSafety, getTimeStepCount, getTimeStepLabel } from "../lib/temporal";
import { useMapStore } from "../store/mapStore";
import type { TimeAnimationSpeedMs } from "../store/mapStore";
import type { BrowseCoverageStatus, DatasetMeta, DatasetServingProfile } from "../types";

const TIME_ANIMATION_SPEEDS: Array<{ label: string; value: TimeAnimationSpeedMs }> = [
  { label: "Slow", value: 2000 },
  { label: "Normal", value: 1000 },
  { label: "Fast", value: 500 }
];

interface SidebarProps {
  shareWarnings: string[];
  shareCopyStatus: ShareCopyStatus;
  onCopyShareLink: () => void;
}

export function Sidebar({ shareWarnings, shareCopyStatus, onCopyShareLink }: SidebarProps) {
  const queryClient = useQueryClient();
  const {
    datasetId,
    variable,
    renderMode,
    compositeStyle,
    timeIndex,
    timeAnimationPlaying,
    timeAnimationSpeedMs,
    timeAnimationLoop,
    colormap,
    vmin,
    vmax,
    rangeMode,
    countryBordersEnabled,
    datasetViewportFilterEnabled,
    viewportBounds,
    setDataset,
    setVariable,
    setRenderMode,
    setCompositeStyle,
    setTimeIndex,
    setTimeAnimationPlaying,
    setTimeAnimationSpeedMs,
    setTimeAnimationLoop,
    setColormap,
    setRange,
    setCountryBordersEnabled,
    setDatasetViewportFilterEnabled
  } = useMapStore();
  const [rangeDraft, setRangeDraft] = useState({ vmin: "", vmax: "" });
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [datasetSearch, setDatasetSearch] = useState("");
  const datasetFilterBbox = datasetViewportFilterEnabled ? viewportBounds : null;
  const { data: datasets, isLoading: datasetsLoading } = useDatasets(datasetFilterBbox);
  const { data: allDatasets } = useDatasets();
  const { data: variables, isLoading: variablesLoading } = useVariables(datasetId);
  const { data: colormaps } = useColormaps();
  const {
    data: servingProfile,
    isLoading: servingProfileLoading,
    isError: servingProfileIsError,
    error: servingProfileError
  } = useServingProfile(datasetId);
  const browseGeneration = useMutation({
    mutationFn: () => api.createBrowseGeneration(datasetId ?? ""),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["serving-profile", datasetId] });
    }
  });
  const rangeStats = useMutation({
    mutationFn: () =>
      api.rangeStats({
        datasetId: datasetId ?? "",
        variable: variable ?? "",
        timeIndex,
        bbox: viewportBounds,
        bins: 32,
        maxWidth: 128,
        maxHeight: 128
      }),
    onSuccess: (payload) => {
      if (
        typeof payload.p02 === "number" &&
        typeof payload.p98 === "number" &&
        Number.isFinite(payload.p02) &&
        Number.isFinite(payload.p98) &&
        payload.p02 < payload.p98
      ) {
        setRange(payload.p02, payload.p98, "manual");
        setRangeDraft({
          vmin: formatRangeDraftValue(payload.p02),
          vmax: formatRangeDraftValue(payload.p98)
        });
        setRangeError(null);
        return;
      }
      setRangeError("The selected view did not return enough valid values.");
    }
  });

  const selectedDataset = useMemo(
    () => allDatasets?.find((item) => item.id === datasetId) ?? datasets?.find((item) => item.id === datasetId) ?? null,
    [allDatasets, datasetId, datasets]
  );
  const datasetOptions = useMemo(
    () =>
      selectedDataset && datasets?.every((item) => item.id !== selectedDataset.id)
        ? [selectedDataset, ...datasets]
        : datasets,
    [datasets, selectedDataset]
  );
  const filteredDatasetOptions = useMemo(
    () => filterDatasets(datasetOptions ?? [], datasetSearch, datasetId, servingProfile ?? null),
    [datasetId, datasetOptions, datasetSearch, servingProfile]
  );
  const visibleDatasetOptions = useMemo(
    () =>
      selectedDataset && filteredDatasetOptions.every((item) => item.id !== selectedDataset.id)
        ? [selectedDataset, ...filteredDatasetOptions]
        : filteredDatasetOptions,
    [filteredDatasetOptions, selectedDataset]
  );
  const datasetSearchActive = normalizeSearch(datasetSearch).length > 0;
  const datasetOptionCount = datasetOptions?.length ?? 0;
  const selectedVariable = useMemo(
    () => variables?.find((item) => item.id === variable) ?? null,
    [variable, variables]
  );
  const compositeStyles = selectedDataset?.composite_styles ?? [];
  const selectedComposite = useMemo(
    () => compositeStyles.find((item) => item.id === compositeStyle) ?? null,
    [compositeStyle, compositeStyles]
  );
  const timeStepCount = getTimeStepCount({
    dataset: selectedDataset,
    variable: selectedVariable,
    variables: variables ?? [],
    renderMode,
    composite: selectedComposite
  });
  const selectedTimeLabel = getTimeStepLabel(selectedDataset, timeIndex);
  const temporalSafety = getTemporalPlaybackSafety({
    dataset: selectedDataset,
    profile: servingProfile ?? null,
    renderMode,
    variable: selectedVariable,
    composite: selectedComposite,
    timeStepCount
  });
  useTemporalAnimation({ timeStepCount, canPlay: temporalSafety.canPlay });
  useEffect(() => {
    if (timeAnimationPlaying && !temporalSafety.canPlay) {
      setTimeAnimationPlaying(false);
    }
  }, [setTimeAnimationPlaying, temporalSafety.canPlay, timeAnimationPlaying]);
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
  const canStepTime = timeStepCount > 1;
  const canTogglePlayback = temporalSafety.canPlay || timeAnimationPlaying;
  const autoRange = useMemo(() => getAutoDisplayRange(selectedVariable), [selectedVariable]);
  const effectiveRange = useMemo(
    () => getEffectiveDisplayRange(selectedVariable, vmin, vmax),
    [selectedVariable, vmax, vmin]
  );
  const hasManualRange = rangeMode === "manual";
  const rangeModeLabel = rangeMode === "manual" ? "Manual" : rangeMode === "seeded" ? "Seeded" : "Auto";
  const hasPercentileRange =
    selectedVariable !== null &&
    Number.isFinite(selectedVariable.stats.p02) &&
    Number.isFinite(selectedVariable.stats.p98) &&
    selectedVariable.stats.p02 < selectedVariable.stats.p98;
  const hasFullRange =
    selectedVariable !== null &&
    Number.isFinite(selectedVariable.stats.min) &&
    Number.isFinite(selectedVariable.stats.max) &&
    selectedVariable.stats.min < selectedVariable.stats.max;

  useEffect(() => {
    if (renderMode === "composite" || !effectiveRange) {
      setRangeDraft({ vmin: "", vmax: "" });
      setRangeError(null);
      return;
    }

    setRangeDraft({
      vmin: formatRangeDraftValue(effectiveRange.min),
      vmax: formatRangeDraftValue(effectiveRange.max)
    });
    setRangeError(null);
  }, [effectiveRange?.max, effectiveRange?.min, renderMode, selectedVariable?.id]);

  const updateRangeDraft = (field: "vmin" | "vmax", value: string) => {
    setRangeDraft((current) => ({ ...current, [field]: value }));
    if (rangeError) {
      setRangeError(null);
    }
  };

  const applyRangeDraft = () => {
    const parsed = parseDisplayRangeDraft(rangeDraft.vmin, rangeDraft.vmax);
    if (!parsed.ok) {
      setRangeError(parsed.error);
      return;
    }

    setRange(parsed.vmin, parsed.vmax, "manual");
    setRangeError(null);
  };

  const resetRange = () => {
    setRange(null, null, "auto");
    if (autoRange) {
      setRangeDraft({
        vmin: formatRangeDraftValue(autoRange.min),
        vmax: formatRangeDraftValue(autoRange.max)
      });
    }
    setRangeError(null);
  };

  const applyPercentileRange = () => {
    if (!selectedVariable || !hasPercentileRange) {
      return;
    }
    setRange(selectedVariable.stats.p02, selectedVariable.stats.p98, "manual");
    setRangeDraft({
      vmin: formatRangeDraftValue(selectedVariable.stats.p02),
      vmax: formatRangeDraftValue(selectedVariable.stats.p98)
    });
    setRangeError(null);
  };

  const applyFullRange = () => {
    if (!selectedVariable || !hasFullRange) {
      return;
    }
    setRange(selectedVariable.stats.min, selectedVariable.stats.max, "manual");
    setRangeDraft({
      vmin: formatRangeDraftValue(selectedVariable.stats.min),
      vmax: formatRangeDraftValue(selectedVariable.stats.max)
    });
    setRangeError(null);
  };

  return (
    <aside className="sidebar">
      <div className="sidebar__header">
        <p className="eyebrow">Vizarr</p>
        <h1>Satellite Zarr Viewer POC</h1>
        <p className="muted">
          First runnable implementation from the docs. Synthetic data for now, full object-store path later.
        </p>
        <div className="share-view">
          <button type="button" onClick={onCopyShareLink} aria-live="polite">
            {shareCopyStatus === "copied" ? "Copied" : shareCopyStatus === "failed" ? "Copy failed" : "Copy link"}
          </button>
        </div>
        {shareWarnings.length > 0 ? (
          <div className="share-warning" role="status" aria-live="polite">
            <p>Shared view adjusted</p>
            <ul>
              {shareWarnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>

      <section className="panel">
        <label htmlFor="dataset-search">Search datasets</label>
        <input
          id="dataset-search"
          type="search"
          value={datasetSearch}
          onChange={(event) => setDatasetSearch(event.target.value)}
          placeholder="Search name, CRS, date, variable"
          autoComplete="off"
          aria-describedby="dataset-search-help"
          disabled={datasetsLoading && datasetOptionCount === 0}
        />
        <p className="dataset-filter-summary" id="dataset-search-help" aria-live="polite">
          {datasetSearchActive
            ? `${filteredDatasetOptions.length} of ${datasetOptionCount} dataset${datasetOptionCount === 1 ? "" : "s"} match`
            : datasetViewportFilterEnabled && viewportBounds
              ? `${datasetOptionCount} dataset${datasetOptionCount === 1 ? "" : "s"} in view`
              : `${datasetOptionCount} dataset${datasetOptionCount === 1 ? "" : "s"} available`}
        </p>
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
          {datasetSearchActive && filteredDatasetOptions.length === 0 ? (
            <option value="__no_dataset_matches" disabled>
              No matching datasets
            </option>
          ) : null}
          {visibleDatasetOptions.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <label className="checkbox-row" htmlFor="dataset-viewport-filter">
          <input
            id="dataset-viewport-filter"
            type="checkbox"
            checked={datasetViewportFilterEnabled}
            disabled={!viewportBounds}
            onChange={(event) => setDatasetViewportFilterEnabled(event.target.checked)}
          />
          <span>Datasets in view</span>
        </label>
        {datasetViewportFilterEnabled ? (
          <p className="muted">
            {viewportBounds ? `${datasets?.length ?? 0} intersecting datasets` : "Move the map to set a viewport"}
          </p>
        ) : null}
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
              Display: {formatRangeLabel(effectiveRange?.min)} to {formatRangeLabel(effectiveRange?.max)}
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
        <div className="timeline-header">
          <label htmlFor="time-index">Time</label>
          <span>{timeIndex + 1} / {timeStepCount}</span>
        </div>
        <p className="timeline-current">{selectedTimeLabel}</p>
        <input
          id="time-index"
          type="range"
          min={0}
          max={Math.max(timeStepCount - 1, 0)}
          value={timeIndex}
          onChange={(event) => setTimeIndex(Number(event.target.value))}
          disabled={!selectedDataset || !canStepTime}
        />
        <div className="timeline-controls" aria-label="Time animation controls">
          <button
            type="button"
            onClick={() => setTimeIndex(getPreviousTimeIndex(timeIndex, timeStepCount))}
            disabled={!canStepTime}
          >
            Prev
          </button>
          <button
            type="button"
            className="timeline-play"
            onClick={() => setTimeAnimationPlaying(!timeAnimationPlaying)}
            disabled={!canTogglePlayback}
            aria-pressed={timeAnimationPlaying}
          >
            {timeAnimationPlaying ? "Pause" : "Play"}
          </button>
          <button
            type="button"
            onClick={() => setTimeIndex(getNextTimeIndex(timeIndex, timeStepCount))}
            disabled={!canStepTime}
          >
            Next
          </button>
        </div>
        <div className="timeline-options">
          <div className="timeline-speed">
            <label htmlFor="time-animation-speed">Speed</label>
            <select
              id="time-animation-speed"
              value={String(timeAnimationSpeedMs)}
              onChange={(event) => setTimeAnimationSpeedMs(Number(event.target.value) as TimeAnimationSpeedMs)}
              disabled={!canStepTime}
            >
              {TIME_ANIMATION_SPEEDS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <label className="checkbox-row timeline-loop" htmlFor="time-animation-loop">
            <input
              id="time-animation-loop"
              type="checkbox"
              checked={timeAnimationLoop}
              onChange={(event) => setTimeAnimationLoop(event.target.checked)}
              disabled={!canStepTime}
            />
            <span>Loop</span>
          </label>
        </div>
        {temporalSafety.reason ? <p className="timeline-warning">{temporalSafety.reason}</p> : null}
      </section>

      <section className="panel">
        <label htmlFor="colormap">Colormap</label>
        <select
          id="colormap"
          value={colormap}
          onChange={(event) => setColormap(event.target.value)}
          disabled={renderMode === "composite"}
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

      <section className="panel panel--range" aria-labelledby="display-range-title">
        <div className="range-panel-header">
          <p className="range-panel-title" id="display-range-title">Display range</p>
          <span className="range-mode">{rangeModeLabel}</span>
        </div>

        {renderMode === "composite" ? (
          <p className="muted">RGB composites use their band stretches. Scalar range controls are available in single-band mode.</p>
        ) : selectedVariable && effectiveRange ? (
          <>
            <div className="range-fields">
              <label htmlFor="display-vmin">
                Min
                <input
                  id="display-vmin"
                  type="number"
                  inputMode="decimal"
                  step="any"
                  value={rangeDraft.vmin}
                  onChange={(event) => updateRangeDraft("vmin", event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      applyRangeDraft();
                    }
                  }}
                />
              </label>
              <label htmlFor="display-vmax">
                Max
                <input
                  id="display-vmax"
                  type="number"
                  inputMode="decimal"
                  step="any"
                  value={rangeDraft.vmax}
                  onChange={(event) => updateRangeDraft("vmax", event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      applyRangeDraft();
                    }
                  }}
                />
              </label>
            </div>

            {rangeError ? <p className="range-error" role="alert">{rangeError}</p> : null}

            <div className="range-actions">
              <button type="button" onClick={applyRangeDraft}>Apply</button>
              <button type="button" className="button-secondary" onClick={resetRange}>Reset auto</button>
            </div>

            <div className="range-presets" aria-label="Dataset range presets">
              <button type="button" onClick={applyPercentileRange} disabled={!hasPercentileRange}>
                Dataset P2-P98
              </button>
              <button type="button" onClick={applyFullRange} disabled={!hasFullRange}>
                Full
              </button>
              <button
                type="button"
                onClick={() => rangeStats.mutate()}
                disabled={!datasetId || !variable || !viewportBounds || rangeStats.isPending}
              >
                {rangeStats.isPending ? "Sampling..." : "View P2-P98"}
              </button>
            </div>

            <p className="range-metadata">
              p02-p98 {formatRangeLabel(selectedVariable.stats.p02)} to {formatRangeLabel(selectedVariable.stats.p98)};
              full {formatRangeLabel(selectedVariable.stats.min)} to {formatRangeLabel(selectedVariable.stats.max)}
            </p>
            {rangeStats.data ? (
              <div className="range-histogram" aria-label="Active view histogram">
                <div className="range-histogram-bars">
                  {rangeStats.data.histogram_counts.map((count, index) => (
                    <span
                      key={`${rangeStats.data?.stats_source ?? "range"}-${index}`}
                      style={{ height: `${histogramBarHeight(count, rangeStats.data?.histogram_counts ?? [])}%` }}
                    />
                  ))}
                </div>
                <p>
                  {rangeStats.data.stats_source === "sampled_bbox" ? "View sample" : "Dataset metadata"}:{" "}
                  {rangeStats.data.valid_count} valid values
                </p>
              </div>
            ) : null}
            {rangeStats.error instanceof Error ? <p className="range-error" role="alert">{rangeStats.error.message}</p> : null}
          </>
        ) : (
          <p className="muted">Select a single-band variable to edit its scalar display range.</p>
        )}
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

      <ReadinessPanel
        dataset={selectedDataset}
        profile={servingProfile ?? null}
        isLoading={servingProfileLoading}
        isError={servingProfileIsError}
        error={servingProfileError}
        browseGenerationStatus={
          browseGeneration.data
            ? `Job ${browseGeneration.data.job_id} ${browseGeneration.data.status}; ${browseGeneration.data.total_artifacts} artifacts planned`
            : browseGeneration.error instanceof Error
              ? browseGeneration.error.message
              : null
        }
        isBrowseGenerationPending={browseGeneration.isPending}
        onStartBrowseGeneration={() => browseGeneration.mutate()}
      />

      {debugTileUrl ? (
        <section className="panel">
          <label>Tile Preview</label>
          <img className="tile-preview" src={debugTileUrl} alt="Map tile preview" />
          <p className="muted">Visual WebP tile. Use readback or export APIs for source values.</p>
          <p className="muted tile-url">{debugTileUrl}</p>
        </section>
      ) : null}
    </aside>
  );
}

function filterDatasets(
  datasets: DatasetMeta[],
  query: string,
  selectedDatasetId: string | null,
  selectedProfile: DatasetServingProfile | null
): DatasetMeta[] {
  const tokens = normalizeSearch(query).split(" ").filter(Boolean);
  if (tokens.length === 0) {
    return datasets;
  }
  return datasets.filter((dataset) => {
    const profile = dataset.id === selectedDatasetId ? selectedProfile : null;
    const haystack = datasetSearchText(dataset, profile);
    return tokens.every((token) => haystack.includes(token));
  });
}

function datasetSearchText(dataset: DatasetMeta, profile: DatasetServingProfile | null): string {
  const parts = [
    dataset.id,
    dataset.name,
    dataset.description,
    dataset.crs_authority,
    dataset.crs_wkt,
    dataset.time_values?.join(" "),
    dataset.zarr_format ? `zarr v${dataset.zarr_format}` : null,
    dataset.zarr_consolidated === true ? "zarr consolidated" : dataset.zarr_consolidated === false ? "zarr unconsolidated" : null,
    dataset.zarr_proxy_root,
    dataset.multiscale_store_path,
    dataset.multiscale_zarr_format ? `multiscale zarr v${dataset.multiscale_zarr_format}` : null,
    dataset.multiscale_zarr_consolidated === true
      ? "multiscale consolidated"
      : dataset.multiscale_zarr_consolidated === false
        ? "multiscale unconsolidated"
        : null,
    dataset.multiscale_proxy_root,
    dataset.multiscale_population_strategy,
    dataset.multiscale_prepopulated_zoom_max ? `prepopulated z${dataset.multiscale_prepopulated_zoom_max}` : null,
    dataset.multiscale_max_zoom ? `max z${dataset.multiscale_max_zoom}` : null,
    dataset.native_resolution_m ? `${dataset.native_resolution_m} meter resolution` : null,
    dataset.bounds ? formatBounds(dataset.bounds) : null
  ].filter((item): item is string => typeof item === "string" && item.length > 0);

  dataset.variables.forEach((variable) => {
    parts.push(
      variable.id,
      variable.name,
      variable.unit,
      `${variable.time_steps} time steps`,
      variable.default_colormap ?? "",
      `range ${variable.stats.min} ${variable.stats.max} p02 ${variable.stats.p02} p98 ${variable.stats.p98}`
    );
  });

  dataset.composite_styles.forEach((style) => {
    parts.push(style.id, style.name, style.description, style.bands.join(" "));
  });

  if (profile) {
    parts.push(
      profile.seamless_rendering_ready ? "smooth mode ready optimized" : "renderable slow fallback",
      profile.browser_multiscale_ready ? "browser native eligible" : "browser native fallback",
      profile.browser_gpu_ready ? "browser gpu eligible ready" : "browser gpu fallback",
      profile.browser_gpu_reason ?? "",
      profile.supported_rendering_modes.join(" "),
      profile.seamless_rendering_gaps.map(formatGap).join(" "),
      profile.browser_gpu_gaps?.map(formatGap).join(" ") ?? "",
      profile.browse_coverage.generation_status,
      profile.browse_coverage.available_zoom_levels.map((zoom) => `browse z${zoom}`).join(" "),
      profile.has_multiscale ? "multiscale sidecar present" : "multiscale sidecar missing",
      profile.chunk_layout?.sharded ? "sharded chunks" : "regular chunks"
    );
  }

  return normalizeSearch(parts.join(" "));
}

function normalizeSearch(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9.+-]+/g, " ").trim();
}

function histogramBarHeight(count: number, counts: number[]): number {
  const max = Math.max(1, ...counts);
  if (!Number.isFinite(count) || count <= 0) {
    return 4;
  }
  return Math.max(6, Math.round((count / max) * 100));
}

type ReadinessTone = "good" | "warn" | "bad" | "neutral";

interface ReadinessRow {
  label: string;
  state: string;
  detail: string;
  tone: ReadinessTone;
}

interface ReadinessPanelProps {
  dataset: DatasetMeta | null;
  profile: DatasetServingProfile | null;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  browseGenerationStatus: string | null;
  isBrowseGenerationPending: boolean;
  onStartBrowseGeneration: () => void;
}

function ReadinessPanel({
  dataset,
  profile,
  isLoading,
  isError,
  error,
  browseGenerationStatus,
  isBrowseGenerationPending,
  onStartBrowseGeneration
}: ReadinessPanelProps) {
  const summary = readinessSummary({ dataset, profile, isLoading, isError, error });
  const rows = profile ? readinessRows(dataset, profile) : [];
  const visibleGaps = profile ? profile.seamless_rendering_gaps.map(formatGap).slice(0, 4) : [];
  const canStartBrowseGeneration =
    Boolean(dataset && profile) && ["missing", "partial", "failed"].includes(profile?.browse_coverage.generation_status ?? "");

  return (
    <section className="panel panel--readiness" aria-labelledby="dataset-readiness-title">
      <div className="readiness-header">
        <p className="eyebrow" id="dataset-readiness-title">Readiness</p>
        <span className={`readiness-pill readiness-pill--${summary.tone}`}>{summary.label}</span>
      </div>
      <p className="readiness-summary">{summary.detail}</p>

      {rows.length > 0 ? (
        <dl className="readiness-list">
          {rows.map((row) => (
            <div className="readiness-row" key={row.label}>
              <dt>{row.label}</dt>
              <dd>
                <span className={`readiness-state readiness-state--${row.tone}`}>{row.state}</span>
                <span>{row.detail}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {visibleGaps.length > 0 ? (
        <div className="readiness-gaps">
          <span>Blocking gaps</span>
          <ul>
            {visibleGaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {canStartBrowseGeneration ? (
        <div className="readiness-action">
          <button type="button" onClick={onStartBrowseGeneration} disabled={isBrowseGenerationPending}>
            {isBrowseGenerationPending ? "Starting..." : "Start browse job"}
          </button>
          {browseGenerationStatus ? <p aria-live="polite">{browseGenerationStatus}</p> : null}
        </div>
      ) : browseGenerationStatus ? (
        <p className="readiness-action-note" aria-live="polite">{browseGenerationStatus}</p>
      ) : null}
    </section>
  );
}

function readinessSummary({
  dataset,
  profile,
  isLoading,
  isError,
  error
}: {
  dataset: DatasetMeta | null;
  profile: DatasetServingProfile | null;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
}): { label: string; detail: string; tone: ReadinessTone } {
  if (!dataset) {
    return { label: "No dataset", detail: "Select a dataset to check rendering readiness.", tone: "neutral" };
  }
  if (isLoading) {
    return { label: "Checking", detail: "Loading serving profile.", tone: "neutral" };
  }
  if (isError) {
    const message = error instanceof Error ? error.message : "Serving profile request failed";
    const unauthorized = message.includes("401") || message.includes("403");
    return {
      label: unauthorized ? "Unauthorized" : "Unavailable",
      detail: unauthorized ? "This key cannot read the serving profile." : message,
      tone: "bad"
    };
  }
  if (!profile) {
    return { label: "Not reported", detail: "Serving profile has not been returned yet.", tone: "neutral" };
  }
  if (profile.seamless_rendering_ready && profile.browser_gpu_ready) {
    return { label: "Optimized", detail: "Browse and browser-GPU paths are ready.", tone: "good" };
  }
  if (profile.seamless_rendering_ready) {
    return { label: "Optimized", detail: "Fast browse or browser-native paths are ready.", tone: "good" };
  }
  if (hasUnsupportedGap(profile.seamless_rendering_gaps)) {
    return { label: "Unsupported", detail: "Core source metadata is incomplete for reliable rendering.", tone: "bad" };
  }
  return { label: "Renderable but slow", detail: "Server tiles can render, but optimized artifacts are incomplete.", tone: "warn" };
}

function readinessRows(dataset: DatasetMeta | null, profile: DatasetServingProfile): ReadinessRow[] {
  const gaps = new Set<string>(profile.seamless_rendering_gaps);
  return [
    crsBoundsRow(dataset, gaps),
    layoutRow(profile),
    browseRow(profile),
    multiscaleRow(profile),
    browserNativeRow(profile),
    browserGpuRow(profile),
    directServingRow(profile),
    {
      label: "Smooth mode",
      state: profile.seamless_rendering_ready ? "ready" : "blocked",
      detail: profile.seamless_rendering_ready
        ? "Optimized first-view path is available."
        : `${profile.seamless_rendering_gaps.length} gap${profile.seamless_rendering_gaps.length === 1 ? "" : "s"} reported.`,
      tone: profile.seamless_rendering_ready ? "good" : "warn"
    }
  ];
}

function crsBoundsRow(dataset: DatasetMeta | null, gaps: Set<string>): ReadinessRow {
  const crsReady = Boolean(dataset?.crs_authority || dataset?.crs_wkt) && !gaps.has("missing_crs_metadata");
  const boundsReady = Boolean(dataset?.bounds) && !gaps.has("missing_spatial_transform");
  const tone: ReadinessTone = crsReady && boundsReady ? "good" : crsReady || boundsReady ? "warn" : "bad";
  return {
    label: "CRS and bounds",
    state: crsReady && boundsReady ? "ready" : "incomplete",
    detail: `${dataset?.crs_authority ?? (dataset?.crs_wkt ? "CRS provided" : "CRS missing")}; ${
      dataset?.bounds ? formatBounds(dataset.bounds) : "bounds missing"
    }`,
    tone
  };
}

function layoutRow(profile: DatasetServingProfile): ReadinessRow {
  const layout = profile.chunk_layout;
  if (!layout) {
    return {
      label: "Chunk layout",
      state: "missing",
      detail: "Array metadata has not exposed chunk details.",
      tone: "bad"
    };
  }
  return {
    label: "Chunk layout",
    state: layout.sharded ? "sharded" : "regular",
    detail: `inner ${formatShape(layout.inner_chunk_shape)}; ${profile.variable_ids.length} variable${
      profile.variable_ids.length === 1 ? "" : "s"
    }`,
    tone: "good"
  };
}

function browseRow(profile: DatasetServingProfile): ReadinessRow {
  const coverage = profile.browse_coverage;
  const toneByStatus: Record<BrowseCoverageStatus, ReadinessTone> = {
    complete: "good",
    queued: "warn",
    running: "warn",
    partial: "warn",
    missing: "bad",
    failed: "bad"
  };
  return {
    label: "Browse overviews",
    state: browseStatusLabel(coverage.generation_status),
    detail: `${coverage.available_artifact_count}/${coverage.expected_artifact_count} artifacts; zooms ${
      coverage.available_zoom_levels.length ? coverage.available_zoom_levels.join(", ") : "none"
    }`,
    tone: toneByStatus[coverage.generation_status]
  };
}

function multiscaleRow(profile: DatasetServingProfile): ReadinessRow {
  if (!profile.has_multiscale) {
    return {
      label: "Multiscale sidecar",
      state: "missing",
      detail: "No browser-facing pyramid is attached.",
      tone: "bad"
    };
  }
  return {
    label: "Multiscale sidecar",
    state: profile.browser_multiscale_ready ? "browser-ready" : "present",
    detail: `${profile.multiscale_paths.length} level${profile.multiscale_paths.length === 1 ? "" : "s"}${
      profile.multiscale_max_zoom ? `; max z${profile.multiscale_max_zoom}` : ""
    }`,
    tone: profile.browser_multiscale_ready ? "good" : "warn"
  };
}

function browserNativeRow(profile: DatasetServingProfile): ReadinessRow {
  return {
    label: "Browser-native",
    state: profile.browser_multiscale_ready ? "eligible" : "server fallback",
    detail: profile.browser_multiscale_ready
      ? "Sidecar can be read by the browser."
      : firstFormattedGap(profile.seamless_rendering_gaps, "multiscale_store_not_browser_readable") ?? "No eligible sidecar.",
    tone: profile.browser_multiscale_ready ? "good" : "warn"
  };
}

function browserGpuRow(profile: DatasetServingProfile): ReadinessRow {
  const gpuGaps = profile.browser_gpu_gaps ?? [];
  return {
    label: "Browser GPU",
    state: profile.browser_gpu_ready ? "eligible" : "server fallback",
    detail: profile.browser_gpu_ready
      ? "Generated sidecar matches the GPU contract."
      : profile.browser_gpu_reason || gpuGaps.map(formatGap).slice(0, 2).join("; ") || "GPU sidecar not ready.",
    tone: profile.browser_gpu_ready ? "good" : "warn"
  };
}

function directServingRow(profile: DatasetServingProfile): ReadinessRow {
  const directReady = profile.supported_rendering_modes.includes("dynamic_tiles");
  return {
    label: "Direct serving",
    state: directReady ? "available" : "unavailable",
    detail: directReady ? "Server-rendered tiles remain the fallback path." : "No dynamic tile path advertised.",
    tone: directReady ? "warn" : "bad"
  };
}

function hasUnsupportedGap(gaps: string[]) {
  return gaps.some((gap) =>
    ["missing_data_array_metadata", "missing_dimension_metadata", "unsupported_dimension_order"].includes(gap)
  );
}

function firstFormattedGap(gaps: string[], preferredGap: string): string | null {
  const gap = gaps.find((item) => item === preferredGap) ?? gaps[0];
  return gap ? formatGap(gap) : null;
}

function browseStatusLabel(status: BrowseCoverageStatus): string {
  const labels: Record<BrowseCoverageStatus, string> = {
    complete: "complete",
    queued: "queued",
    running: "building",
    partial: "partial",
    missing: "missing",
    failed: "failed"
  };
  return labels[status];
}

function formatBounds(bounds: NonNullable<DatasetMeta["bounds"]>): string {
  return `${bounds.west.toFixed(2)}, ${bounds.south.toFixed(2)} to ${bounds.east.toFixed(2)}, ${bounds.north.toFixed(2)}`;
}

function formatShape(shape: number[] | null | undefined): string {
  return shape && shape.length > 0 ? shape.join("x") : "unknown";
}

function formatGap(gap: string): string {
  if (gap.startsWith("level:")) {
    const [, level, reason] = gap.split(":");
    return `level ${level}: ${formatGap(reason ?? "unsupported")}`;
  }
  const labels: Record<string, string> = {
    missing_data_array_metadata: "data array metadata missing",
    missing_dimension_metadata: "dimension metadata missing",
    unsupported_dimension_order: "dimension order unsupported",
    missing_crs_metadata: "CRS metadata missing",
    missing_spatial_transform: "spatial transform missing",
    missing_browser_proxy: "browser proxy missing",
    missing_multiscale_pyramid: "multiscale pyramid missing",
    multiscale_store_not_browser_readable: "multiscale store is not browser-readable",
    missing_browse_overviews: "browse overviews missing",
    incomplete_browse_overview_coverage: "browse overview coverage incomplete",
    missing_multiscale_proxy: "multiscale proxy missing",
    unsupported_multiscale_zarr_format: "multiscale Zarr format unsupported",
    missing_consolidated_metadata: "consolidated metadata missing",
    missing_multiscale_levels: "multiscale levels missing",
    missing_bounds: "level bounds missing",
    missing_browse_zoom: "browse zoom metadata missing",
    unsupported_dtype: "dtype unsupported",
    unsupported_compressor: "compressor unsupported",
    unsupported_filters: "filters unsupported",
    unsupported_order: "array order unsupported",
    unsupported_spatial_chunks: "spatial chunks unsupported",
    unsupported_temporal_or_band_chunks: "time or band chunks unsupported"
  };
  return labels[gap] ?? gap.replace(/_/g, " ");
}
