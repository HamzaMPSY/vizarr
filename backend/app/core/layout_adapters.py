from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Protocol

from app.models.dataset import LayoutValidation
from app.models.dataset import LayoutValidationIssue


PROJECTED_REQUIRED_METADATA = [
    "data array shape",
    "data array dimension names",
    "x coordinate array",
    "y coordinate array",
    "spatial_ref.crs_wkt for reliable CRS handling",
    "spatial_ref.GeoTransform or x/y coordinate arrays for spatial transform",
]
PROJECTED_CRS_CONVENTIONS = [
    "CF spatial_ref.attributes.crs_wkt",
    "GDAL GeoTransform in spatial_ref.attributes.GeoTransform",
    "numeric x and y coordinate arrays",
]
PROJECTED_TILE_CAPABILITIES = [
    "dynamic_tiles",
    "browse_overviews",
    "multiscale_source",
]
PROJECTED_READBACK_CAPABILITIES = [
    "point",
    "bbox",
    "range",
    "clip",
]


@dataclass(frozen=True)
class LayoutAdapterContract:
    name: str
    priority: int
    accepted_dimensions: tuple[str, ...]
    required_metadata: tuple[str, ...]
    crs_transform_conventions: tuple[str, ...]
    tile_capabilities: tuple[str, ...]
    readback_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class LayoutIssue:
    code: str
    message: str
    remediation: str

    def to_model(self) -> LayoutValidationIssue:
        return LayoutValidationIssue(
            code=self.code,
            message=self.message,
            remediation=self.remediation,
        )


@dataclass(frozen=True)
class ProjectedLayout:
    data_array_name: str
    band_array_name: str | None
    variable_array_names: dict[str, str]
    validation: LayoutValidation


@dataclass(frozen=True)
class LayoutAdapterResult:
    contract: LayoutAdapterContract | None
    accepted: bool
    data_array_name: str | None = None
    band_array_name: str | None = None
    variable_array_names: dict[str, str] = field(default_factory=dict)
    matched_dimensions: tuple[str, ...] = ()
    issues: tuple[LayoutIssue, ...] = ()

    def to_validation(self) -> LayoutValidation:
        contract = self.contract
        return LayoutValidation(
            adapter_name=contract.name if contract else None,
            adapter_priority=contract.priority if contract else None,
            accepted=self.accepted,
            data_array_name=self.data_array_name,
            band_array_name=self.band_array_name,
            variable_array_names=dict(self.variable_array_names),
            matched_dimensions=list(self.matched_dimensions),
            accepted_dimensions=list(contract.accepted_dimensions if contract else ()),
            required_metadata=list(contract.required_metadata if contract else ()),
            crs_transform_conventions=list(contract.crs_transform_conventions if contract else ()),
            tile_capabilities=list(contract.tile_capabilities if contract else ()),
            readback_capabilities=list(contract.readback_capabilities if contract else ()),
            issues=[issue.to_model() for issue in self.issues],
        )

    def to_projected_layout(self) -> ProjectedLayout:
        if not self.accepted or self.data_array_name is None:
            raise ValueError(self.error_message())
        return ProjectedLayout(
            data_array_name=self.data_array_name,
            band_array_name=self.band_array_name,
            variable_array_names=dict(self.variable_array_names),
            validation=self.to_validation(),
        )

    def error_message(self) -> str:
        if self.issues:
            details = "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)
            return f"Dataset does not expose a supported projected layout: {details}"
        return "Dataset does not expose a supported projected layout"


class LayoutAdapter(Protocol):
    contract: LayoutAdapterContract

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        ...


class LayoutAdapterRegistry:
    def __init__(self, adapters: list[LayoutAdapter] | None = None) -> None:
        self._adapters: list[LayoutAdapter] = []
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: LayoutAdapter) -> None:
        self._adapters = [item for item in self._adapters if item.contract.name != adapter.contract.name]
        self._adapters.append(adapter)
        self._adapters.sort(key=lambda item: item.contract.priority, reverse=True)

    def adapters(self) -> list[LayoutAdapter]:
        return list(self._adapters)

    def select(self, metadata: dict[str, dict]) -> LayoutAdapterResult:
        rejected: list[LayoutAdapterResult] = []
        for adapter in self._adapters:
            result = adapter.validate(metadata)
            if result is None:
                continue
            if result.accepted:
                return result
            rejected.append(result)
        if rejected:
            return rejected[0]
        return LayoutAdapterResult(
            contract=None,
            accepted=False,
            issues=(
                LayoutIssue(
                    code="missing_data_array_metadata",
                    message="No candidate data array with usable dimension metadata was found.",
                    remediation="Add an array with dimensions time/band/y/x, time/y/x, or y/x plus x/y coordinates.",
                ),
            ),
        )


