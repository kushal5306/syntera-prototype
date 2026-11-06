"""The tools the chat can call, each a thin wrapper over an existing deterministic workflow.

* ``design_plant``: a plant described in plain terms (equipment and what connects to what)
  becomes a P&ID-like connectivity document, which the P&ID builder lays out, routes,
  verifies and audits as one assembly.
* ``check_drawing``: an uploaded DWG/DXF piping drawing is read, rebuilt in 3D, checked,
  and every line is rerouted by Syntera for comparison.
* ``pid_to_3d``: an uploaded DEXPI P&ID becomes a proposed, routed 3D layout.
* ``audit_step``: an uploaded STEP assembly is audited for clashes, clearances and bends.
* ``use_sample``: puts a sample drawing into the conversation, for people with no file.

Each tool returns a compact result for the model (with a ``plain_summary`` written from
the checks, never from the model) and, where there is something to look at, a view: a
headline card plus 3D meshes stored on disk for the browser.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Progress = Callable[[str], None]
FILE_ID = re.compile(r"^f[0-9a-f]{16}$")
VIEW_ID = re.compile(r"^v[0-9a-f]{16}$")
MAX_CHAT_UPLOAD_BYTES = 100 * 1024 * 1024
FILE_KINDS = {".dxf": "drawing", ".dwg": "drawing", ".xml": "pid", ".step": "step", ".stp": "step"}
OUTSIDE = "outside"


class ChatToolError(ValueError):
    """A tool call that cannot be carried out; the message goes back to the model."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- files and views -------------------------------------------------------------------


@dataclass(frozen=True)
class ChatFile:
    id: str
    filename: str
    kind: Literal["drawing", "pid", "step"]
    path: Path
    size: int

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "filename": self.filename, "kind": self.kind, "size": self.size}


def safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip() or "upload"
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120]


def file_kind(filename: str) -> Literal["drawing", "pid", "step"]:
    kind = FILE_KINDS.get(Path(filename).suffix.lower())
    if kind is None:
        raise ChatToolError(
            "Syntera reads DWG or DXF drawings, DEXPI P&ID exports (.xml) and STEP models "
            "(.step, .stp)."
        )
    return kind  # type: ignore[return-value]


