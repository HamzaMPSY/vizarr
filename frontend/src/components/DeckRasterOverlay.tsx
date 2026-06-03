import type { Layer as DeckLayer } from "@deck.gl/core";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { useControl } from "react-map-gl/maplibre";

interface DeckRasterOverlayProps {
  layers: DeckLayer[];
  beforeId?: string;
  onError?: (error: unknown, layer?: DeckLayer) => void;
}

export function DeckRasterOverlay({ layers, beforeId, onError }: DeckRasterOverlayProps) {
  const overlay = useControl<MapboxOverlay>(
    () =>
      new MapboxOverlay({
        interleaved: true,
        layers: []
      } as ConstructorParameters<typeof MapboxOverlay>[0])
  );

  const nextLayers = beforeId
    ? layers.map((layer) => layer.clone({ beforeId } as Record<string, unknown>))
    : layers;
  overlay.setProps({
    layers: nextLayers,
    onError: (error: unknown, layer?: DeckLayer) => {
      onError?.(error, layer);
    }
  } as ConstructorParameters<typeof MapboxOverlay>[0]);

  return null;
}