class ProjectedBanded4DAdapter:
    contract = LayoutAdapterContract(
        name="projected-4d-banded",
        priority=100,
        accepted_dimensions=("time/*/y/x",),
        required_metadata=tuple(PROJECTED_REQUIRED_METADATA + ["band coordinate array or band_labels attribute"]),
        crs_transform_conventions=tuple(PROJECTED_CRS_CONVENTIONS),
        tile_capabilities=tuple(PROJECTED_TILE_CAPABILITIES),
        readback_capabilities=tuple(PROJECTED_READBACK_CAPABILITIES),
    )

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        rejected: list[LayoutAdapterResult] = []
        for array_name, node in sorted(metadata.items()):
            shape = node.get("shape")
            dimensions = dimension_names_from_node(node)
            if not isinstance(shape, list) or len(shape) != 4:
                continue
            if not {"time", "x", "y"}.issubset(dimensions):
                continue
            non_spatial_dims = [name for name in dimensions if name not in {"time", "x", "y"}]
            if len(non_spatial_dims) != 1:
                rejected.append(_unsupported_dimension_order(self.contract, dimensions))
                continue
            if dimensions[0] != "time" or dimensions[-2:] != ("y", "x"):
                rejected.append(_unsupported_dimension_order(self.contract, dimensions))
                continue
            return LayoutAdapterResult(
                contract=self.contract,
                accepted=True,
                data_array_name=array_name,
                band_array_name=non_spatial_dims[0],
                matched_dimensions=dimensions,
            )
        return rejected[0] if rejected else None


class ProjectedTimeVariable3DAdapter:
    contract = LayoutAdapterContract(
        name="projected-3d-time-variable",
        priority=90,
        accepted_dimensions=("time/y/x",),
        required_metadata=tuple(PROJECTED_REQUIRED_METADATA),
        crs_transform_conventions=tuple(PROJECTED_CRS_CONVENTIONS),
        tile_capabilities=tuple(PROJECTED_TILE_CAPABILITIES),
        readback_capabilities=tuple(PROJECTED_READBACK_CAPABILITIES),
    )

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        variable_arrays: dict[str, str] = {}
        rejected: list[LayoutAdapterResult] = []
        for array_name, node in sorted(metadata.items()):
            shape = node.get("shape")
            dimensions = dimension_names_from_node(node)
            if not isinstance(shape, list) or len(shape) != 3:
                continue
            if dimensions == ("time", "y", "x"):
                variable_arrays[array_name] = array_name
            elif {"time", "x", "y"}.issubset(dimensions):
                rejected.append(_unsupported_dimension_order(self.contract, dimensions))

        if variable_arrays:
            first_array_name = next(iter(variable_arrays.values()))
            return LayoutAdapterResult(
                contract=self.contract,
                accepted=True,
                data_array_name=first_array_name,
                variable_array_names=variable_arrays,
                matched_dimensions=("time", "y", "x"),
            )
        return rejected[0] if rejected else None


class ProjectedStaticVariable2DAdapter:
    contract = LayoutAdapterContract(
        name="projected-2d-static-variable",
        priority=80,
        accepted_dimensions=("y/x",),
        required_metadata=tuple(PROJECTED_REQUIRED_METADATA),
        crs_transform_conventions=tuple(PROJECTED_CRS_CONVENTIONS),
        tile_capabilities=tuple(PROJECTED_TILE_CAPABILITIES),
        readback_capabilities=tuple(PROJECTED_READBACK_CAPABILITIES),
    )

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        variable_arrays: dict[str, str] = {}
        rejected: list[LayoutAdapterResult] = []
        for array_name, node in sorted(metadata.items()):
            shape = node.get("shape")
            dimensions = dimension_names_from_node(node)
            if not isinstance(shape, list) or len(shape) != 2:
                continue
            if dimensions == ("y", "x"):
                variable_arrays[array_name] = array_name
            elif {"x", "y"}.issubset(dimensions):
                rejected.append(_unsupported_dimension_order(self.contract, dimensions))

        if variable_arrays:
            first_array_name = next(iter(variable_arrays.values()))
            return LayoutAdapterResult(
                contract=self.contract,
                accepted=True,
                data_array_name=first_array_name,
                variable_array_names=variable_arrays,
                matched_dimensions=("y", "x"),
            )
        return rejected[0] if rejected else None


