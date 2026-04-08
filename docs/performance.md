# Performance

Everything that makes the viewer feel instant.

---

## Caching layers (fastest to slowest)

| Layer | Location | Latency | Hit condition |
|---|---|---|---|
| WebGL buffer | GPU | ~0ms | Tile is already rendered and viewport hasn't moved |
| TanStack Query memory | Browser RAM | ~0ms | Same query key requested again in the same session |
| Browser HTTP cache | Browser disk | ~1ms | `Cache-Control: max-age=3600` header present, tile not expired |
| Redis | Server RAM | <1ms | Tile key exists in Redis |
| Dask + Zarr | Object store | 50–300ms | Cache miss — first request for this tile |

A tile at zoom level 7 over a region the user has already visited will hit the browser HTTP cache and render at GPU speed. A brand new tile over a region never seen in this session will hit Redis if another user has requested it, or fall all the way through to a Zarr read. In practice, after a few minutes of browsing, the vast majority of tiles are served from the browser cache or Redis.

---

## Zarr chunking

This is the single biggest lever for backend performance. The goal is for each map tile request to read exactly one Zarr chunk — no wasted bytes, no reading chunks you throw away.

### Recommended encoding

```python
import xarray as xr

ds.to_zarr(
    "s3://your-bucket/data.zarr",
    encoding={
        "temperature": {
            "chunks": [1, 256, 256],    # [time, lat, lon]
            "compressor": numcodecs.Blosc(
                cname="zstd",
                clevel=3,               # fast decompression
                shuffle=numcodecs.Blosc.BITSHUFFLE,
            ),
            "dtype": "float32",
        }
    },
    consolidated=True,                  # write .zmetadata for fast open
    zarr_version=2,
)
```

The `[1, 256, 256]` chunk shape means each chunk covers exactly one time step and one 256×256 spatial tile at the native resolution. A tile request at the zoom level matching the data resolution reads one chunk. A tile at a coarser zoom level reads one chunk and downsamples. Both are O(1) in I/O.

### Why Blosc-zstd level 3

Level 3 gives ~60% compression ratio with decompression speeds around 2–3 GB/s. The bottleneck is almost always the network read from the object store, not decompression. Higher compression levels save bandwidth but add latency; lower levels are faster to decompress but read more bytes. Level 3 is the sweet spot for typical satellite float32 data.

### Using `consolidated=True`

Writing with `consolidated=True` creates a single `.zmetadata` file containing all chunk metadata. `xarray.open_zarr()` reads this one file instead of issuing a separate HTTP request per variable. This cuts the store open time from O(variables) to O(1) — critical for datasets with dozens of variables.

---

## Tile encoding: WebP vs PNG

WebP is the default output format. At quality 85, a 256×256 tile encoded as WebP is typically 30–50% smaller than the equivalent PNG. For a viewer that may load hundreds of tiles in a session, this translates directly to bandwidth and latency.

| Format | Avg size (256×256, continuous data) | Lossless | Transparency |
|---|---|---|---|
| PNG | ~40–80 KB | Yes | Yes |
| WebP q=85 | ~20–45 KB | No | Yes |
| WebP lossless | ~30–60 KB | Yes | Yes |

For satellite visualizations (colormapped float data rendered as RGBA images) lossy WebP at quality 85 is visually indistinguishable from PNG at normal map zoom levels. If pixel-perfect accuracy matters (e.g. the user can read exact values from the tile), switch to `WebP lossless` or PNG.

To change the encoding in `colormap.py`:

```python
# WebP lossy (default, smallest)
img.save(buf, format="WEBP", quality=85)

# WebP lossless (larger, no quality loss)
img.save(buf, format="WEBP", lossless=True)

# PNG (largest, truly lossless)
img.save(buf, format="PNG", optimize=True)
```

---

## Predictive prefetch

The prefetch worker fires before the user stops moving. The triggering logic in `usePrefetch.ts`:

