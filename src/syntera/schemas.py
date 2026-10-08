"""Typed engineering input and output contracts (all dimensions in millimetres)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveMm = Annotated[float, Field(gt=0)]
Point3 = tuple[float, float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Workspace(StrictModel):
    units: Literal["mm"]
    dimensions: tuple[PositiveMm, PositiveMm, PositiveMm]


class BoxObstacle(StrictModel):
    type: Literal["box"]
    name: str = Field(min_length=1)
    center: Point3
    size: tuple[PositiveMm, PositiveMm, PositiveMm]


class CylinderObstacle(StrictModel):
    type: Literal["cylinder"]
    name: str = Field(min_length=1)
    center: Point3
    radius: PositiveMm
    height: PositiveMm
    axis: Literal["x", "y", "z"] = "z"


class StepDefeaturing(StrictModel):
    """Conservative simplification applied to imported geometry before routing.

    Defeaturing may only add material: every accepted removal must yield a valid solid that
    fully contains the geometry it replaces, so routing clearance is never overstated.
    """

    max_feature_radius: Annotated[float, Field(ge=0)] = 0.0
    envelope: Literal["exact", "bounding_box"] = "exact"


class StepObstacle(StrictModel):
    """Equipment geometry imported from a STEP file (converted to millimetres on import)."""

    type: Literal["step"]
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    translation: Point3 = (0.0, 0.0, 0.0)
    defeaturing: StepDefeaturing = Field(default_factory=StepDefeaturing)

    @model_validator(mode="after")
    def path_must_name_step_file(self) -> StepObstacle:
        if not self.path.lower().endswith((".step", ".stp")):
            raise ValueError("STEP obstacle path must end in .step or .stp")
        return self


Obstacle = Annotated[BoxObstacle | CylinderObstacle | StepObstacle, Field(discriminator="type")]


class Port(StrictModel):
    position: Point3
    direction: Point3

    @model_validator(mode="after")
    def direction_must_be_cardinal_unit_vector(self) -> Port:
        rounded = tuple(round(value, 9) for value in self.direction)
        if sum(abs(value) > 0 for value in rounded) != 1 or not any(
            abs(abs(value) - 1.0) < 1e-9 for value in rounded
        ):
            raise ValueError("port direction must be a cardinal unit vector")
        return self


class TubeSpecification(StrictModel):
    outer_diameter: PositiveMm
    minimum_bend_radius: PositiveMm
    minimum_clearance: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def bend_radius_must_fit_tube(self) -> TubeSpecification:
        if self.minimum_bend_radius < self.outer_diameter / 2:
            raise ValueError("minimum bend radius must be at least the tube radius")
        return self


class RoutingWeights(StrictModel):
    bend: Annotated[float, Field(ge=0)] = 20.0
    obstacle_proximity: Annotated[float, Field(ge=0)] = 5.0
    port_direction: Annotated[float, Field(ge=0)] = 1000.0


class DemoConfig(StrictModel):
    title: str = Field(min_length=1)
    synthetic_data: Literal[True]
    workspace: Workspace
    obstacles: list[Obstacle]
    start_port: Port
    end_port: Port
    tube: TubeSpecification
    voxel_resolution: PositiveMm
    routing_cost_weights: RoutingWeights = Field(default_factory=RoutingWeights)

    @model_validator(mode="after")
    def geometry_must_be_inside_workspace(self) -> DemoConfig:
        dims = self.workspace.dimensions
        for label, port in (("start", self.start_port), ("end", self.end_port)):
            if any(
                value < 0 or value > limit
                for value, limit in zip(port.position, dims, strict=False)
            ):
                raise ValueError(f"{label} port lies outside workspace")
        if self.start_port.position == self.end_port.position:
            raise ValueError("start and end ports must differ")
        return self


class RouteResult(StrictModel):
    found: bool
    points: list[Point3] = Field(default_factory=list)
    length: float = 0.0
    diagnostic: str | None = None
    expanded_nodes: int = 0


class StepImportSummary(StrictModel):
    """Provenance and defeaturing outcome for one imported STEP obstacle."""

    name: str
    source_path: str
    source_sha256: str
    solid_count: int
    envelope: Literal["exact", "bounding_box"]
    candidate_features: int
    removed_features: int
    rejected_features: int
    original_volume_mm3: float
    routing_volume_mm3: float
    routing_bounding_box_mm: tuple[Point3, Point3]


class ImportReport(StrictModel):
    obstacles: list[StepImportSummary]


class AssuranceReport(StrictModel):
    route_found: bool
    route_length_mm: float | None
    number_of_bends: int
    collision_free: bool
    minimum_measured_clearance_mm: float | None
    required_clearance_mm: float
    minimum_generated_bend_radius_mm: float | None
    required_bend_radius_mm: float
    start_port_alignment: bool
    end_port_alignment: bool
    execution_time_seconds: float
    overall_pass: bool
    failure_reasons: list[str]
