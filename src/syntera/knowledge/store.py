"""Schema, registry and loader for knowledge entries."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ENTRIES_DIR = Path(__file__).with_name("entries")
KNOWLEDGE_VERSION = 1
# Paraphrase, not reproduction: entries stay short.
MAX_SUMMARY_CHARS = 400
MAX_DETAIL_CHARS = 1200
MAX_QUOTE_WORDS = 12

# Every rule id Syntera writes into rationale records. Entries may only link these.
KNOWN_RULE_IDS: frozenset[str] = frozenset(
    {
        "design.largest_bore_first.v1",
        "drawing.bend_radius_at_least_tube_radius.v1",
        "drawing.bend_radius_long_radius.v1",
        "drawing.clearance_half_od.v1",
        "drawing.routing_weights_from_grid.v1",
        "drawing.sheet_and_scale.v1",
        "grid.finest_standard_step.v1",
        "pid.bend_radius_long_radius.v1",
        "pid.class_default_envelope.v1",
        "pid.clearance_half_od_min_25.v1",
        "pid.grid_placement_swaps.v1",
        "pid.outside_diameter_from_nominal_size.v1",
        "pid.plate_frame_allowance.v1",
        "pid.routing_weights_from_grid.v1",
        "pid.shell_from_tube_length.v1",
        "pid.skid_workspace.v1",
        "pid.vessel_diameter_from_capacity.v1",
        "rules.allowable_stress_from_yield.v1",
        "rules.bend_radius_long_radius.v1",
        "rules.clearance_half_od_with_floor.v1",
        "rules.flow_velocity_from_flow_and_bore.v1",
        "rules.material_by_medium_temperature.v1",
        "rules.maximum_velocity_by_medium.v1",
        "rules.minimum_inner_diameter_from_velocity.v1",
        "rules.spool_split_max_length.v1",
        "rules.support_placement_max_span.v1",
        "rules.support_span_deflection.v1",
        "rules.tube_size_from_flow.v1",
        "rules.wall_thickness_hoop_stress.v1",
    }
)

EntryId = Annotated[str, Field(pattern=r"^kb\.[a-z]+\.[a-z0-9_]+$")]
EntryKind = Literal["rule", "material", "practice", "check"]


class KnowledgeError(ValueError):
    """A knowledge entry or store that cannot be used."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(StrictModel):
    """Where an idea comes from, described in Syntera's words (never quoted at length)."""

    kind: Literal["textbook", "standard", "practice", "synthetic"]
    source: str = Field(min_length=3, max_length=200)
    note: str = Field(default="", max_length=300)


class KnowledgeEntry(StrictModel):
    id: EntryId
    kind: EntryKind
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=10, max_length=MAX_SUMMARY_CHARS)
    detail: str = Field(min_length=10, max_length=MAX_DETAIL_CHARS)
    rules: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = Field(min_length=1)
    synthetic: bool
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def check_entry(self) -> KnowledgeEntry:
        unknown = sorted(set(self.rules) - KNOWN_RULE_IDS)
        if unknown:
            raise ValueError(f"{self.id}: unknown rule ids {unknown}")
        if self.kind == "rule" and not self.rules:
            raise ValueError(f"{self.id}: a rule entry must link at least one rule id")
        if self.kind == "material" and not self.materials:
            raise ValueError(f"{self.id}: a material entry must name its material")
        if self.synthetic and not any(c.kind == "synthetic" for c in self.citations):
            raise ValueError(f"{self.id}: synthetic entries must cite the synthetic table")
        for text in (self.summary, self.detail):
            for quoted in _quotations(text):
                if len(quoted.split()) > MAX_QUOTE_WORDS:
                    raise ValueError(f"{self.id}: quotations longer than {MAX_QUOTE_WORDS} "
                                     "words are not allowed; paraphrase instead")  # fmt: skip
        return self


def _quotations(text: str) -> list[str]:
    parts = text.split('"')
    return parts[1::2]


class KnowledgeBase(StrictModel):
    version: int = KNOWLEDGE_VERSION
    entries: tuple[KnowledgeEntry, ...]

    def get(self, entry_id: str) -> KnowledgeEntry:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        raise KnowledgeError(f"unknown knowledge entry {entry_id}")

    def for_rule(self, rule_id: str) -> list[KnowledgeEntry]:
        return [entry for entry in self.entries if rule_id in entry.rules]

    def for_material(self, name: str) -> list[KnowledgeEntry]:
        return [entry for entry in self.entries if name in entry.materials]


def load_knowledge(directory: Path = ENTRIES_DIR) -> KnowledgeBase:
    """Load and validate every ``*.yaml`` entry (one or more per file), sorted by id."""
    entries: list[KnowledgeEntry] = []
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise KnowledgeError(f"{directory}: no knowledge entries")
    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise KnowledgeError(f"{path.name}: not valid YAML: {error}") from error
        items = raw if isinstance(raw, list) else [raw]
        for index, item in enumerate(items, start=1):
            try:
                entries.append(KnowledgeEntry.model_validate(item))
            except ValidationError as error:
                raise KnowledgeError(f"{path.name} entry {index}: {error}") from error
    ids = [entry.id for entry in entries]
    duplicates = sorted({entry_id for entry_id in ids if ids.count(entry_id) > 1})
    if duplicates:
        raise KnowledgeError(f"duplicate knowledge entry ids: {duplicates}")
    return KnowledgeBase(entries=tuple(sorted(entries, key=lambda entry: entry.id)))
