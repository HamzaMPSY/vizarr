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

export interface MultiscaleReadBudget {
  maxPixels: number;
  maxChunks: number;
  maxChunkBytes: number;
  maxConcurrentChunkLoads: number;
}

export interface MultiscaleReadWindow {
  xStart: number;
  xStop: number;
  yStart: number;
  yStop: number;
  bbox: [number, number, number, number];
  mode: "full-level" | "viewport-window";
}

export interface LoadedMultiscalePlane {
  values: Float32Array;
  width: number;
  height: number;
  bbox: [number, number, number, number];
  levelPath: string;
  browseZoom: number | null;
  mode: "full-level" | "viewport-window";
  pixelCount: number;
  chunkCount: number;
  loadedBytes: number;
  estimatedChunkBytes: number;
}

export interface RenderedMultiscaleRaster {
  dataUrl: string;
  paletteImageData: ImageData;
}

export interface RenderedCompositeMultiscaleRaster {
  dataUrl: string;
  width: number;
  height: number;
}

export interface CompositeRasterBand {
  rawValues: Float32Array;
  width: number;
  height: number;
  vmin: number;
  vmax: number;
}

export async function loadMultiscaleMetadata(
  proxyRoot: string,
  dataArrayName: string,
  options: { signal?: AbortSignal } = {}
): Promise<MultiscaleMetadata> {
  const response = await fetch(buildApiUrl(`${proxyRoot}/.zmetadata`), { signal: options.signal });
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
  return explainUnsupportedLevel(level).length === 0;
}

export function explainUnsupportedLevel(level: MultiscaleLevelDescriptor): string[] {
  const reasons: string[] = [];
  if (level.dtype !== "<f4") {
    reasons.push(`dtype ${level.dtype} is not browser-native float32`);
  }
  if (level.compressor !== null) {
    reasons.push(`compressor ${describeCodec(level.compressor)} is not supported in the browser-native path`);
  }
  if (!filtersAreEmpty(level.filters)) {
    reasons.push("filters are not supported in the browser-native path");
  }
  if (level.order !== "C") {
    reasons.push(`array order ${level.order ?? "missing"} is not C-order`);
  }
  if (level.chunks[0] !== 1 || level.chunks[1] !== 1) {
    reasons.push("chunks must contain exactly one time step and one band");
  }
  if (level.chunks[2] !== 256 || level.chunks[3] !== 256) {
    reasons.push(`spatial chunks ${level.chunks[2]}x${level.chunks[3]} are not 256x256`);
  }
  return reasons;
}

export function fullLevelWindow(level: MultiscaleLevelDescriptor): MultiscaleReadWindow {
  return {
    xStart: 0,
    xStop: level.shape[3],
    yStart: 0,
    yStop: level.shape[2],
    bbox: level.bbox,
    mode: "full-level"
  };
}

export function viewportWindowForLevel(
  level: MultiscaleLevelDescriptor,
  viewportBounds: [number, number, number, number] | null
): MultiscaleReadWindow | null {
  if (!viewportBounds) {
    return null;
  }
  const [levelWest, levelSouth, levelEast, levelNorth] = level.bbox;
  const [viewWest, viewSouth, viewEast, viewNorth] = viewportBounds;
  const west = Math.max(levelWest, viewWest);
  const south = Math.max(levelSouth, viewSouth);
  const east = Math.min(levelEast, viewEast);
  const north = Math.min(levelNorth, viewNorth);
  if (!(west < east && south < north)) {
    return null;
  }

  const [, , height, width] = level.shape;
  const xStart = clamp(Math.floor(((west - levelWest) / (levelEast - levelWest)) * width), 0, width - 1);
  const xStop = clamp(Math.ceil(((east - levelWest) / (levelEast - levelWest)) * width), xStart + 1, width);
  const yStart = clamp(Math.floor(((levelNorth - north) / (levelNorth - levelSouth)) * height), 0, height - 1);
  const yStop = clamp(Math.ceil(((levelNorth - south) / (levelNorth - levelSouth)) * height), yStart + 1, height);

  return {
    xStart,
    xStop,
    yStart,
    yStop,
    bbox: pixelWindowToBBox(level, xStart, xStop, yStart, yStop),
    mode: "viewport-window"
  };
}

