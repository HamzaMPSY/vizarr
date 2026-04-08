import io

import matplotlib
import numpy as np
from PIL import Image


def list_colormaps() -> list[str]:
    names = sorted(matplotlib.colormaps.keys())
    return [name for name in names if not name.endswith("_r")][:24]


def encode_tile(data: np.ndarray, colormap: str, vmin: float, vmax: float) -> bytes:
    if vmax <= vmin:
        vmax = vmin + 1e-6

    cmap = matplotlib.colormaps[colormap]
    finite_mask = np.isfinite(data)
    clipped = np.clip(data, vmin, vmax)
    normalized = ((clipped - vmin) / (vmax - vmin)).astype(np.float32)
    normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=0.0)
    rgba = (cmap(normalized) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(finite_mask, 255, 0).astype(np.uint8)

    image = Image.fromarray(rgba, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=85)
    return buffer.getvalue()

