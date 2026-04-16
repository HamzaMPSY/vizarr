from __future__ import annotations

from app.models.dataset import VariableMeta


NDVI_VARIABLE_IDS = {"NDVI"}
NDVI_DEFAULT_COLORMAP = "red_green"


def apply_variable_display_defaults(
    *,
    variable_id: str,
    variable_name: str,
) -> tuple[float | None, float | None, str | None]:
    normalized_id = variable_id.strip().upper()
    normalized_name = variable_name.strip().upper()
    if normalized_id in NDVI_VARIABLE_IDS or "NDVI" in normalized_name:
        return 0.0, 1.0, NDVI_DEFAULT_COLORMAP
    return None, None, None


def resolve_display_range(variable: VariableMeta, vmin: float | None, vmax: float | None) -> tuple[float, float]:
    fallback_vmin = variable.display_vmin if variable.display_vmin is not None else variable.stats.p02
    fallback_vmax = variable.display_vmax if variable.display_vmax is not None else variable.stats.p98
    return (
        fallback_vmin if vmin is None else vmin,
        fallback_vmax if vmax is None else vmax,
    )


def resolve_default_colormap(variable: VariableMeta, fallback: str) -> str:
    return variable.default_colormap or fallback
