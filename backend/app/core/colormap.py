import io

import matplotlib
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from PIL import Image

from app.core.tile_observability import observe_tile_time


CUSTOM_COLORMAPS = {
    "red_green": LinearSegmentedColormap.from_list(
        "red_green",
        ["#c62828", "#2e7d32"],
    )
}


def _ensure_custom_colormaps() -> None:
    for name, colormap in CUSTOM_COLORMAPS.items():
        if name not in matplotlib.colormaps:
            matplotlib.colormaps.register(colormap, name=name)


def list_colormaps() -> list[str]:
    _ensure_custom_colormaps()
    preferred = ["red_green", "viridis", "plasma", "inferno", "magma", "cividis", "turbo"]
    available = [name for name in sorted(matplotlib.colormaps.keys()) if not name.endswith("_r")]
    names = [name for name in preferred if name in available]
    names.extend(name for name in available if name not in names)
    return names[:24]


def sample_colormap_palette(colormap: str, samples: int = 256) -> list[list[int]]:
    _ensure_custom_colormaps()
    sample_count = max(int(samples), 2)
    cmap = matplotlib.colormaps[colormap]
    values = np.linspace(0.0, 1.0, sample_count, dtype=np.float32)
    rgba = (cmap(values) * 255).astype(np.uint8)
    return rgba.tolist()


def encode_tile(data: np.ndarray, colormap: str, vmin: float, vmax: float) -> bytes:
    _ensure_custom_colormaps()
    if vmax <= vmin:
        vmax = vmin + 1e-6

    cmap = matplotlib.colormaps[colormap]
    finite_mask = np.isfinite(data)
    clipped = np.clip(data, vmin, vmax)
    normalized = ((clipped - vmin) / (vmax - vmin)).astype(np.float32)
    normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=0.0)
    rgba = (cmap(normalized) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(finite_mask, 255, 0).astype(np.uint8)

    with observe_tile_time("image_encoding"):
        image = Image.fromarray(rgba, mode="RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=85)
    return buffer.getvalue()
