import { buildApiUrl } from "../api/endpoints";

export interface MultiscaleLevelDescriptor {
  path: string;
  browseZoom: number | null;
  bbox: [number, number, number, number];
  shape: [number, number, number, number];
  chunks: [number, number, number, number];
  dtype: string;
  compressor: unknown;
  filters: unknown | null;
  order: string | null;
  dimensionSeparator: "." | "/";
}

interface ZarrV2ArrayMetadata {
  shape: number[];
  chunks: number[];
  dtype: string;
  compressor: unknown;
  filters?: unknown | null;
  order?: string;
  dimension_separator?: "." | "/";
}

interface ZarrV2MetadataEnvelope {
  metadata?: Record<string, unknown>;
}

export interface MultiscaleMetadata {
  proxyRoot: string;
  dataArrayName: string;
  levels: MultiscaleLevelDescriptor[];
}

export async function loadMultiscaleMetadata(proxyRoot: string, dataArrayName: string): Promise<MultiscaleMetadata> {
  const response = await fetch(buildApiUrl(`${proxyRoot}/.zmetadata`));
  if (!response.ok) {
    throw new Error(`Failed to load multiscale metadata: ${response.status}`);
  }

  const payload = (await response.json()) as ZarrV2MetadataEnvelope;
  const metadata = payload.metadata ?? {};
  const rootAttrs = asRecord(metadata[".zattrs"]);
  const multiscales = Array.isArray(rootAttrs.multiscales) ? rootAttrs.multiscales : [];
  const firstMultiscale = asRecord(multiscales[0]);
  const datasets = Array.isArray(firstMultiscale.datasets) ? firstMultiscale.datasets : [];
  const browseZoomLevels = Array.isArray(rootAttrs.browse_zoom_levels)
    ? rootAttrs.browse_zoom_levels.map((value) => Number(value))
    : [];

  const levels: MultiscaleLevelDescriptor[] = [];
  for (const [index, dataset] of datasets.entries()) {
    const path = asRecord(dataset).path;
    if (typeof path !== "string" || !path) {
      continue;
    }
    const levelAttrs = asRecord(metadata[`${path}/.zattrs`]);
    const arrayMeta = asRecord(metadata[`${path}/${dataArrayName}/.zarray`]) as unknown as ZarrV2ArrayMetadata;
    if (!Array.isArray(arrayMeta.shape) || !Array.isArray(arrayMeta.chunks) || typeof arrayMeta.dtype !== "string") {
      continue;
    }
    const bboxRaw = Array.isArray(levelAttrs.bbox_epsg3857)
      ? levelAttrs.bbox_epsg3857
      : Array.isArray(levelAttrs.bbox_wgs84)
        ? levelAttrs.bbox_wgs84
        : null;
    const bboxIsWebMercator = Array.isArray(levelAttrs.bbox_epsg3857);
    if (!bboxRaw || bboxRaw.length !== 4) {
      continue;
    }
    levels.push({
      path,
      browseZoom: Number.isFinite(browseZoomLevels[index]) ? browseZoomLevels[index] : null,
      bbox: bboxIsWebMercator
        ? webMercatorBBoxToLonLat(bboxRaw.map((value) => Number(value)) as [number, number, number, number])
        : bboxRaw.map((value) => Number(value)) as [number, number, number, number],
      shape: arrayMeta.shape.map((value) => Number(value)) as [number, number, number, number],
      chunks: arrayMeta.chunks.map((value) => Number(value)) as [number, number, number, number],
      dtype: arrayMeta.dtype,
      compressor: arrayMeta.compressor,
      filters: arrayMeta.filters ?? null,
      order: typeof arrayMeta.order === "string" ? arrayMeta.order : null,
      dimensionSeparator: arrayMeta.dimension_separator === "/" ? "/" : "."
    });
  }

  if (levels.length === 0) {
    throw new Error("No usable multiscale levels were found in consolidated metadata");
  }

  return {
    proxyRoot,
    dataArrayName,
    levels
  };
}

export function selectMultiscaleLevel(
  metadata: MultiscaleMetadata,
  zoom: number
): MultiscaleLevelDescriptor | null {
  if (metadata.levels.length === 0) {
    return null;
  }

  const candidates = metadata.levels.filter((level) => level.browseZoom !== null);
  if (candidates.length === 0) {
    return metadata.levels[0];
  }

  return candidates.reduce((best, current) => {
    const bestDistance = Math.abs((best.browseZoom ?? 0) - zoom);
    const currentDistance = Math.abs((current.browseZoom ?? 0) - zoom);
    return currentDistance < bestDistance ? current : best;
  });
}

export function levelSupportsDirectChunkRead(level: MultiscaleLevelDescriptor): boolean {
  return (
    level.dtype === "<f4" &&
    level.compressor === null &&
    level.filters === null &&
    level.order === "C" &&
    level.chunks[0] === 1 &&
    level.chunks[1] === 1 &&
    level.chunks[2] === 256 &&
    level.chunks[3] === 256
  );
}

