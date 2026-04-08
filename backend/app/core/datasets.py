from dataclasses import dataclass

import numpy as np
import xarray as xr

from app.models.dataset import DatasetBounds, DatasetMeta, VariableMeta, VariableStats


@dataclass
class DatasetRegistry:
    dataset: xr.Dataset
    meta: DatasetMeta


def build_synthetic_dataset() -> xr.Dataset:
    time = np.arange(0, 4, dtype=np.int32)
    lat = np.linspace(85.0, -85.0, 480, dtype=np.float32)
    lon = np.linspace(-180.0, 180.0, 960, dtype=np.float32)

    lon_grid, lat_grid = np.meshgrid(lon, lat)
    lon_radians = np.deg2rad(lon_grid)
    lat_radians = np.deg2rad(lat_grid)

    temperature_frames: list[np.ndarray] = []
    precipitation_frames: list[np.ndarray] = []

    for t in time:
        temperature = (
            18.0
            + 12.0 * np.sin(lat_radians)
            + 6.0 * np.cos(lon_radians * 2.0 + (t * 0.5))
            + 2.0 * np.sin((lat_radians + lon_radians) * 3.0)
        )
        precipitation = (
            50.0
            + 40.0 * np.maximum(0.0, np.cos(lat_radians * 1.6 - t * 0.4))
            + 25.0 * (np.sin(lon_radians * 3.0 + t) ** 2)
        )
        temperature_frames.append(temperature.astype(np.float32))
        precipitation_frames.append(precipitation.astype(np.float32))

    return xr.Dataset(
        data_vars={
            "temperature": (("time", "lat", "lon"), np.stack(temperature_frames)),
            "precipitation": (("time", "lat", "lon"), np.stack(precipitation_frames)),
        },
        coords={"time": time, "lat": lat, "lon": lon},
        attrs={
            "id": "demo-global",
            "name": "Synthetic Global Weather",
            "description": "Synthetic temperature and precipitation fields for the Vizarr POC.",
        },
    )


def _stats(array: np.ndarray) -> VariableStats:
    flat = array.astype(np.float32)
    return VariableStats(
        min=float(np.nanmin(flat)),
        max=float(np.nanmax(flat)),
        p02=float(np.nanpercentile(flat, 2)),
        p98=float(np.nanpercentile(flat, 98)),
    )


def _sample_values(data_array: xr.DataArray) -> np.ndarray:
    sampled = data_array
    if "time" in sampled.dims and sampled.sizes.get("time", 1) > 1:
        sampled = sampled.isel(time=0)

    indexers: dict[str, slice] = {}
    for dim, size in sampled.sizes.items():
        if size > 256:
            stride = max(size // 256, 1)
            indexers[dim] = slice(None, None, stride)
    if indexers:
        sampled = sampled.isel(**indexers)

    return sampled.values.astype(np.float32)


def _stats_for_data_array(data_array: xr.DataArray) -> VariableStats:
    if {"valid_min", "valid_max"}.issubset(data_array.attrs):
        valid_min = float(data_array.attrs["valid_min"])
        valid_max = float(data_array.attrs["valid_max"])
        return VariableStats(
            min=valid_min,
            max=valid_max,
            p02=valid_min,
            p98=valid_max,
        )
    return _stats(_sample_values(data_array))


def build_metadata(
    dataset: xr.Dataset,
    dataset_id: str,
    dataset_name: str,
    dataset_description: str,
) -> DatasetMeta:
    variables = []
    units = {
        "temperature": "degC",
        "precipitation": "mm/day",
    }
    for variable_name, data_array in dataset.data_vars.items():
        dims = data_array.sizes
        time_steps = int(dims.get("time", 1))
        variables.append(
            VariableMeta(
                id=variable_name,
                name=variable_name.replace("_", " ").title(),
                unit=units.get(variable_name, "unknown"),
                time_steps=time_steps,
                stats=_stats_for_data_array(data_array),
            )
        )

    return DatasetMeta(
        id=dataset_id,
        name=dataset_name,
        description=dataset_description,
        variables=variables,
        bounds=DatasetBounds(west=-180.0, south=-85.0, east=180.0, north=85.0),
    )


def build_registry_from_dataset(
    dataset: xr.Dataset,
    dataset_id: str,
    dataset_name: str,
    dataset_description: str,
) -> DatasetRegistry:
    return DatasetRegistry(
        dataset=dataset,
        meta=build_metadata(
            dataset=dataset,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            dataset_description=dataset_description,
        ),
    )


def build_registry() -> DatasetRegistry:
    dataset = build_synthetic_dataset()
    return build_registry_from_dataset(
        dataset=dataset,
        dataset_id=str(dataset.attrs["id"]),
        dataset_name=str(dataset.attrs["name"]),
        dataset_description=str(dataset.attrs["description"]),
    )
