import { buildPrefetchPlan } from "../lib/tilePrefetchPlanner";
import type { TilePrefetchPlanInput } from "../lib/tilePrefetchPlanner";

interface TilePrefetchWorkerRequest {
  type: "plan";
  input: TilePrefetchPlanInput;
}

self.onmessage = (event: MessageEvent<TilePrefetchWorkerRequest>) => {
  if (event.data.type !== "plan") {
    return;
  }
  self.postMessage(buildPrefetchPlan(event.data.input));
};
