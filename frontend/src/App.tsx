import { useEffect } from "react";

import { MapView } from "./components/MapView";
import { Sidebar } from "./components/Sidebar";
import { useDatasetInvalidation } from "./hooks/useDatasetInvalidation";
import { useDatasets, useVariables } from "./hooks/useDatasets";
import { useMapStore } from "./store/mapStore";

function App() {
  const datasetSocketStatus = useDatasetInvalidation();
  const { data: datasets } = useDatasets();
  const {
    datasetId,
    variable,
    renderMode,
    compositeStyle,
    setDataset,
    setVariable,
    setCompositeStyle,
    setRange,
    setColormap,
  } = useMapStore();
  const { data: variables } = useVariables(datasetId);

  useEffect(() => {
    if (!datasetId && datasets && datasets.length > 0) {
      setDataset(datasets[0].id);
    }
  }, [datasetId, datasets, setDataset]);

  useEffect(() => {
    if (!variable && variables && variables.length > 0) {
      setVariable(variables[0].id);
    }
  }, [setVariable, variable, variables]);

  useEffect(() => {
    const selectedDataset = datasets?.find((item) => item.id === datasetId);
    const styles = selectedDataset?.composite_styles ?? [];
    if (renderMode === "composite" && !compositeStyle && styles.length > 0) {
      setCompositeStyle(styles[0].id);
    }
    if (compositeStyle && !styles.some((style) => style.id === compositeStyle)) {
      setCompositeStyle(styles[0]?.id ?? null);
    }
  }, [compositeStyle, datasetId, datasets, renderMode, setCompositeStyle]);

  useEffect(() => {
    if (!variable || !variables) {
      return;
    }

    const selectedVariable = variables.find((item) => item.id === variable);
    if (!selectedVariable) {
      return;
    }

    if (selectedVariable.display_vmin != null || selectedVariable.display_vmax != null) {
      setRange(selectedVariable.display_vmin ?? null, selectedVariable.display_vmax ?? null);
    }
    setColormap(selectedVariable.default_colormap ?? "viridis");
  }, [setColormap, setRange, variable, variables]);

  return (
    <div className="app-shell">
      <Sidebar />
      <main className="content">
        <div className="realtime-status" data-status={datasetSocketStatus}>
          <span aria-hidden="true" />
          <span>Dataset sync {datasetSocketStatus}</span>
        </div>
        <MapView />
      </main>
    </div>
  );
}

export default App;
