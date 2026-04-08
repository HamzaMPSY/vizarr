import { useQuery } from "@tanstack/react-query";

import { api } from "../api/endpoints";
import type { DatasetMeta, VariableMeta } from "../types";

export function useDatasets() {
  return useQuery<DatasetMeta[]>({
    queryKey: ["datasets"],
    queryFn: api.datasets,
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

export function useColormaps() {
  return useQuery<string[]>({
    queryKey: ["colormaps"],
    queryFn: api.colormaps,
    staleTime: Infinity
  });
}
