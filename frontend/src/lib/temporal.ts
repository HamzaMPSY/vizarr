import type { CompositeStyle, DatasetMeta, DatasetServingProfile, VariableMeta } from "../types";
import type { RenderMode } from "../store/mapStore";

export interface TemporalSelection {
  dataset: DatasetMeta | null;
  variable: VariableMeta | null;
  variables: VariableMeta[];
  renderMode: RenderMode;
  composite: CompositeStyle | null;
}

export interface TemporalPlaybackSafety {
  canPlay: boolean;
  canPrefetchAdjacent: boolean;
  reason: string | null;
}

export function getTimeStepCount({
  dataset,
  variable,
  variables,
  renderMode,
  composite
}: TemporalSelection): number {
  const datasetTimeCount = dataset?.time_values?.length ?? 0;
  if (renderMode === "composite") {
    const bandCounts =
      composite?.bands
        .map((bandId) => variables.find((item) => item.id === bandId)?.time_steps)
        .filter((value): value is number => typeof value === "number" && value > 0) ?? [];
    const compositeCount = bandCounts.length > 0 ? Math.min(...bandCounts) : 0;
    return Math.max(1, datasetTimeCount || compositeCount || 1);
  }

  return Math.max(1, variable?.time_steps ?? datasetTimeCount ?? 1);
}

export function getTimeStepLabel(dataset: DatasetMeta | null, timeIndex: number): string {
  const rawValue = dataset?.time_values?.[timeIndex];
  return rawValue && rawValue.length > 0 ? rawValue : `index ${timeIndex}`;
}

export function getPreviousTimeIndex(timeIndex: number, timeStepCount: number): number {
  if (timeStepCount <= 1) {
    return 0;
  }
  return timeIndex <= 0 ? timeStepCount - 1 : timeIndex - 1;
}

export function getNextTimeIndex(timeIndex: number, timeStepCount: number): number {
  if (timeStepCount <= 1) {
    return 0;
  }
  return timeIndex >= timeStepCount - 1 ? 0 : timeIndex + 1;
}

export function getTemporalPlaybackSafety({
  dataset,
  profile,
  renderMode,
  variable,
  composite,
  timeStepCount
}: {
  dataset: DatasetMeta | null;
  profile: DatasetServingProfile | null;
  renderMode: RenderMode;
  variable: VariableMeta | null;
  composite: CompositeStyle | null;
  timeStepCount: number;
}): TemporalPlaybackSafety {
  if (timeStepCount <= 1) {
    return {
      canPlay: false,
      canPrefetchAdjacent: false,
      reason: "Only one time step is available."
    };
  }

  if (isSyntheticDataset(dataset)) {
    return {
      canPlay: true,
      canPrefetchAdjacent: true,
      reason: null
    };
  }

  if (!profile) {
    return {
      canPlay: false,
      canPrefetchAdjacent: false,
      reason: "Waiting for serving profile."
    };
  }

  if (profile.browser_gpu_ready === true || profile.browser_multiscale_ready) {
    return {
      canPlay: true,
      canPrefetchAdjacent: true,
      reason: null
    };
  }

  const variableIds = selectedTemporalVariableIds(renderMode, variable, composite);
  if (hasBrowseCoverageForSelection(profile, variableIds)) {
    return {
      canPlay: true,
      canPrefetchAdjacent: true,
      reason: null
    };
  }

  return {
    canPlay: false,
    canPrefetchAdjacent: false,
    reason: "Animation is disabled on direct serving. Generate browse or multiscale artifacts first."
  };
}

export function canPrefetchTimeStep({
  dataset,
  profile,
  renderMode,
  variable,
  composite,
  timeIndex
}: {
  dataset: DatasetMeta | null;
  profile: DatasetServingProfile | null;
  renderMode: RenderMode;
  variable: VariableMeta | null;
  composite: CompositeStyle | null;
  timeIndex: number;
}): boolean {
  if (isSyntheticDataset(dataset)) {
    return true;
  }
  if (!profile) {
    return false;
  }
  if (profile.browser_gpu_ready === true || profile.browser_multiscale_ready) {
    return true;
  }
  return hasBrowseCoverageForTimeStep(profile, selectedTemporalVariableIds(renderMode, variable, composite), timeIndex);
}

export function buildTimeIndexedTileTemplate(tileTemplate: string | null, timeIndex: number): string | null {
  if (!tileTemplate) {
    return null;
  }
  const [path, query = ""] = tileTemplate.split("?");
  const params = new URLSearchParams(query);
  params.set("time_index", String(timeIndex));
  return `${path}?${params.toString()}`;
}

function selectedTemporalVariableIds(
  renderMode: RenderMode,
  variable: VariableMeta | null,
  composite: CompositeStyle | null
): string[] {
  if (renderMode === "composite") {
    return composite?.bands ?? [];
  }
  return variable ? [variable.id] : [];
}

function hasBrowseCoverageForSelection(profile: DatasetServingProfile, variableIds: string[]): boolean {
  const coverage = profile.browse_coverage;
  if (coverage.generation_status === "complete" && coverage.available_artifact_count > 0) {
    return true;
  }
  if (coverage.generation_status === "missing" || coverage.generation_status === "failed") {
    return false;
  }
  return variableIds.length > 0 && variableIds.every((variableId) => (coverage.missing_time_steps[variableId] ?? []).length === 0);
}

function hasBrowseCoverageForTimeStep(
  profile: DatasetServingProfile,
  variableIds: string[],
  timeIndex: number
): boolean {
  const coverage = profile.browse_coverage;
  if (coverage.generation_status === "complete" && coverage.available_artifact_count > 0) {
    return true;
  }
  if (coverage.generation_status === "missing" || coverage.generation_status === "failed") {
    return false;
  }
  return (
    variableIds.length > 0 &&
    variableIds.every((variableId) => !(coverage.missing_time_steps[variableId] ?? []).includes(timeIndex))
  );
}

function isSyntheticDataset(dataset: DatasetMeta | null): boolean {
  return Boolean(dataset && dataset.zarr_format == null && !dataset.zarr_proxy_root);
}
