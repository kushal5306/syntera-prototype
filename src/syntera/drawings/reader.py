"""Read a DXF drawing into a deterministic summary and an SVG view."""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox, recover, units
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg
from ezdxf.document import Drawing
from ezdxf.math import BoundingBox2d

from syntera.drawings.convert import DrawingError
from syntera.drawings.models import (
    DimensionNote,
    DrawingUnits,
    LayerSummary,
    TextNote,
)

MAX_NOTES = 500
# Bump when the SVG rendering changes so stored drawings are re-rendered on next access.
RENDER_VERSION = 2
PAPER = "#ffffff"
# Drawing colours are darkened until they reach this contrast ratio against white paper,
# so white, yellow and cyan CAD colours stay legible (WCAG 2.1 non-text contrast).
MIN_CONTRAST = 3.0
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b")
# An entity is clipped from the default view when it alone is this many times larger than
# everything else together (typically a stray or mis-scaled block far from the drawing).
OUTLIER_RATIO = 10.0
MAX_OUTLIERS = 5

DIMENSION_KINDS = {
    0: "linear",
    1: "aligned",
    2: "angular",
    3: "diameter",
    4: "radius",
    5: "angular_3p",
    6: "ordinate",
}

Extents = tuple[tuple[float, float], tuple[float, float]]


def load_dxf(path: Path) -> tuple[Drawing, int, int]:
    """Load a DXF with ezdxf's recovery mode; return the document and audit counts."""
    previous = logging.root.manager.disable
    logging.disable(logging.WARNING)  # recovery logs every repaired handle
    try:
        doc, auditor = recover.readfile(path)
    except (OSError, ezdxf.DXFStructureError, ValueError) as error:
        raise DrawingError(f"drawing could not be read as DXF: {error}") from error
    finally:
        logging.disable(previous)
    return doc, len(auditor.errors), len(auditor.fixes)


def drawing_units(doc: Drawing) -> DrawingUnits:
    code = int(doc.header.get("$INSUNITS", 0))
    try:
        name = units.unit_name(code)
        factor = units.conversion_factor(code, units.MM) if code else None
    except (KeyError, IndexError):
        name, factor = f"Unknown ({code})", None
    return DrawingUnits(code=code, name=name, mm_per_unit=factor)


def _extents(box: BoundingBox2d) -> Extents | None:
    if not box.has_data:
        return None
    low, high = box.extmin, box.extmax
    return ((round(low.x, 6), round(low.y, 6)), (round(high.x, 6), round(high.y, 6)))


def view_box(doc: Drawing) -> tuple[BoundingBox2d, BoundingBox2d, list[str]]:
    """Full model-space extents, the default view extents, and handles clipped from view."""
    cache = bbox.Cache()
    boxes: list[tuple[str, BoundingBox2d]] = []
    for entity in doc.modelspace():
        box = bbox.extents([entity], fast=False, cache=cache)
        if box.has_data:
            boxes.append((entity.dxf.handle, BoundingBox2d([box.extmin.vec2, box.extmax.vec2])))
    full = BoundingBox2d()
    for _, box in boxes:
        full.extend([box.extmin, box.extmax])
    by_size = sorted(range(len(boxes)), key=lambda i: (-boxes[i][1].size.magnitude, i))
    clipped: set[int] = set()
    for index in by_size[:MAX_OUTLIERS]:
        rest = BoundingBox2d()
        for other, (_, box) in enumerate(boxes):
            if other != index and other not in clipped:
                rest.extend([box.extmin, box.extmax])
        if not rest.has_data:
            break
        if boxes[index][1].size.magnitude > OUTLIER_RATIO * max(rest.size.magnitude, 1e-9):
            clipped.add(index)
        else:
            break
    view = BoundingBox2d()
    for index, (_, box) in enumerate(boxes):
        if index not in clipped:
            view.extend([box.extmin, box.extmax])
    return full, view, [boxes[index][0] for index in sorted(clipped)]


def layer_summaries(doc: Drawing, counts: Counter[str]) -> list[LayerSummary]:
    layers = []
    for layer in sorted(doc.layers, key=lambda item: item.dxf.name.lower()):
        name = layer.dxf.name
        layers.append(
            LayerSummary(
                name=name,
                color=abs(int(layer.dxf.get("color", 7))),
                visible=layer.is_on(),
                frozen=layer.is_frozen(),
                entity_count=counts.get(name, 0),
            )
        )
    return layers


def _measurement(dimension) -> float | None:
    try:
        value = dimension.get_measurement()
    except (ValueError, TypeError, AttributeError, ZeroDivisionError):
        return None
    if isinstance(value, int | float):
        return round(float(value), 6)
    return round(float(value.magnitude), 6)


