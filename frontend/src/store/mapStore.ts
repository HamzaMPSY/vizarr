import { create } from "zustand";

export interface MapViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

export type RenderMode = "band" | "composite";

interface MapState {
  datasetId: string | null;
  variable: string | null;
  renderMode: RenderMode;
  compositeStyle: string | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  viewState: MapViewState;
  setDataset: (datasetId: string) => void;
  setVariable: (variable: string) => void;
  setRenderMode: (renderMode: RenderMode) => void;
  setCompositeStyle: (compositeStyle: string | null) => void;
  setTimeIndex: (timeIndex: number) => void;
  setColormap: (colormap: string) => void;
  setRange: (vmin: number | null, vmax: number | null) => void;
  setRangeFromTileHeaders: (vmin: number, vmax: number) => void;
  setViewState: (viewState: MapViewState) => void;
}

export const useMapStore = create<MapState>((set) => ({
  datasetId: null,
  variable: null,
  renderMode: "band",
  compositeStyle: null,
  timeIndex: 0,
  colormap: "viridis",
  vmin: null,
  vmax: null,
  viewState: {
    longitude: 0,
    latitude: 20,
    zoom: 1.8,
    pitch: 0,
    bearing: 0
  },
  setDataset: (datasetId) =>
    set((state) => ({
      datasetId,
      variable: null,
      renderMode: "band",
      compositeStyle: null,
      timeIndex: 0,
      vmin: null,
      vmax: null,
      viewState: state.viewState
    })),
  setVariable: (variable) => set({ variable, timeIndex: 0, vmin: null, vmax: null }),
  setRenderMode: (renderMode) => set({ renderMode, vmin: null, vmax: null }),
  setCompositeStyle: (compositeStyle) => set({ compositeStyle, timeIndex: 0, vmin: null, vmax: null }),
  setTimeIndex: (timeIndex) => set({ timeIndex }),
  setColormap: (colormap) => set({ colormap }),
  setRange: (vmin, vmax) => set({ vmin, vmax }),
  setRangeFromTileHeaders: (vmin, vmax) =>
    set((state) => {
      if (state.vmin !== null || state.vmax !== null) {
        return state;
      }
      return { vmin, vmax };
    }),
  setViewState: (viewState) => set({ viewState })
}));