export function estimateReadWindow(
  level: MultiscaleLevelDescriptor,
  window: MultiscaleReadWindow
): { pixelCount: number; chunkCount: number; estimatedChunkBytes: number } {
  const [timeChunkSize, bandChunkSize, chunkHeight, chunkWidth] = level.chunks;
  const yChunkStart = Math.floor(window.yStart / chunkHeight);
  const yChunkStop = Math.ceil(window.yStop / chunkHeight);
  const xChunkStart = Math.floor(window.xStart / chunkWidth);
  const xChunkStop = Math.ceil(window.xStop / chunkWidth);
  let estimatedChunkBytes = 0;
  for (let chunkY = yChunkStart; chunkY < yChunkStop; chunkY += 1) {
    for (let chunkX = xChunkStart; chunkX < xChunkStop; chunkX += 1) {
      const yStart = chunkY * chunkHeight;
      const xStart = chunkX * chunkWidth;
      const actualHeight = Math.min(chunkHeight, level.shape[2] - yStart);
      const actualWidth = Math.min(chunkWidth, level.shape[3] - xStart);
      estimatedChunkBytes += timeChunkSize * bandChunkSize * actualHeight * actualWidth * Float32Array.BYTES_PER_ELEMENT;
    }
  }
  return {
    pixelCount: (window.xStop - window.xStart) * (window.yStop - window.yStart),
    chunkCount: Math.max(0, yChunkStop - yChunkStart) * Math.max(0, xChunkStop - xChunkStart),
    estimatedChunkBytes
  };
}

export function validateReadWindowBudget(
  level: MultiscaleLevelDescriptor,
  window: MultiscaleReadWindow,
  budget: MultiscaleReadBudget
): { pixelCount: number; chunkCount: number; estimatedChunkBytes: number } {
  const estimate = estimateReadWindow(level, window);
  if (estimate.pixelCount > budget.maxPixels) {
    throw new Error(`browser-native pixel budget exceeded: ${estimate.pixelCount} > ${budget.maxPixels}`);
  }
  if (estimate.chunkCount > budget.maxChunks) {
    throw new Error(`browser-native chunk budget exceeded: ${estimate.chunkCount} > ${budget.maxChunks}`);
  }
  if (estimate.estimatedChunkBytes > budget.maxChunkBytes) {
    throw new Error(`browser-native byte budget exceeded: ${estimate.estimatedChunkBytes} > ${budget.maxChunkBytes}`);
  }
  return estimate;
}

export function chooseReadWindow(
  level: MultiscaleLevelDescriptor,
  viewportBounds: [number, number, number, number] | null,
  budget: MultiscaleReadBudget
): MultiscaleReadWindow {
  const fullWindow = fullLevelWindow(level);
  const fullEstimate = estimateReadWindow(level, fullWindow);
  if (
    fullEstimate.pixelCount <= budget.maxPixels &&
    fullEstimate.chunkCount <= budget.maxChunks &&
    fullEstimate.estimatedChunkBytes <= budget.maxChunkBytes
  ) {
    return fullWindow;
  }

  const viewportWindow = viewportWindowForLevel(level, viewportBounds);
  if (!viewportWindow) {
    throw new Error("Selected multiscale level is too large for full-plane rendering and no viewport window is available");
  }
  validateReadWindowBudget(level, viewportWindow, budget);
  return viewportWindow;
}

