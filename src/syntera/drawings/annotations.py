"""Read piping annotations from a plan drawing: line numbers, pipe sizes and elevations.

Piping general arrangement drawings carry the third dimension and the line size as text
beside each run, not as geometry: a line number such as ``100-P-2001-A1A`` (DN, service,
number, pipe class) and an elevation such as ``CL EL +4.200``. These parsers are strict on
purpose; text that does not match is ignored rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ASME B36.10M / ISO 4200 outside diameters, mm, by DN and by NPS (inches).
OUTSIDE_DIAMETER_BY_DN: dict[int, float] = {
    15: 21.3, 20: 26.7, 25: 33.4, 32: 42.2, 40: 48.3, 50: 60.3, 65: 73.0, 80: 88.9,
    100: 114.3, 125: 141.3, 150: 168.3, 200: 219.1, 250: 273.0, 300: 323.9, 350: 355.6,
    400: 406.4, 450: 457.0, 500: 508.0, 600: 610.0,
}  # fmt: skip
DN_BY_NPS: dict[str, int] = {
    "1/2": 15, "3/4": 20, "1": 25, "1-1/4": 32, "1.25": 32, "1-1/2": 40, "1.5": 40, "2": 50,
    "2-1/2": 65, "2.5": 65, "3": 80, "4": 100, "5": 125, "6": 150, "8": 200, "10": 250,
    "12": 300, "14": 350, "16": 400, "18": 450, "20": 500, "24": 600,
}  # fmt: skip

# DN-SERVICE-NUMBER[-CLASS], e.g. 100-P-2001-A1A, 4"-P-2001-A1A, DN100-CW-0031.
LINE_NUMBER = re.compile(
    r'^(?:DN\s*)?(?P<size>\d+(?:[-/.]\d+(?:/\d+)?)?)\s*(?P<inch>"|IN\b|NB\b)?\s*-\s*'
    r"(?P<service>[A-Z]{1,4})\s*-\s*(?P<number>[A-Z]?\d{2,6}[A-Z]?)"
    r"(?:\s*-\s*(?P<spec>[A-Z0-9]{1,8}))?$"
)
# CL EL +4.200, EL +4200, BOP EL. 3.750, TOP EL 4,350 ...
ELEVATION = re.compile(
    r"^(?:(?P<datum>CL|C\.L\.|C/L|BOP|B\.O\.P\.|TOP|T\.O\.P\.)\s*)?EL\.?\s*"
    r"(?P<value>[+-]?\s*\d+(?:[.,]\d+)?)\s*(?P<unit>MM|M)?$"
)


@dataclass(frozen=True)
class LineNumber:
    text: str
    dn: int | None
    service: str
    outer_diameter_mm: float | None


@dataclass(frozen=True)
class Elevation:
    """An elevation label; ``datum`` says which point of the pipe it gives."""

    text: str
    value_mm: float
    datum: str  # "centreline", "bottom" or "top" of pipe

    def centreline(self, outer_diameter: float) -> float:
        if self.datum == "bottom":
            return self.value_mm + outer_diameter / 2
        if self.datum == "top":
            return self.value_mm - outer_diameter / 2
        return self.value_mm


def parse_line_number(text: str) -> LineNumber | None:
    match = LINE_NUMBER.match(" ".join(text.upper().split()))
    if not match:
        return None
    size = match.group("size")
    if match.group("inch"):
        dn = DN_BY_NPS.get(size)
    else:
        dn = int(size) if size.isdigit() else None
        # A bare small number is an NPS on imperial drawings (2-P-101 is 2 inch, not DN2).
        if dn is not None and dn not in OUTSIDE_DIAMETER_BY_DN:
            dn = DN_BY_NPS.get(size)
    return LineNumber(
        text=match.group(0),
        dn=dn,
        service=match.group("service"),
        outer_diameter_mm=OUTSIDE_DIAMETER_BY_DN.get(dn) if dn else None,
    )


def parse_elevation(text: str) -> Elevation | None:
    """Elevation in mm. A value with a decimal point and no unit is in metres (EL +4.200)."""
    match = ELEVATION.match(" ".join(text.upper().split()))
    if not match:
        return None
    raw = match.group("value").replace(" ", "")
    unit = match.group("unit")
    decimal = "." in raw or "," in raw
    value = float(raw.replace(",", "."))
    if unit == "M" or (unit is None and decimal):
        value *= 1000.0
    datum = match.group("datum") or "CL"
    kind = "bottom" if datum.startswith("B") else "top" if datum.startswith("T") else "centreline"
    return Elevation(text=match.group(0), value_mm=round(value, 6), datum=kind)
