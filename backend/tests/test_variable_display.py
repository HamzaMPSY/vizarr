import numpy as np

from app.core.colormap import encode_tile
from app.core.colormap import list_colormaps
from app.core.variable_display import apply_variable_display_defaults
from app.core.variable_display import resolve_display_range
from app.models.dataset import VariableMeta
from app.models.dataset import VariableStats


def test_apply_variable_display_defaults_returns_ndvi_policy() -> None:
    display_vmin, display_vmax, default_colormap = apply_variable_display_defaults(
        variable_id="NDVI",
        variable_name="NDVI",
    )

    assert display_vmin == 0.0
    assert display_vmax == 1.0
    assert default_colormap == "red_green"


def test_resolve_display_range_prefers_variable_defaults() -> None:
    variable = VariableMeta(
        id="NDVI",
        name="NDVI",
        unit="DN",
        time_steps=4,
        stats=VariableStats(min=0.0, max=25.0, p02=0.31, p98=0.49),
        display_vmin=0.0,
        display_vmax=1.0,
        default_colormap="red_green",
    )

    assert resolve_display_range(variable, None, None) == (0.0, 1.0)


def test_list_colormaps_includes_custom_red_green() -> None:
    assert "red_green" in list_colormaps()


def test_encode_tile_accepts_custom_red_green_colormap() -> None:
    tile = encode_tile(
        np.array([[0.0, 0.5], [1.0, np.nan]], dtype=np.float32),
        "red_green",
        0.0,
        1.0,
    )

    assert len(tile) > 0