```ts
import { useEffect, useRef } from "react";
import { useMapStore } from "@/store/mapStore";
import type { ViewState } from "@/types";

export function usePrefetch(tileUrlTemplate: string) {
  const workerRef = useRef<Worker>();

  useEffect(() => {
    workerRef.current = new Worker(
      new URL("@/workers/prefetch.worker.ts", import.meta.url),
      { type: "module" }
    );
    return () => workerRef.current?.terminate();
  }, []);

  const trigger = (viewState: ViewState) => {
    const z = Math.floor(viewState.zoom);
    const n = 2 ** z;
    const x = Math.floor((viewState.longitude + 180) / 360 * n);
    const y = Math.floor(
      (1 - Math.log(
        Math.tan(viewState.latitude * Math.PI / 180) +
        1 / Math.cos(viewState.latitude * Math.PI / 180)
      ) / Math.PI) / 2 * n
    );
    workerRef.current?.postMessage({ z, centerX: x, centerY: y, tileUrlTemplate, radius: 2 });
  };

  return trigger;
}
```

The worker prefetches a 5×5 grid (radius=2) around the centre tile — 24 tiles in the surrounding ring. At zoom 7, this covers a region roughly 5× the visible viewport. In practice this means the next pan destination is almost always already cached.

---

## Debouncing and stale-while-revalidate

Two patterns work together to keep the UI responsive during fast interactions.

**Debouncing (150ms):** Viewport changes fire on every animation frame (~60fps). Without debouncing, every frame would trigger dataset metadata queries and prefetch calls. The 150ms debounce in `useViewport.ts` means these only fire once per pan/zoom gesture, after the user pauses.

**Stale-while-revalidate:** TanStack Query shows the last known data immediately (stale) while silently fetching fresh data in the background (revalidate). For dataset lists and variable metadata, this means the sidebar never shows a loading spinner on repeat visits — it shows the cached list instantly and updates it if anything changed.

---

## Nginx tile cache

The `nginx.conf` adds a disk-based tile cache in front of FastAPI. This is particularly valuable for popular tiles that are requested by many users — Nginx serves them directly without touching the Python process.

```nginx
proxy_cache_path /var/cache/nginx/tiles
    levels=1:2
    keys_zone=tiles:10m
    max_size=2g
    inactive=1h
    use_temp_path=off;

location /api/tiles/ {
    proxy_pass         http://backend:8000;
    proxy_cache        tiles;
    proxy_cache_key    "$uri$is_args$args";
    proxy_cache_valid  200 1h;
    proxy_cache_use_stale error timeout updating;
    add_header X-Cache-Status $upstream_cache_status;
}
```

The `proxy_cache_use_stale updating` directive means that while Nginx is fetching a fresh tile in the background, it serves the stale cached version to the user. This eliminates cache-expiry latency spikes entirely.

---

## fsspec block cache

`fsspec` maintains an in-process byte-range cache (`BlockCache`) for recently read chunks from the object store. This is separate from Redis and operates at the byte level rather than the tile level. Configure it in `zarr_reader.py`:

```python
import fsspec

fs = fsspec.filesystem(
    "s3",
    key=settings.aws_access_key_id,
    secret=settings.aws_secret_access_key,
    client_kwargs={"region_name": settings.aws_region},
    cache_type="blockcache",
    cache_options={"cache_storage": "/tmp/fsspec_cache", "block_size": 2**20},
)
```

With `block_size=1MB`, each HTTP range request fetches 1MB at a time. Adjacent tiles that share a Zarr chunk will be served from the local block cache without a second network request.

---

## HTTP/2 + connection reuse

Ensure the backend is served over HTTP/2 in production (Nginx handles this with the `http2` directive). HTTP/2 multiplexes multiple tile requests over a single TCP connection, eliminating the per-request connection overhead that makes HTTP/1.1 slow for tile workloads. The browser typically issues 6–10 tile requests simultaneously for each viewport — HTTP/2 handles all of them over one connection.

```nginx
listen 443 ssl;
http2 on;
```

---

## Colormap range seeding

Rather than requiring a separate API call to discover the data range (`vmin`/`vmax`) before the first tile can be requested, the backend embeds the range in the tile response as HTTP headers:

```python
# In tiles.py
return Response(
    tile,
    media_type="image/webp",
    headers={
        "Cache-Control": "max-age=3600",
        "X-Data-Vmin": str(stats.p2),
        "X-Data-Vmax": str(stats.p98),
    }
)
```

The frontend reads these headers from the first tile response and seeds the Zustand store with the range, enabling the legend to render correctly with zero additional requests.
