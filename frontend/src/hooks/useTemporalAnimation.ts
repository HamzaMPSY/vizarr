import { useEffect } from "react";

import { getNextTimeIndex } from "../lib/temporal";
import { useMapStore } from "../store/mapStore";

export function useTemporalAnimation({
  timeStepCount,
  canPlay
}: {
  timeStepCount: number;
  canPlay: boolean;
}): void {
  const timeIndex = useMapStore((state) => state.timeIndex);
  const isPlaying = useMapStore((state) => state.timeAnimationPlaying);
  const speedMs = useMapStore((state) => state.timeAnimationSpeedMs);
  const loop = useMapStore((state) => state.timeAnimationLoop);
  const setTimeIndex = useMapStore((state) => state.setTimeIndex);
  const setTimeAnimationPlaying = useMapStore((state) => state.setTimeAnimationPlaying);

  useEffect(() => {
    if (timeIndex >= timeStepCount) {
      setTimeIndex(Math.max(0, timeStepCount - 1));
    }
  }, [setTimeIndex, timeIndex, timeStepCount]);

  useEffect(() => {
    if (!isPlaying) {
      return;
    }
    if (!canPlay || timeStepCount <= 1) {
      setTimeAnimationPlaying(false);
      return;
    }

    const timer = window.setTimeout(() => {
      const atEnd = timeIndex >= timeStepCount - 1;
      if (atEnd && !loop) {
        setTimeAnimationPlaying(false);
        return;
      }
      setTimeIndex(getNextTimeIndex(timeIndex, timeStepCount));
    }, speedMs);

    return () => window.clearTimeout(timer);
  }, [
    canPlay,
    isPlaying,
    loop,
    setTimeAnimationPlaying,
    setTimeIndex,
    speedMs,
    timeIndex,
    timeStepCount
  ]);
}