class GeographicLatLonAdapter:
    contract = LayoutAdapterContract(
        name="geographic-lat-lon",
        priority=20,
        accepted_dimensions=("lat/lon", "time/lat/lon"),
        required_metadata=(
            "latitude coordinate array",
            "longitude coordinate array",
            "adapter-specific reprojection policy",
        ),
        crs_transform_conventions=("EPSG:4326 latitude/longitude coordinate arrays",),
        tile_capabilities=(),
        readback_capabilities=(),
    )

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        for node in metadata.values():
            dimensions = dimension_names_from_node(node)
            if "lat" not in dimensions or "lon" not in dimensions:
                continue
            return LayoutAdapterResult(
                contract=self.contract,
                accepted=False,
                matched_dimensions=dimensions,
                issues=(
                    LayoutIssue(
                        code="unsupported_dimension_order",
                        message="Latitude/longitude dimensions are recognized but are not a renderable source adapter yet.",
                        remediation="Normalize the source to y/x projected axes or add a project-specific lat/lon adapter.",
                    ),
                ),
            )
        return None


class AmbiguousDimensionsAdapter:
    contract = LayoutAdapterContract(
        name="unsupported-ambiguous-dimensions",
        priority=0,
        accepted_dimensions=("time/band/y/x", "time/y/x", "y/x"),
        required_metadata=tuple(PROJECTED_REQUIRED_METADATA),
        crs_transform_conventions=tuple(PROJECTED_CRS_CONVENTIONS),
        tile_capabilities=(),
        readback_capabilities=(),
    )

    def validate(self, metadata: dict[str, dict]) -> LayoutAdapterResult | None:
        for node in metadata.values():
            if isinstance(node.get("shape"), list) and dimension_names_from_node(node):
                return LayoutAdapterResult(
                    contract=self.contract,
                    accepted=False,
                    matched_dimensions=dimension_names_from_node(node),
                    issues=(
                        LayoutIssue(
                            code="unsupported_dimension_order",
                            message="Arrays expose dimensions, but none match time/*/y/x, time/y/x, or y/x.",
                            remediation="Use trailing y/x spatial dimensions and put time first when a time axis exists.",
                        ),
                    ),
                )
        return LayoutAdapterResult(
            contract=self.contract,
            accepted=False,
            issues=(
                LayoutIssue(
                    code="missing_dimension_metadata",
                    message="Array dimension names are missing.",
                    remediation="Add Zarr v3 dimension_names or Xarray _ARRAY_DIMENSIONS attributes.",
                ),
            ),
        )


def dimension_names_from_node(node: dict) -> tuple[str, ...]:
    dimension_names = node.get("dimension_names")
    if isinstance(dimension_names, (list, tuple)) and all(isinstance(item, str) for item in dimension_names):
        return tuple(dimension_names)

    attributes = node.get("attributes", {})
    if isinstance(attributes, dict):
        array_dimensions = attributes.get("_ARRAY_DIMENSIONS")
        if isinstance(array_dimensions, (list, tuple)) and all(isinstance(item, str) for item in array_dimensions):
            return tuple(array_dimensions)

    return ()


def validate_projected_layout(
    metadata: dict[str, dict],
    registry: LayoutAdapterRegistry | None = None,
) -> LayoutAdapterResult:
    return (registry or DEFAULT_LAYOUT_ADAPTER_REGISTRY).select(metadata)


def select_projected_layout(
    metadata: dict[str, dict],
    registry: LayoutAdapterRegistry | None = None,
) -> ProjectedLayout:
    result = validate_projected_layout(metadata, registry)
    return result.to_projected_layout()


def select_projected_array_names(metadata: dict[str, dict]) -> tuple[str, str]:
    result = ProjectedBanded4DAdapter().validate(metadata)
    if result is None or not result.accepted or result.data_array_name is None or result.band_array_name is None:
        raise ValueError("Dataset does not expose a supported projected 4D array with dims time/*/y/x")
    return result.data_array_name, result.band_array_name


def register_layout_adapter(adapter: LayoutAdapter) -> None:
    DEFAULT_LAYOUT_ADAPTER_REGISTRY.register(adapter)


def _unsupported_dimension_order(
    contract: LayoutAdapterContract,
    dimensions: tuple[str, ...],
) -> LayoutAdapterResult:
    return LayoutAdapterResult(
        contract=contract,
        accepted=False,
        matched_dimensions=dimensions,
        issues=(
            LayoutIssue(
                code="unsupported_dimension_order",
                message=f"Dimensions {dimensions!r} do not match {', '.join(contract.accepted_dimensions)}.",
                remediation="Use trailing y/x spatial dimensions and put time first when a time axis exists.",
            ),
        ),
    )


DEFAULT_LAYOUT_ADAPTER_REGISTRY = LayoutAdapterRegistry(
    [
        ProjectedBanded4DAdapter(),
        ProjectedTimeVariable3DAdapter(),
        ProjectedStaticVariable2DAdapter(),
        GeographicLatLonAdapter(),
        AmbiguousDimensionsAdapter(),
    ]
)
