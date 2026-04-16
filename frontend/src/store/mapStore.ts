import { create } from "zustand";

export interface MapViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

interface MapState {
  datasetId: string | null;
  variable: string | null;
  timeIndex: number;
  colormap: string;
  vmin: number | null;
  vmax: number | null;
  viewState: MapViewState;
  setDataset: (datasetId: string) => void;
  setVariable: (variable: string) => void;
  setTimeIndex: (timeIndex: number) => void;
  setColormap: (colormap: string) => void;
  setRange: (vmin: number | null, vmax: number | null) => void;
  setViewState: (viewState: MapViewState) => void;
}

export const useMapStore = create<MapState>((set) => ({
  datasetId: null,
  variable: null,
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
      timeIndex: 0,
      vmin: null,
      vmax: null,
      viewState:
        datasetId === "demo-global"
          ? state.viewState
          : {
              longitude: -9.5,
              latitude: 31.2,
              zoom: 7.2,
              pitch: 0,
              bearing: 0
            }
    })),
  setVariable: (variable) => set({ variable, timeIndex: 0, vmin: null, vmax: null }),
  setTimeIndex: (timeIndex) => set({ timeIndex }),
  setColormap: (colormap) => set({ colormap }),
  setRange: (vmin, vmax) => set({ vmin, vmax }),
  setViewState: (viewState) => set({ viewState })
}));