export async function loadLevelPlaneWindow(
  proxyRoot: string,
  dataArrayName: string,
  level: MultiscaleLevelDescriptor,
  {
    timeIndex,
    bandIndex,
    window,
    budget,
    signal
  }: {
    timeIndex: number;
    bandIndex: number;
    window: MultiscaleReadWindow;
    budget: MultiscaleReadBudget;
    signal?: AbortSignal;
  }
): Promise<LoadedMultiscalePlane> {
  const [timeCount, bandCount, height, width] = level.shape;
  const [timeChunkSize, bandChunkSize, chunkHeight, chunkWidth] = level.chunks;
  const unsupported = explainUnsupportedLevel(level);
  if (unsupported.length > 0) {
    throw new Error(`Selected multiscale level is not browser-native readable: ${unsupported.join("; ")}`);
  }
  if (timeIndex < 0 || timeIndex >= timeCount || bandIndex < 0 || bandIndex >= bandCount) {
    throw new Error("Requested band/time index is outside the multiscale chunk bounds");
  }

  throwIfAborted(signal);
  const estimate = validateReadWindowBudget(level, window, budget);
  const timeChunk = Math.floor(timeIndex / timeChunkSize);
  const bandChunk = Math.floor(bandIndex / bandChunkSize);
  const timeOffset = timeIndex % timeChunkSize;
  const bandOffset = bandIndex % bandChunkSize;
  const yChunkStart = Math.floor(window.yStart / chunkHeight);
  const yChunkStop = Math.ceil(window.yStop / chunkHeight);
  const xChunkStart = Math.floor(window.xStart / chunkWidth);
  const xChunkStop = Math.ceil(window.xStop / chunkWidth);
  const targetWidth = window.xStop - window.xStart;
  const targetHeight = window.yStop - window.yStart;
  const plane = new Float32Array(targetWidth * targetHeight);
  plane.fill(Number.NaN);

  const chunkPositions: [number, number][] = [];
  for (let chunkY = yChunkStart; chunkY < yChunkStop; chunkY += 1) {
    for (let chunkX = xChunkStart; chunkX < xChunkStop; chunkX += 1) {
      chunkPositions.push([chunkY, chunkX]);
    }
  }

  let loadedBytes = 0;
  await mapWithConcurrency(
    chunkPositions,
    Math.max(1, Math.min(budget.maxConcurrentChunkLoads, chunkPositions.length)),
    async ([chunkY, chunkX]) => {
      throwIfAborted(signal);
      const yStart = chunkY * chunkHeight;
      const xStart = chunkX * chunkWidth;
      const actualHeight = Math.min(chunkHeight, height - yStart);
      const actualWidth = Math.min(chunkWidth, width - xStart);
      const chunk = await loadChunk(proxyRoot, dataArrayName, level, [timeChunk, bandChunk, chunkY, chunkX], signal);
      throwIfAborted(signal);
      loadedBytes += chunk.byteLength;
      const expectedSize = timeChunkSize * bandChunkSize * actualHeight * actualWidth;
      if (chunk.length !== expectedSize) {
        throw new Error(`Unexpected chunk length: received ${chunk.length}, expected ${expectedSize}`);
      }

      const copyYStart = Math.max(yStart, window.yStart);
      const copyYStop = Math.min(yStart + actualHeight, window.yStop);
      const copyXStart = Math.max(xStart, window.xStart);
      const copyXStop = Math.min(xStart + actualWidth, window.xStop);
      const sourcePlaneOffset = ((timeOffset * bandChunkSize) + bandOffset) * actualHeight * actualWidth;
      for (let globalY = copyYStart; globalY < copyYStop; globalY += 1) {
        const sourceRow = globalY - yStart;
        const targetRow = globalY - window.yStart;
        const sourceOffset = sourcePlaneOffset + sourceRow * actualWidth + (copyXStart - xStart);
        const targetOffset = targetRow * targetWidth + (copyXStart - window.xStart);
        plane.set(chunk.subarray(sourceOffset, sourceOffset + (copyXStop - copyXStart)), targetOffset);
      }
    }
  );

  return {
    values: plane,
    width: targetWidth,
    height: targetHeight,
    bbox: window.bbox,
    levelPath: level.path,
    browseZoom: level.browseZoom,
    mode: window.mode,
    pixelCount: estimate.pixelCount,
    chunkCount: estimate.chunkCount,
    loadedBytes,
    estimatedChunkBytes: estimate.estimatedChunkBytes
  };
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
  return (
    await loadLevelPlaneWindow(proxyRoot, dataArrayName, level, {
      timeIndex,
      bandIndex,
      window: fullLevelWindow(level),
      budget: {
        maxPixels: Number.MAX_SAFE_INTEGER,
        maxChunks: Number.MAX_SAFE_INTEGER,
        maxChunkBytes: Number.MAX_SAFE_INTEGER,
        maxConcurrentChunkLoads: 1
      }
    })
  ).values;
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
  return renderMultiscaleRaster(values, { width, height, palette, vmin, vmax }).dataUrl;
}

export function renderMultiscaleRaster(
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
): RenderedMultiscaleRaster {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("2D canvas context is unavailable");
  }

  const imageData = context.createImageData(width, height);
  const paletteImageData = context.createImageData(Math.max(1, palette.length), 1);
  const output = imageData.data;
  const paletteOutput = paletteImageData.data;
  const denominator = vmax > vmin ? vmax - vmin : 1;

  for (let index = 0; index < paletteImageData.width; index += 1) {
    const color = palette[index] ?? [0, 0, 0, 255];
    const targetIndex = index * 4;
    paletteOutput[targetIndex] = color[0] ?? 0;
    paletteOutput[targetIndex + 1] = color[1] ?? 0;
    paletteOutput[targetIndex + 2] = color[2] ?? 0;
    paletteOutput[targetIndex + 3] = color[3] ?? 255;
  }

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
  return {
    dataUrl: canvas.toDataURL("image/png"),
    paletteImageData
  };
}

