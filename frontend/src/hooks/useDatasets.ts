import { useQuery } from "@tanstack/react-query";

import { api } from "../api/endpoints";
import type { BBox, DatasetMeta, DatasetServingProfile, TileJson, VariableMeta } from "../types";

export function useDatasets(bbox: BBox | null = null) {
  return useQuery<DatasetMeta[]>({
    queryKey: ["datasets", bbox ? bbox.map((value) => value.toFixed(5)).join(",") : "all"],
    queryFn: () => api.datasets({ bbox }),
    staleTime: 30_000
  });
}

export function useDataset(datasetId: string | null) {
  return useQuery<DatasetMeta>({
    queryKey: ["dataset", datasetId],
    queryFn: () => api.dataset(datasetId ?? ""),
    enabled: datasetId !== null,
    staleTime: 30_000
  });
}

export function useVariables(datasetId: string | null) {
  return useQuery<VariableMeta[]>({
    queryKey: ["variables", datasetId],
    queryFn: () => api.variables(datasetId ?? ""),
    enabled: datasetId !== null,
    staleTime: 30_000
  });
}

export function useServingProfile(datasetId: string | null) {
  return useQuery<DatasetServingProfile>({
    queryKey: ["serving-profile", datasetId],
    queryFn: () => api.servingProfile(datasetId ?? ""),
    enabled: datasetId !== null,
    staleTime: 30_000
  });
}

export function useTileJson(
  datasetId: string | null,
  variable: string | null,
  timeIndex: number,
  colormap: string,
  vmin: number | null,
  vmax: number | null,
) {
  return useQuery<TileJson>({
    queryKey: ["tilejson", datasetId, variable, timeIndex, colormap, vmin, vmax],
    queryFn: () =>
      api.tilejson({
        datasetId: datasetId ?? "",
        variable: variable ?? "",
        timeIndex,
        colormap,
        vmin,
        vmax,
      }),
    enabled: datasetId !== null && variable !== null,
    staleTime: 0,
  });
}

export function useColormaps() {
  return useQuery<string[]>({
    queryKey: ["colormaps"],
    queryFn: api.colormaps,
    staleTime: Infinity
  });
}

export function useColormapPalette(name: string | null, samples = 256) {
  return useQuery<number[][]>({
    queryKey: ["colormap-palette", name, samples],
    queryFn: () => api.colormapPalette(name ?? "", samples),
    enabled: name !== null,
    staleTime: Infinity
  });
}
