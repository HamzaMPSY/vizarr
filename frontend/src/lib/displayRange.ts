import type { VariableMeta } from "../types";

export interface DisplayRange {
  min: number;
  max: number;
}

export type RangeDraftParseResult =
  | { ok: true; vmin: number; vmax: number }
  | { ok: false; error: string };

export function getAutoDisplayRange(variable: VariableMeta | null | undefined): DisplayRange | null {
  if (!variable) {
    return null;
  }

  const min = firstFinite(variable.display_vmin, variable.stats.p02, variable.stats.min);
  const max = firstFinite(variable.display_vmax, variable.stats.p98, variable.stats.max);
  if (min === null || max === null) {
    return null;
  }

  return normalizeRange(min, max);
}

export function getEffectiveDisplayRange(
  variable: VariableMeta | null | undefined,
  vmin: number | null,
  vmax: number | null
): DisplayRange | null {
  const auto = getAutoDisplayRange(variable);
  const min = vmin ?? auto?.min ?? null;
  const max = vmax ?? auto?.max ?? null;
  if (min === null || max === null) {
    return null;
  }
  return normalizeRange(min, max);
}

export function parseDisplayRangeDraft(vminDraft: string, vmaxDraft: string): RangeDraftParseResult {
  const minText = vminDraft.trim();
  const maxText = vmaxDraft.trim();
  if (!minText || !maxText) {
    return { ok: false, error: "Enter both min and max values." };
  }

  const vmin = Number(minText);
  const vmax = Number(maxText);
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) {
    return { ok: false, error: "Display range values must be finite numbers." };
  }
  if (vmin >= vmax) {
    return { ok: false, error: "Min must be less than max." };
  }
  return { ok: true, vmin, vmax };
}

export function formatRangeDraftValue(value: number): string {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Number(value.toPrecision(8)).toString();
}

export function formatRangeLabel(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return "auto";
  }
  const absolute = Math.abs(value);
  if (absolute !== 0 && (absolute < 0.01 || absolute >= 10000)) {
    return value.toExponential(2);
  }
  if (absolute >= 1000) {
    return value.toFixed(0);
  }
  if (absolute >= 10) {
    return value.toFixed(2);
  }
  return value.toFixed(3);
}

export function paletteToCssGradient(palette: number[][] | undefined, maxStops = 18): string {
  if (!palette || palette.length === 0) {
    return "linear-gradient(90deg, #102030 0%, #8dc7ff 100%)";
  }

  const stopCount = Math.min(maxStops, palette.length);
  const stops = Array.from({ length: stopCount }, (_, index) => {
    const paletteIndex = stopCount === 1 ? 0 : Math.round((index / (stopCount - 1)) * (palette.length - 1));
    const color = palette[paletteIndex] ?? [16, 32, 48, 255];
    const position = stopCount === 1 ? 0 : (index / (stopCount - 1)) * 100;
    return `${toCssRgba(color)} ${position.toFixed(1)}%`;
  });

  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

function firstFinite(...values: Array<number | null | undefined>): number | null {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function normalizeRange(min: number, max: number): DisplayRange {
  if (max > min) {
    return { min, max };
  }
  const delta = Math.max(Math.abs(min) * 0.01, 1);
  return { min: min - delta, max: max + delta };
}

function toCssRgba(color: number[]): string {
  const red = clampColor(color[0] ?? 0);
  const green = clampColor(color[1] ?? 0);
  const blue = clampColor(color[2] ?? 0);
  const alphaByte = clampColor(color[3] ?? 255);
  return `rgba(${red}, ${green}, ${blue}, ${(alphaByte / 255).toFixed(3)})`;
}

function clampColor(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(255, Math.max(0, Math.round(value)));
}
