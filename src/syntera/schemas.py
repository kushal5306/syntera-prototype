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


Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class Material(StrictModel):
    """Linear-elastic isotropic material in the mm-N-s-tonne unit system (stress in MPa)."""

    name: Identifier
    elastic_modulus_mpa: PositiveMm
    poisson_ratio: Annotated[float, Field(gt=0, lt=0.5)]
    yield_strength_mpa: PositiveMm
    density_tonne_per_mm3: Annotated[float, Field(gt=0)] | None = None
    thermal_expansion_per_k: Annotated[float, Field(gt=0)] | None = None


class LoadCase(StrictModel):
    """One independent static load case; both tube ends are clamped at their ports."""

    name: Identifier
    internal_pressure_mpa: Annotated[float, Field(ge=0)] = 0.0
    gravity_mm_per_s2: Point3 | None = None
    temperature_change_k: float = 0.0

    @model_validator(mode="after")
    def must_apply_a_load(self) -> LoadCase:
        gravity = self.gravity_mm_per_s2 is not None and any(self.gravity_mm_per_s2)
        if not (self.internal_pressure_mpa > 0 or gravity or self.temperature_change_k != 0):
            raise ValueError(f"load case '{self.name}' applies no load")
        return self


class MeshControls(StrictModel):
    """Structured quadratic-shell mesh controls for the tube mid-surface."""

    circumferential_elements: Annotated[int, Field(ge=8, le=128)] = 16
    maximum_axial_element_length: PositiveMm | None = None
    minimum_elements_per_bend: Annotated[int, Field(ge=2, le=64)] = 6
    maximum_aspect_ratio: Annotated[float, Field(ge=1)] = 5.0


class AcceptanceThresholds(StrictModel):
    """Deterministic pass limits applied to every load case."""

    stress_safety_factor: Annotated[float, Field(ge=1)] = 1.5
    maximum_displacement_mm: PositiveMm


class AnalysisSpecification(StrictModel):
    wall_thickness: PositiveMm
    material: Material
    load_cases: list[LoadCase] = Field(min_length=1)
    mesh: MeshControls = Field(default_factory=MeshControls)
    acceptance: AcceptanceThresholds

    @model_validator(mode="after")
    def load_cases_must_be_supported(self) -> AnalysisSpecification:
        names = [case.name for case in self.load_cases]
        if len(names) != len(set(names)):
            raise ValueError("load case names must be unique")
        for case in self.load_cases:
            if case.gravity_mm_per_s2 is not None and self.material.density_tonne_per_mm3 is None:
                raise ValueError(f"load case '{case.name}' needs material density for gravity")
            if case.temperature_change_k != 0 and self.material.thermal_expansion_per_k is None:
                raise ValueError(
                    f"load case '{case.name}' needs material thermal expansion for temperature"
                )
        return self


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
    analysis: AnalysisSpecification | None = None

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
        analysis = self.analysis
        if analysis is not None and analysis.wall_thickness >= self.tube.outer_diameter / 2:
            raise ValueError("wall thickness must be less than the tube radius")
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


class PipingMetrics(StrictModel):
    """Centreline quantities of the routed main line; bends are exact circular arcs."""

    main_length_mm: float
    straight_length_mm: float
    bend_length_mm: float
    straights: int
    bends: int


class PackageMetrics(StrictModel):
    """Size of the modelled package: the axis-aligned envelope of every exact solid."""

    envelope_min_mm: Point3
    envelope_max_mm: Point3
    envelope_dimensions_mm: Point3
    envelope_volume_m3: float
    footprint_m2: float
    height_mm: float
    frame_dimensions_mm: Point3
    frame_volume_m3: float
    included_parts: list[str]
    piping: PipingMetrics | None


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


class FeaMeshSummary(StrictModel):
    element_type: Literal["S8R"]
    nodes: int
    elements: int
    axial_elements: int
    circumferential_elements: int
    mid_surface_radius_mm: float
    maximum_aspect_ratio: float


class FeaLoadCaseDeck(StrictModel):
    name: str
    deck_file: str
    result_file: str


class FeaManifest(StrictModel):
    """Provenance and acceptance limits for one set of generated CalculiX decks."""

    deck_format_version: int
    generator: str
    input_sha256: str
    generated: bool
    diagnostic: str | None = None
    mesh_file: str | None = None
    mesh: FeaMeshSummary | None = None
    allowable_von_mises_mpa: float
    maximum_displacement_mm: float
    load_cases: list[FeaLoadCaseDeck] = Field(default_factory=list)


class FeaCaseAcceptance(StrictModel):
    name: str
    maximum_von_mises_mpa: float | None
    allowable_von_mises_mpa: float
    maximum_displacement_mm: float | None
    allowed_displacement_mm: float
    passed: bool
    failure_reasons: list[str]


class FeaAcceptanceReport(StrictModel):
    input_sha256: str
    overall_pass: bool
    load_cases: list[FeaCaseAcceptance]
    failure_reasons: list[str]
