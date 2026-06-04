import { useEffect, useMemo, useRef, useState } from "react";

import { MapView } from "./components/MapView";
import { Sidebar } from "./components/Sidebar";
import { TileIssueBanner } from "./components/TileDebugOverlay";
import type { TileIssue } from "./components/TileDebugOverlay";
import { isOciAuthApiError } from "./api/endpoints";
import { useDatasetInvalidation } from "./hooks/useDatasetInvalidation";
import { useDatasets, useVariables } from "./hooks/useDatasets";
import { useShareableUrlState } from "./hooks/useShareableUrlState";
import { useMapStore } from "./store/mapStore";

function App() {
  useDatasetInvalidation();
  const datasetsQuery = useDatasets();
  const { data: datasets } = datasetsQuery;
  const {
    datasetId,
    variable,
    renderMode,
    compositeStyle,
    setDataset,
    setVariable,
    setRenderMode,
    setCompositeStyle,
    setColormap,
  } = useMapStore();
  const variablesQuery = useVariables(datasetId);
  const { data: variables } = variablesQuery;
  const shareUrl = useShareableUrlState({ datasets, variables });
  const [dismissedIssueKey, setDismissedIssueKey] = useState<string | null>(null);
  const skipInitialUrlColormapRef = useRef(
    typeof window !== "undefined" && new URLSearchParams(window.location.search).has("colormap")
  );
  const apiAuthIssue = useMemo<TileIssue | null>(() => {
    const errors = [datasetsQuery.error, variablesQuery.error];
    if (!errors.some(isOciAuthApiError)) {
      return null;
    }
    return {
      key: "api-oci-auth-session",
      title: "Storage session needs attention",
      detail: "Backend API requests are returning 503 because OCI auth is unavailable. Refresh the OCI session or use instance/resource principal auth for this deployment.",
      tone: "bad"
    };
  }, [datasetsQuery.error, variablesQuery.error]);
  const visibleIssue = apiAuthIssue && apiAuthIssue.key !== dismissedIssueKey ? apiAuthIssue : null;

  useEffect(() => {
    if (!datasets || datasets.length === 0) {
      return;
    }

    const requestedDatasetId = shareUrl.requested.datasetId;
    if (!datasetId && requestedDatasetId && datasets.some((item) => item.id === requestedDatasetId)) {
      return;
    }

    if (!datasetId || !datasets.some((item) => item.id === datasetId)) {
      setDataset(datasets[0].id);
    }
  }, [datasetId, datasets, setDataset, shareUrl.requested.datasetId]);

  useEffect(() => {
    if (!variables || variables.length === 0) {
      return;
    }

    const requestedVariable = shareUrl.requested.variable;
    if (!variable && requestedVariable && variables.some((item) => item.id === requestedVariable)) {
      return;
    }

    if (!variable || !variables.some((item) => item.id === variable)) {
      setVariable(variables[0].id);
    }
  }, [setVariable, shareUrl.requested.variable, variable, variables]);

  useEffect(() => {
    const selectedDataset = datasets?.find((item) => item.id === datasetId);
    const styles = selectedDataset?.composite_styles ?? [];
    if (renderMode === "composite" && selectedDataset && styles.length === 0) {
      setRenderMode("band");
      return;
    }
    if (renderMode === "composite" && !compositeStyle && styles.length > 0) {
      setCompositeStyle(styles[0].id);
    }
    if (compositeStyle && !styles.some((style) => style.id === compositeStyle)) {
      setCompositeStyle(styles[0]?.id ?? null);
    }
  }, [compositeStyle, datasetId, datasets, renderMode, setCompositeStyle, setRenderMode]);

  useEffect(() => {
    if (!variable || !variables) {
      return;
    }

    const selectedVariable = variables.find((item) => item.id === variable);
    if (!selectedVariable) {
      return;
    }

    if (skipInitialUrlColormapRef.current) {
      skipInitialUrlColormapRef.current = false;
      return;
    }

    setColormap(selectedVariable.default_colormap ?? "viridis");
  }, [setColormap, variable, variables]);

  return (
    <div className="app-shell">
      <Sidebar
        shareWarnings={shareUrl.warnings}
        shareCopyStatus={shareUrl.copyStatus}
        onCopyShareLink={shareUrl.copyShareLink}
      />
      <main className="content">
        <TileIssueBanner issue={visibleIssue} onDismiss={setDismissedIssueKey} />
        <MapView />
      </main>
    </div>
  );
}

export default App;