@dataclass
class ChatWorkspace:
    """Where chat uploads, views and the outputs of each workflow are kept."""

    output_root: Path
    examples: Path | None = None
    sample_builders: dict[str, Callable[[Path], Path]] = field(default_factory=dict)

    @property
    def files_root(self) -> Path:
        return self.output_root / "chat" / "files"

    @property
    def views_root(self) -> Path:
        return self.output_root / "chat" / "views"

    def store_file(self, data: bytes, filename: str) -> ChatFile:
        name = safe_filename(filename)
        kind = file_kind(name)
        if not data:
            raise ChatToolError("the file is empty")
        if len(data) > MAX_CHAT_UPLOAD_BYTES:
            raise ChatToolError("the file is larger than 100 MB")
        file_id = "f" + hashlib.sha256(data).hexdigest()[:16]
        folder = self.files_root / file_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if not target.is_file():
            target.write_bytes(data)
        return ChatFile(file_id, name, kind, target, len(data))

    def file(self, file_id: str) -> ChatFile:
        if not FILE_ID.match(file_id or ""):
            raise ChatToolError(f"{file_id!r} is not a file id; ids look like f0123456789abcdef")
        folder = self.files_root / file_id
        found = sorted(p for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []
        if not found:
            raise ChatToolError(f"no uploaded file {file_id}")
        path = found[0]
        return ChatFile(file_id, path.name, file_kind(path.name), path, path.stat().st_size)

    def save_view(self, key: str, title: str, card: dict[str, Any], meshes: list[dict]) -> dict:
        view_id = "v" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        self.views_root.mkdir(parents=True, exist_ok=True)
        view = {"id": view_id, "title": title, "card": card}
        (self.views_root / f"{view_id}.json").write_text(
            json.dumps({**view, "meshes": meshes}), encoding="utf-8"
        )
        (self.views_root / f"{view_id}.card.json").write_text(json.dumps(view), encoding="utf-8")
        return view

    def load_view(self, view_id: str) -> dict | None:
        if not VIEW_ID.match(view_id or ""):
            return None
        path = self.views_root / f"{view_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def has_view(self, key: str) -> dict | None:
        """The card of a stored view for ``key`` (without loading its meshes)."""
        view_id = "v" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        card = self.views_root / f"{view_id}.card.json"
        return json.loads(card.read_text(encoding="utf-8")) if card.is_file() else None


@dataclass
class ToolOutcome:
    result: dict[str, Any]
    view: dict[str, Any] | None = None
    files: list[ChatFile] = field(default_factory=list)


def _m(mm: float | None, digits: int = 1) -> float | None:
    return None if mm is None else round(mm / 1000.0, digits)


def _money(value: float | None, currency: str) -> str:
    return "n/a" if value is None else f"{currency} {value:,.0f}"


# ---- design_plant ----------------------------------------------------------------------

EquipmentType = Literal[
    "tank", "vessel", "pump", "compressor", "heat_exchanger", "plate_heat_exchanger",
    "column", "filter", "separator", "drum", "mixer", "other",
]  # fmt: skip
COMPONENT_CLASS = {
    "tank": "Tank",
    "vessel": "Vessel",
    "pump": "CentrifugalPump",
    "compressor": "Compressor",
    "heat_exchanger": "TubularHeatExchanger",
    "plate_heat_exchanger": "PlateHeatExchanger",
    "column": "Column",
    "filter": "Filter",
    "separator": "Separator",
    "drum": "Drum",
    "mixer": "Mixer",
    "other": "Equipment",
}


class EquipmentSpec(StrictModel):
    tag: str = Field(min_length=1, max_length=40, description="short tag, e.g. T-101 or P-1")
    type: EquipmentType
    diameter_mm: float | None = Field(default=None, gt=0, le=20000)
    height_mm: float | None = Field(default=None, gt=0, le=60000)
    capacity_m3: float | None = Field(default=None, gt=0, le=20000)


class LineSpec(StrictModel):
    from_tag: str = Field(min_length=1, max_length=40, description="equipment tag or 'outside'")
    to_tag: str = Field(min_length=1, max_length=40, description="equipment tag or 'outside'")
    dn: int | None = Field(default=None, ge=10, le=800, description="nominal size; 50 if unsaid")
    name: str | None = Field(default=None, max_length=40)


class RoomSpec(StrictModel):
    length_m: float = Field(gt=0, le=500)
    width_m: float = Field(gt=0, le=500)
    height_m: float | None = Field(default=None, gt=0, le=100)


class DesignPlant(StrictModel):
    """Lay out, route, verify and audit a new plant described in plain terms."""

    title: str = Field(default="New plant", min_length=1, max_length=120)
    equipment: list[EquipmentSpec] = Field(min_length=1, max_length=20)
    lines: list[LineSpec] = Field(min_length=1, max_length=30)
    room: RoomSpec | None = None


def _equipment_id(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", tag).strip("-") or "item"


def plant_document(spec: DesignPlant):
    """The plain-language spec as a P&ID connectivity document (no positions)."""
    from syntera.pid.models import PidConnection, PidDocument, PidEquipment, PidNozzle, Terminal

    tags: dict[str, EquipmentSpec] = {}
    for item in spec.equipment:
        key = item.tag.strip().lower()
        if key == OUTSIDE:
            raise ChatToolError("'outside' is reserved for pipes that leave the plant")
        if key in tags:
            raise ChatToolError(f"equipment tag {item.tag} is used twice")
        tags[key] = item
    nozzles: dict[str, list[Any]] = {key: [] for key in tags}
    connections = []
    for index, line in enumerate(spec.lines, start=1):
        name = line.name or f"L{index}"
        ends = []
        for side, raw in (("start", line.from_tag), ("end", line.to_tag)):
            key = raw.strip().lower()
            if key == OUTSIDE:
                ends.append(Terminal(id=f"BL-{index}-{side}", kind="battery_limit",
                                     label=f"{name} {'inlet' if side == 'start' else 'outlet'}"
                                     " at the plant edge", inbound=side == "start"))  # fmt: skip
                continue
            if key not in tags:
                known = ", ".join(item.tag for item in spec.equipment)
                raise ChatToolError(f"line {name} names {raw}, which is not equipment ({known})")
            item = tags[key]
            number = len(nozzles[key]) + 1
            nozzle_id = f"{_equipment_id(item.tag)}-N{number}"
            nozzles[key].append(PidNozzle(id=nozzle_id, tag=f"N{number}"))
            ends.append(Terminal(id=nozzle_id, kind="nozzle", equipment_id=_equipment_id(item.tag),
                                 label=f"{item.tag} N{number}"))  # fmt: skip
        if ends[0].kind == ends[1].kind == "battery_limit":
            raise ChatToolError(f"line {name} must connect to at least one piece of equipment")
        if ends[0].equipment_id and ends[0].equipment_id == ends[1].equipment_id:
            raise ChatToolError(f"line {name} starts and ends at {line.from_tag}")
        connections.append(PidConnection(id=f"line-{index}", line=name, nominal_diameter=line.dn,
                                         start=ends[0], end=ends[1], via=[]))  # fmt: skip
    equipment = []
    for key, item in tags.items():
        attributes: dict[str, float] = {}
        if item.diameter_mm:
            attributes["OutsideDiameter"] = item.diameter_mm
        if item.height_mm:
            attributes["Height"] = item.height_mm
            if item.type in {"tank", "vessel"}:
                attributes["CylinderLength"] = item.height_mm
        if item.capacity_m3:
            attributes["NominalCapacity(Volume)"] = item.capacity_m3 * 1e9
        equipment.append(PidEquipment(id=_equipment_id(item.tag), tag=item.tag,
                                      component_class=COMPONENT_CLASS[item.type],
                                      nozzles=nozzles[key], attributes=attributes))  # fmt: skip
    if len({e.id for e in equipment}) != len(equipment):
        raise ChatToolError("two equipment tags differ only in punctuation; make them distinct")
    payload = json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    return PidDocument(
        source_sha256=hashlib.sha256(payload).hexdigest(),
        filename=spec.title,
        originating_system="Syntera chat",
        equipment=equipment,
        connections=connections,
        skipped=[],
    )


def _room_fit(workspace: tuple[float, float, float], room: RoomSpec | None) -> dict | None:
    if room is None:
        return None
    need = sorted([workspace[0] / 1000, workspace[1] / 1000])
    have = sorted([room.length_m, room.width_m])
    fits = need[0] <= have[0] + 1e-6 and need[1] <= have[1] + 1e-6
    note = None
    if room.height_m is not None and workspace[2] / 1000 > room.height_m + 1e-6:
        fits, note = False, f"needs {workspace[2] / 1000:.1f} m headroom"
    return {
        "fits": fits,
        "room_m": [room.length_m, room.width_m, room.height_m],
        "needed_m": [
            round(workspace[0] / 1000, 1),
            round(workspace[1] / 1000, 1),
            round(workspace[2] / 1000, 1),
        ],  # fmt: skip
        "note": note
        or "The needed area includes 1.5 m access space around the equipment on every side.",
    }


def plant_summary(result, plant_id: str, room: RoomSpec | None, user_sized: set[str]) -> dict:
    report = result.report
    audit = report.audit
    currency = report.currency
    failing = [r for r in (audit.rules if audit else []) if r.status == "fail"]
    problems = [
        {"message": f.message, "location_mm": list(f.location) if f.location else None}
        for rule in failing
        for f in rule.findings
    ][:8]
    routes = [
        {
            "line": r.line,
            "connects": r.label,
            "outer_diameter_mm": r.outer_diameter_mm,
            "routed": r.routed,
            "length_m": _m(r.length_mm, 2),
            "bends": r.bends,
            "estimated_cost": r.estimated_cost,
            "problems": r.failure_reasons[:3],
        }
        for r in report.routes
    ]
    passes = report.failed == 0 and (audit is None or audit.overall_pass)
    fit = _room_fit(report.workspace_mm, room)
    equipment = [
        {
            "tag": e.tag,
            "type": e.component_class,
            "size_mm": [round(v) for v in e.size],
            "size_from": "your input" if e.tag in user_sized and e.size_source == "pid"
            else "typical size (assumed)",
        }
        for e in report.equipment
    ]  # fmt: skip
    length = report.total_pipe_length_mm
    sentences = [
        (
            f"All {report.routed} pipe runs were routed and every check passed."
            if passes
            else f"{report.routed} of {report.routed + report.failed} pipe runs were routed; "
            f"the checks found {len(problems) or report.failed} problem(s)."
        ),
        f"Total pipe length {length / 1000:.1f} m with {report.total_bends} bends, "
        f"estimated cost {_money(report.total_cost, currency)} (placeholder rates).",
        f"The layout needs {report.workspace_mm[0] / 1000:.1f} m x "
        f"{report.workspace_mm[1] / 1000:.1f} m including access space.",
    ]
    if fit is not None:
        sentences.append("It fits the room." if fit["fits"] else "It does not fit the room.")
    defaulted = [e["tag"] for e in equipment if e["size_from"] != "your input"]
    if defaulted:
        sentences.append(f"Typical sizes were assumed for {', '.join(defaulted)}.")
    return {
        "verdict": "pass" if passes else "fail",
        "verdict_source": "Syntera's exact geometry checks and whole-plant design audit",
        "equipment": equipment,
        "lines": routes,
        "totals": {
            "main_piping_length_m": round(length / 1000, 2),
            "bends": report.total_bends,
            "estimated_cost": report.total_cost,
            "currency": currency,
            "routed": report.routed,
            "failed": report.failed,
        },
        "layout_size_m": [round(v / 1000, 1) for v in report.workspace_mm],
        "footprint_m2": report.footprint_m2,
        "room_fit": fit,
        "design_audit": {
            "passes": audit.overall_pass if audit else None,
            "summary": audit.summary if audit else "not run",
            "problems": problems,
        },
        "rerouting_tried": [t.strategy for t in report.optimisation],
        "assumptions": report.assumptions,
        "downloads": [
            {"label": "3D model (STEP)", "url": f"/api/pid/{plant_id}/plant_assembly.step"},
            {"label": "Layout report (JSON)", "url": f"/api/pid/{plant_id}/plant_layout.json"},
            {"label": "Why each value (JSON)", "url": f"/api/pid/{plant_id}/rationale.json"},
        ],
        "plain_summary": " ".join(sentences),
    }


def plant_card(summary: dict) -> dict:
    totals = summary["totals"]
    stats = [
        {"label": "Pipe length", "value": f"{totals['main_piping_length_m']:.1f} m"},
        {"label": "Bends", "value": str(totals["bends"])},
        {"label": "Est. cost", "value": _money(totals["estimated_cost"], totals["currency"])},
        {"label": "Layout", "value": " x ".join(f"{v:g}" for v in summary["layout_size_m"][:2])
         + " m"},
    ]  # fmt: skip
    if summary["room_fit"] is not None:
        stats.append(
            {"label": "Fits room", "value": "yes" if summary["room_fit"]["fits"] else "no"}
        )
    issues = [
        {"title": p["message"], "fix": "", "point_mm": p["location_mm"]}
        for p in summary["design_audit"]["problems"]
    ]
    issues += [
        {"title": f"{line['line']} could not be routed", "fix": "; ".join(line["problems"])}
        for line in summary["lines"]
        if not line["routed"]
    ]
    return {
        "verdict": summary["verdict"],
        "headline": summary["plain_summary"],
        "stats": stats,
        "issues": issues[:10],
        "links": summary["downloads"],
    }


def _design_plant(ws: ChatWorkspace, call: DesignPlant, progress: Progress) -> ToolOutcome:
    from syntera.pid.build import build_plant
    from syntera.web.pid import plant_meshes

    document = plant_document(call)
    plant_id = document.source_sha256[:16]
    progress(f"Laying out {len(call.equipment)} equipment items and routing "
             f"{len(call.lines)} pipe runs…")  # fmt: skip
    folder = ws.output_root / "pid" / plant_id
    result = build_plant(document, folder / "plant")
    (folder / "chat_request.json").write_text(
        json.dumps(call.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sized = {e.tag for e in call.equipment if e.diameter_mm or e.height_mm or e.capacity_m3}
    summary = plant_summary(result, plant_id, call.room, sized)
    view = ws.save_view(f"plant:{plant_id}", call.title, plant_card(summary), plant_meshes(result))
    return ToolOutcome(summary, view)


# ---- pid_to_3d -------------------------------------------------------------------------


class FileCall(StrictModel):
    file_id: str = Field(description="id of an uploaded file, e.g. f0123456789abcdef")


class PidTo3D(FileCall):
    """Turn an uploaded DEXPI P&ID (.xml) into a proposed, routed and audited 3D layout."""


def _pid_to_3d(ws: ChatWorkspace, call: PidTo3D, progress: Progress) -> ToolOutcome:
    from syntera.pid.build import build_plant
    from syntera.pid.dexpi import PidError, read_dexpi
    from syntera.web.pid import plant_meshes

    chat_file = ws.file(call.file_id)
    if chat_file.kind != "pid":
        raise ChatToolError(f"{chat_file.filename} is not a P&ID export (.xml)")
    data = chat_file.path.read_bytes()
    try:
        document = read_dexpi(data, chat_file.filename)
    except PidError as error:
        raise ChatToolError(str(error)) from error
    plant_id = document.source_sha256[:16]
    progress(f"Placing {len(document.equipment)} equipment items and routing "
             f"{len(document.connections)} lines from {chat_file.filename}…")  # fmt: skip
    folder = ws.output_root / "pid" / plant_id
    result = build_plant(document, folder / "plant")
    (folder / "source.xml").write_bytes(data)
    summary = plant_summary(result, plant_id, None, set())
    summary["read_from_pid"] = {
        "equipment": len(document.equipment),
        "lines": len(document.connections),
        "skipped": document.skipped[:10],
    }
    view = ws.save_view(f"pid:{plant_id}", chat_file.filename, plant_card(summary),
                        plant_meshes(result))  # fmt: skip
    return ToolOutcome(summary, view)


# ---- check_drawing ---------------------------------------------------------------------


class CheckDrawing(FileCall):
    """Read an uploaded DWG/DXF piping drawing, rebuild it in 3D, check it, reroute each line."""

    pipe_elevation_mm: float | None = Field(
        default=None, gt=0, le=50000, description="only if the drawing gives no levels"
    )
    equipment_height_mm: float | None = Field(default=None, gt=0, le=50000)


def _model_point(report, where) -> list[float] | None:
    """An issue's drawing coordinates as 3D model coordinates (for the viewer marker)."""
    if where is None or report.drawing_origin_mm is None:
        return None
    origin = report.drawing_origin_mm
    return [round(where.x - origin[0], 1), round(where.y - origin[1], 1), round(where.el, 1)]


def drawing_summary(report, drawing_id: str) -> dict:
    audit = report.audit
    currency = report.currency
    issues = [
        {
            "title": issue.title,
            "what": issue.what,
            "fix": issue.fix,
            "lines": issue.lines,
            "where_on_drawing": (
                f"x {issue.where.x:.0f}, y {issue.where.y:.0f}, EL +{issue.where.el / 1000:.3f}"
                if issue.where
                else None
            ),
            "model_point_mm": _model_point(report, issue.where),
        }
        for issue in report.issues
    ]
    savings = sorted(
        (line for line in report.lines if (line.length_saving_mm or 0) > 1.0),
        key=lambda line: -(line.length_saving_mm or 0),
    )
    better = [
        {
            "line": line.line_number or line.name,
            "shorter_by_m": _m(line.length_saving_mm, 2),
            "shorter_by_percent": line.length_saving_percent,
            "fewer_bends": line.bend_saving,
            "cost_saving": line.cost_saving,
            "why": line.route_rationale.summary if line.route_rationale else None,
        }
        for line in savings
    ]
    passes = report.supported and not issues and (audit is None or audit.overall_pass)
    verdict = "unsupported" if not report.supported else ("pass" if passes else "fail")
    drawn = report.total_drawn_length_mm
    saved_mm = sum(line.length_saving_mm or 0 for line in savings)
    # money from the same lines as the length, so the two figures describe one change
    saved_cost = sum(max(0.0, line.cost_saving or 0) for line in savings)
    sentences = [report.message]
    if report.supported:
        sentences.append(
            "The checks found no problems." if passes
            else f"The checks found {len(issues) or 'some'} problem(s) in the drawing."
        )  # fmt: skip
        if drawn:
            sentences.append(f"The drawing has {drawn / 1000:.1f} m of main piping.")
        if better:
            sentences.append(
                f"Syntera found shorter verified routes for {len(better)} line(s), "
                f"{saved_mm / 1000:.1f} m less pipe in total"
                + (f", about {_money(saved_cost, currency)} saved." if saved_cost else ".")
            )  # fmt: skip
    return {
        "verdict": verdict,
        "verdict_source": "Syntera's exact geometry checks and whole-plant design audit",
        "what_was_read": report.message,
        "problems": issues[:12],
        "lines_checked": len(report.lines),
        "shorter_routes": better[:10],
        "totals": {
            "drawn_main_piping_m": _m(drawn, 2),
            "syntera_main_piping_m": _m(report.total_optimised_length_mm, 2),
            "pipe_saved_m": round(saved_mm / 1000, 2),
            "cost_saving": round(saved_cost, 2),
            "net_cost_change_all_lines": report.total_cost_saving,
            "currency": currency,
        },
        "design_audit": {
            "passes": audit.overall_pass if audit else None,
            "summary": audit.summary if audit else "not run",
        },
        "assumptions": [f"{a.quantity}: {a.value} ({a.source})" for a in report.assumptions][:8],
        "expert_view": f"/drawings#{drawing_id}",
        "downloads": [
            {"label": "Open in the drawings page", "url": f"/drawings#{drawing_id}"},
            {
                "label": "Check report (JSON)",
                "url": f"/api/drawings/{drawing_id}/assembly/piping_check.json",
            },
            {
                "label": "As-drawn 3D (STEP)",
                "url": f"/api/drawings/{drawing_id}/assembly/drawn_plant.step",
            },
        ],  # fmt: skip
        "plain_summary": " ".join(sentences),
    }


def drawing_card(summary: dict) -> dict:
    totals = summary["totals"]
    stats = [
        {"label": "Problems", "value": str(len(summary["problems"]))},
        {"label": "Lines", "value": str(summary["lines_checked"])},
    ]
    if totals["drawn_main_piping_m"] is not None:
        stats.append({"label": "Drawn piping", "value": f"{totals['drawn_main_piping_m']:.1f} m"})
    if totals["pipe_saved_m"]:
        stats.append({"label": "Pipe saved", "value": f"{totals['pipe_saved_m']:.1f} m"})
    if totals["cost_saving"]:
        stats.append({"label": "Saving", "value": _money(totals["cost_saving"],
                                                         totals["currency"])})  # fmt: skip
    issues = [
        {
            "title": p["title"],
            "fix": p["fix"],
            "where": p["where_on_drawing"],
            "point_mm": p["model_point_mm"],
        }  # fmt: skip
        for p in summary["problems"]
    ]
    return {
        "verdict": summary["verdict"],
        "headline": summary["plain_summary"],
        "stats": stats,
        "issues": issues[:10],
        "links": summary["downloads"],
    }


def _check_drawing(ws: ChatWorkspace, call: CheckDrawing, progress: Progress) -> ToolOutcome:
    from syntera.drawings.convert import DrawingError
    from syntera.drawings.intake import DXF_FILE, ingest_drawing, load_summary
    from syntera.drawings.models import AssemblyOptions, PipingCheckReport
    from syntera.drawings.piping_check import ASSEMBLY_DIR, REPORT_FILE, check_drawing
    from syntera.drawings.reader import load_dxf
    from syntera.web.drawings import assembly_meshes

    chat_file = ws.file(call.file_id)
    if chat_file.kind != "drawing":
        raise ChatToolError(f"{chat_file.filename} is not a DWG or DXF drawing")
    root = ws.output_root / "drawings"
    progress(f"Reading {chat_file.filename}…")
    try:
        summary = ingest_drawing(chat_file.path.read_bytes(), chat_file.filename, root)
    except DrawingError as error:
        raise ChatToolError(str(error)) from error
    options = AssemblyOptions(pipe_elevation_mm=call.pipe_elevation_mm,
                              equipment_height_mm=call.equipment_height_mm)  # fmt: skip
    folder = root / summary.drawing_id
    key = f"drawing:{summary.drawing_id}:{options.model_dump_json()}"
    report_path = folder / ASSEMBLY_DIR / REPORT_FILE
    cached = ws.has_view(key)
    if cached is not None and report_path.is_file():
        progress("Using the check Syntera already ran on this exact drawing…")
        report = PipingCheckReport.model_validate_json(report_path.read_bytes())
        result_summary = drawing_summary(report, summary.drawing_id)
        return ToolOutcome(result_summary, cached)
    progress(f"Rebuilding {chat_file.filename} in 3D, checking every line and looking for "
             "shorter routes. Large drawings take a few minutes…")  # fmt: skip
    try:
        doc, _, _ = load_dxf(folder / DXF_FILE)
        result = check_drawing(doc, load_summary(folder), folder, options)
    except (DrawingError, ValueError) as error:
        raise ChatToolError(str(error)) from error
    result_summary = drawing_summary(result.report, summary.drawing_id)
    view = ws.save_view(key, chat_file.filename, drawing_card(result_summary),
                        assembly_meshes(result))  # fmt: skip
    return ToolOutcome(result_summary, view)


# ---- audit_step ------------------------------------------------------------------------


class AuditStep(FileCall):
    """Audit an uploaded STEP assembly: clashes, clearances, bend radii, open pipe ends."""


def _audit_step(ws: ChatWorkspace, call: AuditStep, progress: Progress) -> ToolOutcome:
    from syntera.geometry.step_import import StepImportError
    from syntera.verification.step_audit import audit_step
    from syntera.web.audit import audit_meshes

    chat_file = ws.file(call.file_id)
    if chat_file.kind != "step":
        raise ChatToolError(f"{chat_file.filename} is not a STEP model")
    progress(f"Auditing every solid in {chat_file.filename}…")
    try:
        result = audit_step(chat_file.path)
    except StepImportError as error:
        raise ChatToolError(str(error)) from error
    audit = result.audit
    rules = [
        {
            "check": rule.title,
            "status": rule.status,
            "findings": [
                {
                    "message": f.message,
                    "parts": f.parts,
                    "measured_mm": f.measured_mm,
                    "required_mm": f.required_mm,
                    "location_mm": list(f.location) if f.location else None,
                }  # fmt: skip
                for f in rule.findings[:5]
            ],
        }
        for rule in audit.rules
        if rule.status != "pass"
    ]
    failing = [rule for rule in rules if rule["status"] == "fail"]
    summary = {
        "verdict": "pass" if audit.overall_pass else "fail",
        "verdict_source": "Syntera's whole-assembly design audit",
        "pipes": result.pipes,
        "equipment": result.equipment,
        "pipe_sizes_mm": list(result.sizes_mm),
        "summary": audit.summary,
        "not_passing": rules,
        "plain_summary": (
            f"Syntera found {result.pipes} pipe(s) and {result.equipment} other solid(s). "
            + (
                "Every check passed."
                if audit.overall_pass
                else f"{len(failing)} check(s) failed: {audit.summary}."
            )
        ),  # fmt: skip
    }
    card = {
        "verdict": summary["verdict"],
        "headline": summary["plain_summary"],
        "stats": [
            {"label": "Pipes", "value": str(result.pipes)},
            {"label": "Other solids", "value": str(result.equipment)},
            {"label": "Failed checks", "value": str(len(failing))},
        ],
        "issues": [
            {"title": f["message"], "fix": "", "point_mm": f["location_mm"]}
            for rule in failing
            for f in rule["findings"]
        ][:10],
        "links": [],
    }
    view = ws.save_view(f"step:{chat_file.id}", chat_file.filename, card, audit_meshes(result))
    return ToolOutcome(summary, view)


# ---- use_sample ------------------------------------------------------------------------


class UseSample(StrictModel):
    """Add a sample file to the conversation for someone who has none of their own."""

    sample: Literal["plant_room_drawing"] = Field(
        description="plant_room_drawing: a 14-line solvent recovery plant-room piping GA (DXF)"
    )


def plant_room_builder(examples: Path) -> Callable[[Path], Path] | None:
    script = examples / "plant_room_drawing.py"
    if not script.is_file():
        return None

    def build(target: Path) -> Path:
        spec = importlib.util.spec_from_file_location("syntera_plant_room_sample", script)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module.build(target)

    return build


def _use_sample(ws: ChatWorkspace, call: UseSample, progress: Progress) -> ToolOutcome:
    builder = ws.sample_builders.get(call.sample)
    if builder is None and ws.examples is not None:
        builder = plant_room_builder(ws.examples)
    if builder is None:
        raise ChatToolError("the sample drawing is not installed on this server")
    progress("Drawing the sample plant room…")
    scratch = ws.output_root / "chat" / "samples"
    scratch.mkdir(parents=True, exist_ok=True)
    path = builder(scratch / "plant-room-piping-ga.dxf")
    chat_file = ws.store_file(path.read_bytes(), "plant-room-piping-ga.dxf")
    return ToolOutcome(
        {
            "file": chat_file.public(),
            "about": "Sample piping general arrangement of a solvent recovery plant room: 8 "
            "equipment items, 14 numbered lines with elevations, a plan and a section.",
            "next": "call check_drawing with this file_id",
        },
        files=[chat_file],
    )


# ---- registry --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatTool:
    name: str
    description: str
    input_model: type[StrictModel]
    run: Callable[[ChatWorkspace, Any, Progress], ToolOutcome]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


CHAT_TOOLS: tuple[ChatTool, ...] = (
    ChatTool(
        "design_plant",
        "Design a new plant from a plain description: equipment (tag, type, optional sizes) "
        "and the pipe runs between them (from_tag, to_tag, optional DN; 'outside' for a pipe "
        "entering or leaving the plant). Syntera places the equipment, routes every pipe, "
        "verifies each route and audits the whole plant. Takes about 10-60 s.",
        DesignPlant,
        _design_plant,
    ),
    ChatTool(
        "check_drawing",
        "Check an uploaded DWG/DXF piping drawing: reads equipment, pipe runs, line numbers "
        "and elevations, rebuilds it in 3D, finds clashes and unbuildable bends, and "
        "reroutes each line to find verified savings. Can take several minutes.",
        CheckDrawing,
        _check_drawing,
    ),
    ChatTool(
        "pid_to_3d",
        "Turn an uploaded DEXPI P&ID export (.xml) into a proposed 3D layout with every line "
        "routed, verified and audited.",
        PidTo3D,
        _pid_to_3d,
    ),
    ChatTool(
        "audit_step",
        "Audit an uploaded STEP 3D model for clashes, clearances, bend radii and open ends.",
        AuditStep,
        _audit_step,
    ),
    ChatTool(
        "use_sample",
        "Add Syntera's sample plant-room piping drawing to the conversation (returns a "
        "file_id to pass to check_drawing). Use when the person wants to try Syntera "
        "without a file of their own.",
        UseSample,
        _use_sample,
    ),
)
CHAT_TOOLS_BY_NAME = {tool.name: tool for tool in CHAT_TOOLS}


def chat_tool_schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in CHAT_TOOLS]


def run_chat_tool(
    ws: ChatWorkspace, name: str, arguments: dict[str, Any], progress: Progress
) -> ToolOutcome:
    tool = CHAT_TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ChatToolError(f"unknown tool {name}; tools: {sorted(CHAT_TOOLS_BY_NAME)}")
    try:
        call = tool.input_model.model_validate(arguments)
    except ValidationError as error:
        raise ChatToolError(f"invalid arguments for {name}: {error}") from error
    return tool.run(ws, call, progress)
