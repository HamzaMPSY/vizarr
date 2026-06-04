import { create } from "zustand";
import type { BBox } from "../types";

export interface MapViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

export type RenderMode = "band" | "composite";

export type TimeAnimationSpeedMs = 2000 | 1000 | 500;

export type DisplayRangeMode = "auto" | "seeded" | "manual";

export interface UrlHydrationPatch {
  datasetId?: string;
  variable?: string;
  renderMode?: RenderMode;
  compositeStyle?: string | null;
  timeIndex?: number;
  colormap?: string;
  vmin?: number | null;
  vmax?: number | null;
  rangeMode?: DisplayRangeMode;
  viewState?: MapViewState;
  urlCameraRestored?: boolean;
}

interface MapState {
  datasetId: string | null;
  variable: string | null;
  renderMode: RenderMode;
  compositeStyle: string | null;
  timeIndex: number;
  timeAnimationPlaying: boolean;
  timeAnimationSpeedMs: TimeAnimationSpeedMs;
  timeAnimationLoop: boolean;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  rangeMode: DisplayRangeMode;
  countryBordersEnabled: boolean;
  datasetViewportFilterEnabled: boolean;
  viewportBounds: BBox | null;
  viewState: MapViewState;
  urlCameraRestored: boolean;
  setDataset: (datasetId: string) => void;
  setVariable: (variable: string) => void;
  setRenderMode: (renderMode: RenderMode) => void;
  setCompositeStyle: (compositeStyle: string | null) => void;
  setTimeIndex: (timeIndex: number) => void;
  setTimeAnimationPlaying: (playing: boolean) => void;
  setTimeAnimationSpeedMs: (speedMs: TimeAnimationSpeedMs) => void;
  setTimeAnimationLoop: (loop: boolean) => void;
  setColormap: (colormap: string) => void;
  setRange: (vmin: number | null, vmax: number | null, mode?: DisplayRangeMode) => void;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
  setCountryBordersEnabled: (enabled: boolean) => void;
  setDatasetViewportFilterEnabled: (enabled: boolean) => void;
  setViewportBounds: (bounds: BBox | null) => void;
  setViewState: (viewState: MapViewState) => void;
  hydrateFromUrl: (patch: UrlHydrationPatch) => void;
}

export const useMapStore = create<MapState>((set) => ({
  datasetId: null,
  variable: null,
  renderMode: "band",
  compositeStyle: null,
  timeIndex: 0,
  timeAnimationPlaying: false,
  timeAnimationSpeedMs: 1000,
  timeAnimationLoop: true,
  colormap: "viridis",
  vmin: null,
  vmax: null,
  rangeMode: "auto",
  countryBordersEnabled: false,
  datasetViewportFilterEnabled: false,
  viewportBounds: null,
  viewState: {
    longitude: 0,
    latitude: 20,
    zoom: 1.8,
    pitch: 0,
    bearing: 0
  },
  urlCameraRestored: false,
  setDataset: (datasetId) =>
    set((state) => ({
      datasetId,
      variable: null,
      renderMode: "band",
      compositeStyle: null,
      timeIndex: 0,
      timeAnimationPlaying: false,
      vmin: null,
      vmax: null,
      rangeMode: "auto",
      viewState: state.viewState,
      urlCameraRestored: false
    })),
  setVariable: (variable) =>
    set({
      variable,
      timeIndex: 0,
      timeAnimationPlaying: false,
      vmin: null,
      vmax: null,
      rangeMode: "auto",
      urlCameraRestored: false
    }),
  setRenderMode: (renderMode) =>
    set({ renderMode, timeAnimationPlaying: false, vmin: null, vmax: null, rangeMode: "auto", urlCameraRestored: false }),
  setCompositeStyle: (compositeStyle) =>
    set({
      compositeStyle,
      timeIndex: 0,
      timeAnimationPlaying: false,
      vmin: null,
      vmax: null,
      rangeMode: "auto",
      urlCameraRestored: false
    }),
  setTimeIndex: (timeIndex) => set({ timeIndex }),
  setTimeAnimationPlaying: (timeAnimationPlaying) => set({ timeAnimationPlaying }),
  setTimeAnimationSpeedMs: (timeAnimationSpeedMs) => set({ timeAnimationSpeedMs }),
  setTimeAnimationLoop: (timeAnimationLoop) => set({ timeAnimationLoop }),
  setColormap: (colormap) => set({ colormap }),
  setRange: (vmin, vmax, mode) =>
    set({
      vmin,
      vmax,
      rangeMode: mode ?? (vmin === null && vmax === null ? "auto" : "manual")
    }),
  setRangeFromTileHeaders: (vmin, vmax) =>
    set((state) => {
      if (state.rangeMode === "manual" || state.vmin !== null || state.vmax !== null) {
        return state;
      }
      return { vmin, vmax, rangeMode: "seeded" };
    }),
  setCountryBordersEnabled: (countryBordersEnabled) => set({ countryBordersEnabled }),
  setDatasetViewportFilterEnabled: (datasetViewportFilterEnabled) => set({ datasetViewportFilterEnabled }),
  setViewportBounds: (viewportBounds) => set({ viewportBounds }),
  setViewState: (viewState) => set({ viewState }),
  hydrateFromUrl: (patch) =>
    set((state) => ({
      datasetId: patch.datasetId ?? state.datasetId,
      variable: patch.variable ?? state.variable,
      renderMode: patch.renderMode ?? state.renderMode,
      compositeStyle: patch.compositeStyle !== undefined ? patch.compositeStyle : state.compositeStyle,
      timeIndex: patch.timeIndex ?? state.timeIndex,
      timeAnimationPlaying: false,
      colormap: patch.colormap ?? state.colormap,
      vmin: patch.vmin !== undefined ? patch.vmin : state.vmin,
      vmax: patch.vmax !== undefined ? patch.vmax : state.vmax,
      rangeMode: patch.rangeMode ?? state.rangeMode,
      viewState: patch.viewState ?? state.viewState,
      urlCameraRestored: patch.urlCameraRestored ?? state.urlCameraRestored
    }))
}));
