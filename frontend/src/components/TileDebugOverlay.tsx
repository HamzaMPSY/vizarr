import type { TilePrefetchDiagnostic } from "../hooks/useTilePrefetch";

export interface TileDebugRecord extends TilePrefetchDiagnostic {
  id: string;
  count: number;
}

export interface BrowserRenderDebug {
  renderMode: "server-tiles" | "browser-native" | "browser-gpu";
  nativeStatus: string;
  nativeReason: string;
  nativeMode: string;
  nativeChunks: number;
  nativeBytes: number;
  gpuStatus: string;
  gpuReason: string;
  gpuRenderer: string;
  gpuFailureCount: number;
  gpuLastError: string | null;
}

export interface TileIssue {
  key: string;
  title: string;
  detail: string;
  tone: "warn" | "bad";
}

interface TileIssueBannerProps {
  issue: TileIssue | null;
  onDismiss: (key: string) => void;
}

interface TileDebugOverlayProps {
  records: TileDebugRecord[];
  browser: BrowserRenderDebug;
}

export function TileIssueBanner({ issue, onDismiss }: TileIssueBannerProps) {
  if (!issue) {
    return null;
  }

  return (
    <section className={`tile-issue tile-issue--${issue.tone}`} role="status" aria-live="polite">
      <div>
        <p>{issue.title}</p>
        <span>{issue.detail}</span>
      </div>
      <button type="button" onClick={() => onDismiss(issue.key)}>
        Dismiss
      </button>
    </section>
  );
}

export function TileDebugOverlay({ records, browser }: TileDebugOverlayProps) {
  const latest = records.slice(0, 8);
  return (
    <aside className="tile-debug-overlay" aria-label="Tile debug overlay">
      <div className="tile-debug-overlay__header">
        <div>
          <p className="eyebrow">Debug</p>
          <h2>Tile diagnostics</h2>
        </div>
        <span>{browser.renderMode}</span>
      </div>

      <dl className="tile-debug-grid">
        <DebugPair label="Browser native" value={`${browser.nativeStatus}: ${browser.nativeReason}`} />
        <DebugPair label="Native read" value={`${browser.nativeMode}; ${browser.nativeChunks} chunks; ${formatBytes(browser.nativeBytes)}`} />
        <DebugPair label="Browser GPU" value={`${browser.gpuStatus}: ${browser.gpuReason}`} />
        <DebugPair
          label="GPU renderer"
          value={`${browser.gpuRenderer}; failures ${browser.gpuFailureCount}${
            browser.gpuLastError ? `; ${browser.gpuLastError}` : ""
          }`}
        />
      </dl>

      <div className="tile-debug-overlay__section">
        <div className="tile-debug-overlay__section-header">
          <span>Recent server tile samples</span>
          <span>{records.length}</span>
        </div>
        {latest.length > 0 ? (
          <ol className="tile-debug-list">
            {latest.map((record) => (
              <TileDebugRow key={record.id} record={record} />
            ))}
          </ol>
        ) : (
          <p className="tile-debug-empty">No sampled tile responses yet. Pan or zoom to collect diagnostics.</p>
        )}
      </div>
    </aside>
  );
}

function TileDebugRow({ record }: { record: TileDebugRecord }) {
  const headers = record.headers;
  const status = record.status === null ? "network" : String(record.status);
  const representation = header(headers, "X-Representation") ?? "unknown";
  const executionPath = header(headers, "X-Execution-Path") ?? "unknown";
  const cacheStatus = header(headers, "X-Cache-Status") ?? "unknown";
  const budgetStatus = header(headers, "X-Tile-Budget-Status") ?? "not reported";
  const timing = header(headers, "X-Tile-Time-Ms");
  const objectGets = header(headers, "X-Object-Get-Count") ?? "0";
  const byteRanges = header(headers, "X-Object-Byte-Range-Get-Count") ?? "0";
  const bytesRead = Number(header(headers, "X-Object-Bytes-Read") ?? "0");
  const chunks = header(headers, "X-Zarr-Chunk-Count") ?? "0";
  const budgetReason = header(headers, "X-Tile-Budget-Reason");

  return (
    <li className={record.ok ? "tile-debug-row" : "tile-debug-row tile-debug-row--failed"}>
      <div className="tile-debug-row__top">
        <span>{status}</span>
        <span>{representation}</span>
        <span>{cacheStatus}</span>
        <span>{relativeTime(record.recordedAt)}</span>
      </div>
      <p>
        z{record.z ?? "?"}/x{record.x ?? "?"}/y{record.y ?? "?"} via {executionPath}
        {record.count > 1 ? `, repeated ${record.count}x` : ""}
      </p>
      <dl>
        <DebugPair label="time" value={timing ? `${formatNumber(timing)} ms` : "n/a"} />
        <DebugPair label="objects" value={`${objectGets} full, ${byteRanges} range`} />
        <DebugPair label="bytes" value={formatBytes(bytesRead)} />
        <DebugPair label="chunks" value={chunks} />
        <DebugPair label="budget" value={budgetReason ? `${budgetStatus}: ${budgetReason}` : budgetStatus} />
      </dl>
      {record.errorMessage ? <p className="tile-debug-row__error">{record.errorMessage}</p> : null}
    </li>
  );
}

function DebugPair({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function header(headers: Record<string, string>, name: string): string | null {
  return headers[name] ?? null;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0 B";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function formatNumber(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(1) : value;
}

function relativeTime(value: number): string {
  const ageSeconds = Math.max(0, Math.round((Date.now() - value) / 1000));
  if (ageSeconds < 60) {
    return `${ageSeconds}s ago`;
  }
  return `${Math.round(ageSeconds / 60)}m ago`;
}