def notes(doc: Drawing) -> tuple[list[DimensionNote], list[TextNote], bool]:
    """Dimensions and text in model-space order, capped at ``MAX_NOTES`` each."""
    dimensions: list[DimensionNote] = []
    texts: list[TextNote] = []
    truncated = False
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "DIMENSION":
            if len(dimensions) >= MAX_NOTES:
                truncated = True
                continue
            dimensions.append(
                DimensionNote(
                    handle=entity.dxf.handle,
                    layer=entity.dxf.layer,
                    kind=DIMENSION_KINDS.get(entity.dimtype & 7, "other"),
                    measurement=_measurement(entity),
                    text=entity.dxf.get("text", ""),
                )
            )
        elif kind in {"TEXT", "MTEXT"}:
            if len(texts) >= MAX_NOTES:
                truncated = True
                continue
            content = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
            point = entity.dxf.insert
            texts.append(
                TextNote(
                    handle=entity.dxf.handle,
                    layer=entity.dxf.layer,
                    text=content.strip(),
                    position=(round(point.x, 6), round(point.y, 6)),
                )
            )
    return dimensions, texts, truncated


def _luminance(rgb: tuple[float, float, float]) -> float:
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def legible_on_paper(colour: str) -> str:
    """Darken ``colour`` (#rrggbb) just enough to reach ``MIN_CONTRAST`` on white."""
    rgb = tuple(int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    factor = 1.0
    while (1.05 / (_luminance(tuple(c * factor for c in rgb)) + 0.05)) < MIN_CONTRAST:
        factor -= 0.02
    if factor == 1.0:
        return colour.lower()
    return "#" + "".join(f"{round(c * factor * 255):02x}" for c in rgb)


def _recolour_styles(document: str) -> str:
    """Apply ``legible_on_paper`` to every entity colour class; the page rect stays white."""
    end = document.index("</defs>")
    styles = HEX_COLOUR.sub(lambda match: legible_on_paper(match.group(0)), document[:end])
    return styles + document[end:]


def render_svg(doc: Drawing, view: BoundingBox2d) -> str:
    """Render model space as dark lines on white paper, framed on ``view``."""
    if not view.has_data:
        raise DrawingError(
            "model space has no drawable geometry (paper-space-only drawings are not supported)"
        )
    backend = svg.SVGBackend()
    paper = config.Configuration(
        background_policy=config.BackgroundPolicy.CUSTOM,
        custom_bg_color=PAPER,
        lineweight_policy=config.LineweightPolicy.RELATIVE,
    )
    Frontend(RenderContext(doc), backend, config=paper).draw_layout(doc.modelspace(), finalize=True)
    page = layout.Page(0, 0, layout.Units.mm, layout.Margins.all(4), max_width=1189, max_height=841)
    # Line widths scale with the sheet (heaviest 0.2% of its size); the thinnest lines are
    # kept at half of that so they stay visible when the whole sheet is in view.
    settings = layout.Settings(max_stroke_width=0.002, min_stroke_width=0.5)
    document = backend.get_string(page, settings=settings, render_box=view, xml_declaration=False)
    return _recolour_styles(document)


def svg_page_mm(document: str) -> tuple[float, float]:
    """Page size the SVG was laid out on, which the viewer needs to place markers."""
    found = re.search(r'width="([\d.]+)mm" height="([\d.]+)mm"', document[:500])
    if not found:
        raise DrawingError("rendered SVG has no page size")
    return float(found.group(1)), float(found.group(2))


def read_drawing(path: Path) -> dict:
    """Everything the summary needs from one DXF file, plus its SVG."""
    doc, audit_errors, audit_fixes = load_dxf(path)
    msp = doc.modelspace()
    types = Counter(entity.dxftype() for entity in msp)
    per_layer = Counter(entity.dxf.layer for entity in msp)
    inserts = Counter(entity.dxf.name for entity in msp.query("INSERT"))
    full, view, clipped = view_box(doc)
    dimensions, texts, truncated = notes(doc)
    document = render_svg(doc, view)
    return {
        "dxf_version": doc.dxfversion,
        "units": drawing_units(doc),
        "extents": _extents(full),
        "view_extents": _extents(view),
        "clipped_handles": clipped,
        "entity_count": sum(types.values()),
        "entity_types": dict(sorted(types.items())),
        "layers": layer_summaries(doc, per_layer),
        "block_inserts": dict(sorted(inserts.items())),
        "dimensions": dimensions,
        "texts": texts,
        "truncated": truncated,
        "audit_errors": audit_errors,
        "audit_fixes": audit_fixes,
        "render_version": RENDER_VERSION,
        "svg_page_mm": svg_page_mm(document),
        "svg": document,
    }