export async function loadLevelPlane(
  proxyRoot: string,
  dataArrayName: string,
  level: MultiscaleLevelDescriptor,
  {
    timeIndex,
    bandIndex
  }: {
    timeIndex: number;
    bandIndex: number;
  }
): Promise<Float32Array> {
  const [timeCount, bandCount, height, width] = level.shape;
  const [timeChunkSize, bandChunkSize, chunkHeight, chunkWidth] = level.chunks;
  if (!levelSupportsDirectChunkRead(level)) {
    throw new Error("Selected multiscale level is not directly readable by the browser");
  }
  if (timeIndex < 0 || timeIndex >= timeCount || bandIndex < 0 || bandIndex >= bandCount) {
    throw new Error("Requested band/time index is outside the multiscale chunk bounds");
  }

  const timeChunk = Math.floor(timeIndex / timeChunkSize);
  const bandChunk = Math.floor(bandIndex / bandChunkSize);
  const timeOffset = timeIndex % timeChunkSize;
  const bandOffset = bandIndex % bandChunkSize;
  const chunksY = Math.ceil(height / chunkHeight);
  const chunksX = Math.ceil(width / chunkWidth);
  const plane = new Float32Array(height * width);

  for (let chunkY = 0; chunkY < chunksY; chunkY += 1) {
    for (let chunkX = 0; chunkX < chunksX; chunkX += 1) {
      const yStart = chunkY * chunkHeight;
      const xStart = chunkX * chunkWidth;
      const actualHeight = Math.min(chunkHeight, height - yStart);
      const actualWidth = Math.min(chunkWidth, width - xStart);
      const chunk = await loadChunk(proxyRoot, dataArrayName, level, [timeChunk, bandChunk, chunkY, chunkX]);
      const expectedSize = timeChunkSize * bandChunkSize * actualHeight * actualWidth;
      if (chunk.length !== expectedSize) {
        throw new Error(`Unexpected chunk length: received ${chunk.length}, expected ${expectedSize}`);
      }

      const sourcePlaneOffset = ((timeOffset * bandChunkSize) + bandOffset) * actualHeight * actualWidth;
      for (let row = 0; row < actualHeight; row += 1) {
        const sourceOffset = sourcePlaneOffset + row * actualWidth;
        const targetOffset = (yStart + row) * width + xStart;
        plane.set(chunk.subarray(sourceOffset, sourceOffset + actualWidth), targetOffset);
      }
    }
  }

  return plane;
}

export function renderChunkToDataUrl(
  values: Float32Array,
  {
    width,
    height,
    palette,
    vmin,
    vmax
  }: {
    width: number;
    height: number;
    palette: number[][];
    vmin: number;
    vmax: number;
  }
): string {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("2D canvas context is unavailable");
  }

  const imageData = context.createImageData(width, height);
  const output = imageData.data;
  const denominator = vmax > vmin ? vmax - vmin : 1;

  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    const targetIndex = index * 4;
    if (!Number.isFinite(value)) {
      output[targetIndex + 3] = 0;
      continue;
    }

    const normalized = Math.min(1, Math.max(0, (value - vmin) / denominator));
    const paletteIndex = Math.min(palette.length - 1, Math.max(0, Math.round(normalized * (palette.length - 1))));
    const color = palette[paletteIndex] ?? [0, 0, 0, 255];
    output[targetIndex] = color[0] ?? 0;
    output[targetIndex + 1] = color[1] ?? 0;
    output[targetIndex + 2] = color[2] ?? 0;
    output[targetIndex + 3] = color[3] ?? 255;
  }

  context.putImageData(imageData, 0, 0);
  return canvas.toDataURL("image/png");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

async function loadChunk(
  proxyRoot: string,
  dataArrayName: string,
  level: MultiscaleLevelDescriptor,
  coordinates: [number, number, number, number]
): Promise<Float32Array> {
  const chunkKey = level.dimensionSeparator === "/"
    ? coordinates.join("/")
    : coordinates.join(".");
  const response = await fetch(buildApiUrl(`${proxyRoot}/${level.path}/${dataArrayName}/${chunkKey}`));
  if (!response.ok) {
    throw new Error(`Failed to load multiscale chunk ${chunkKey}: ${response.status}`);
  }
  return new Float32Array(await response.arrayBuffer());
}

function webMercatorBBoxToLonLat(
  bbox: [number, number, number, number]
): [number, number, number, number] {
  return [mercatorXToLon(bbox[0]), mercatorYToLat(bbox[1]), mercatorXToLon(bbox[2]), mercatorYToLat(bbox[3])];
}


function mercatorXToLon(value: number): number {
  return (value / 20037508.342789244) * 180;
}


function mercatorYToLat(value: number): number {
  const degrees = (value / 20037508.342789244) * 180;
  return (180 / Math.PI) * (2 * Math.atan(Math.exp((degrees * Math.PI) / 180)) - (Math.PI / 2));
}
