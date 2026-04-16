export interface VariableStats {
  min: number;
  max: number;
  p02: number;
  p98: number;
}

export interface VariableMeta {
  id: string;
  name: string;
  unit: string;
  time_steps: number;
  stats: VariableStats;
}

export interface DatasetBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface DatasetMeta {
  id: string;
  name: string;
  description: string;
  variables: VariableMeta[];
  bounds?: DatasetBounds | null;
  time_values?: string[] | null;
}
