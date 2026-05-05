import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { buildWebSocketUrl } from "../api/endpoints";

type DatasetSocketStatus = "connecting" | "connected" | "disconnected";

const DATASET_QUERY_ROOTS = new Set([
  "datasets",
  "dataset",
  "variables",
  "serving-profile",
  "tilejson",
]);

export function useDatasetInvalidation() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<DatasetSocketStatus>("connecting");

  useEffect(() => {
    if (typeof window === "undefined" || typeof WebSocket === "undefined") {
      setStatus("disconnected");
      return;
    }

    let closedByEffect = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | undefined;

    const connect = () => {
      setStatus("connecting");
      const activeSocket = new WebSocket(buildWebSocketUrl("/ws/datasets"));
      socket = activeSocket;

      activeSocket.addEventListener("open", () => {
        setStatus("connected");
      });

      activeSocket.addEventListener("message", (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as { type?: string };
          if (payload.type === "datasets.invalidate") {
            void queryClient.invalidateQueries({
              predicate: (query) => DATASET_QUERY_ROOTS.has(String(query.queryKey[0])),
            });
          }
        } catch {
          // Ignore non-JSON messages; the backend contract is JSON.
        }
      });

      activeSocket.addEventListener("close", () => {
        setStatus("disconnected");
        if (!closedByEffect) {
          reconnectTimer = window.setTimeout(connect, 5000);
        }
      });

      activeSocket.addEventListener("error", () => {
        activeSocket.close();
      });
    };

    connect();

    return () => {
      closedByEffect = true;
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
      if (socket !== undefined) {
        socket.close();
      }
    };
  }, [queryClient]);

  return status;
}
