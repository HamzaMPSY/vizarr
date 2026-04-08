import { useEffect } from "react";

import { MapView } from "./components/MapView";
import { Sidebar } from "./components/Sidebar";
import { useDatasets, useVariables } from "./hooks/useDatasets";
import { useMapStore } from "./store/mapStore";

function App() {
  const { data: datasets } = useDatasets();
  const { datasetId, variable, setDataset, setVariable } = useMapStore();
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

  return (
    <div className="app-shell">
      <Sidebar />
      <main className="content">
        <MapView />
      </main>
    </div>
  );
}

export default App;