export function renderCompositeMultiscaleRaster(
  bands: [CompositeRasterBand, CompositeRasterBand, CompositeRasterBand]
): RenderedCompositeMultiscaleRaster {
  const [red, green, blue] = bands;
  const width = red.width;
  const height = red.height;
  if (green.width !== width || blue.width !== width || green.height !== height || blue.height !== height) {
    throw new Error("Composite band dimensions do not match");
  }

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("2D canvas context is unavailable");
  }

  const imageData = context.createImageData(width, height);
  const output = imageData.data;
  const redDenominator = red.vmax > red.vmin ? red.vmax - red.vmin : 1;
  const greenDenominator = green.vmax > green.vmin ? green.vmax - green.vmin : 1;
  const blueDenominator = blue.vmax > blue.vmin ? blue.vmax - blue.vmin : 1;

  for (let index = 0; index < red.rawValues.length; index += 1) {
    const redValue = red.rawValues[index];
    const greenValue = green.rawValues[index];
    const blueValue = blue.rawValues[index];
    const targetIndex = index * 4;
    if (!Number.isFinite(redValue) || !Number.isFinite(greenValue) || !Number.isFinite(blueValue)) {
      output[targetIndex + 3] = 0;
      continue;
    }
    output[targetIndex] = Math.round(clamp((redValue - red.vmin) / redDenominator, 0, 1) * 255);
    output[targetIndex + 1] = Math.round(clamp((greenValue - green.vmin) / greenDenominator, 0, 1) * 255);
    output[targetIndex + 2] = Math.round(clamp((blueValue - blue.vmin) / blueDenominator, 0, 1) * 255);
    output[targetIndex + 3] = 255;
  }

  context.putImageData(imageData, 0, 0);
  return {
    dataUrl: canvas.toDataURL("image/png"),
    width,
    height
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

async function loadChunk(
  proxyRoot: string,
  dataArrayName: string,
  level: MultiscaleLevelDescriptor,
  coordinates: [number, number, number, number],
  signal?: AbortSignal
): Promise<Float32Array> {
  const chunkKey = level.dimensionSeparator === "/"
    ? coordinates.join("/")
    : coordinates.join(".");
  const response = await fetch(buildApiUrl(`${proxyRoot}/${level.path}/${dataArrayName}/${chunkKey}`), { signal });
  if (!response.ok) {
    throw new Error(`Failed to load multiscale chunk ${chunkKey}: ${response.status}`);
  }
  return new Float32Array(await response.arrayBuffer());
}

async function mapWithConcurrency<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>
): Promise<void> {
  let nextIndex = 0;
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (nextIndex < items.length) {
      const item = items[nextIndex];
      nextIndex += 1;
      if (item !== undefined) {
        await worker(item);
      }
      await Promise.resolve();
    }
  });
  await Promise.all(runners);
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (!signal?.aborted) {
    return;
  }
  throw signal.reason instanceof Error
    ? signal.reason
    : new DOMException("Browser multiscale request was aborted", "AbortError");
}

function filtersAreEmpty(filters: unknown | null): boolean {
  return filters === null || (Array.isArray(filters) && filters.length === 0);
}

function describeCodec(codec: unknown): string {
  const record = asRecord(codec);
  const id = record.id ?? record.name;
  return typeof id === "string" ? id : "configured";
}

function pixelWindowToBBox(
  level: MultiscaleLevelDescriptor,
  xStart: number,
  xStop: number,
  yStart: number,
  yStop: number
): [number, number, number, number] {
  const [west, south, east, north] = level.bbox;
  const [, , height, width] = level.shape;
  const nextWest = west + ((xStart / width) * (east - west));
  const nextEast = west + ((xStop / width) * (east - west));
  const nextNorth = north - ((yStart / height) * (north - south));
  const nextSouth = north - ((yStop / height) * (north - south));
  return [nextWest, nextSouth, nextEast, nextNorth];
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
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
