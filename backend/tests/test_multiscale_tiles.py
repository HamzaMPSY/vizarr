import json

from app.core.multiscale_tiles import load_pyramid_level_metadata


class _Connector:
    def __init__(self, payloads: dict[str, str]) -> None:
        self.payloads = payloads

    def build_oci_uri(self, object_path: str) -> str:
        return f"oci://bucket/{object_path.lstrip('/')}"

    def read_text(self, object_path: str, *, use_cache: bool = True) -> str:
        try:
            return self.payloads[object_path]
        except KeyError as exc:
            raise FileNotFoundError(object_path) from exc


def test_load_pyramid_level_metadata_supports_unconsolidated_zarr_v2_levels() -> None:
    connector = _Connector(
        {
            "oci://bucket/multiscale/cubes/example.zarr/.zgroup": json.dumps({"zarr_format": 2}),
            "oci://bucket/multiscale/cubes/example.zarr/.zattrs": json.dumps(
                {"multiscales": [{"datasets": [{"path": "9"}]}]}
            ),
            "oci://bucket/multiscale/cubes/example.zarr/9/.zattrs": json.dumps(
                {"tile_x_min": 299, "tile_x_max": 299, "tile_y_min": 258, "tile_y_max": 259}
            ),
            "oci://bucket/multiscale/cubes/example.zarr/9/bands/.zarray": json.dumps(
                {
                    "shape": [4, 1, 2, 1],
                    "chunks": [1, 1, 1, 1],
                    "dtype": "<f4",
                    "dimension_separator": ".",
                }
            ),
        }
    )

    level = load_pyramid_level_metadata(
        connector=connector,  # type: ignore[arg-type]
        store_path="multiscale/cubes/example.zarr",
        data_array_name="bands",
        zoom=9,
    )

    assert level is not None
    assert level.level_path == "9"
    assert level.tile_x_min == 299
    assert level.tile_x_max == 299
    assert level.tile_y_min == 258
    assert level.tile_y_max == 259
    assert level.shape == (4, 1, 2, 1)
    assert level.chunks == (1, 1, 1, 1)
    assert level.dimension_separator == "."
