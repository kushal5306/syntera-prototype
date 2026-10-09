"""DWG to DXF conversion through an installed external converter.

DWG is a closed format, so Syntera never parses it directly. The ODA File Converter is used
when installed (highest fidelity, user-installed under ODA's own terms); otherwise LibreDWG's
``dwg2dxf`` (GPLv3, run as a separate program). The order is fixed so results are repeatable.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from syntera.drawings.models import ConversionRecord

CONVERSION_TIMEOUT_S = 120
MAX_WARNINGS = 20

# First six bytes of a DWG file name the format release.
DWG_RELEASES = {
    "MC0.0": "R1.0",
    "AC1.2": "R1.2",
    "AC1.4": "R1.4",
    "AC1.50": "R2.0",
    "AC2.10": "R2.10",
    "AC1001": "R2.5",
    "AC1002": "R2.6",
    "AC1003": "R9",
    "AC1004": "R10",
    "AC1006": "R10",
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "AutoCAD 2000",
    "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}


class DrawingError(ValueError):
    """Raised when a drawing cannot be converted, read or trusted."""


@dataclass(frozen=True)
class Converter:
    name: Literal["oda", "libredwg"]
    executable: str


def dwg_version(data: bytes) -> str:
    """Return the DWG release code from the file header, or fail if it is not a DWG."""
    head = data[:6].decode("ascii", errors="replace")
    for code in sorted(DWG_RELEASES, key=len, reverse=True):
        if head.startswith(code):
            return code
    raise DrawingError("file is not a DWG drawing (unrecognised header)")


def available_converters() -> list[Converter]:
    """Installed converters in preference order."""
    found = []
    for name, executable in (("oda", "ODAFileConverter"), ("libredwg", "dwg2dxf")):
        path = shutil.which(executable)
        if path:
            found.append(Converter(name=name, executable=path))
    return found


def _version(converter: Converter) -> str | None:
    if converter.name != "libredwg":
        return None
    try:
        completed = subprocess.run(
            [converter.executable, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = completed.stdout.strip().splitlines()
    return lines[0] if lines else None


def _warnings(stderr: str) -> list[str]:
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.startswith(("Reading ", "Writing "))
    ]
    unique = list(dict.fromkeys(lines))
    errors_first = sorted(unique, key=lambda line: not line.upper().startswith("ERROR"))
    return errors_first[:MAX_WARNINGS]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise DrawingError(f"DWG conversion exceeded {CONVERSION_TIMEOUT_S} s") from error
    except OSError as error:
        raise DrawingError(f"DWG converter could not be started: {error}") from error


def _convert_libredwg(converter: Converter, source: Path, target: Path) -> str:
    completed = _run([converter.executable, "-y", "-o", str(target), str(source)])
    return completed.stderr


def _convert_oda(converter: Converter, source: Path, target: Path) -> str:
    # ODA converts whole folders, so stage the one input in an isolated folder.
    with tempfile.TemporaryDirectory() as work:
        inbox, outbox = Path(work, "in"), Path(work, "out")
        inbox.mkdir()
        outbox.mkdir()
        shutil.copyfile(source, inbox / "drawing.dwg")
        completed = _run(
            [converter.executable, str(inbox), str(outbox), "ACAD2018", "DXF", "0", "1", "*.DWG"]
        )
        produced = outbox / "drawing.dxf"
        if produced.is_file():
            shutil.copyfile(produced, target)
        return completed.stderr + completed.stdout


def convert_dwg(source: Path, target: Path) -> ConversionRecord:
    """Convert ``source`` DWG to ``target`` DXF with the preferred installed converter."""
    converters = available_converters()
    if not converters:
        raise DrawingError(
            "no DWG converter is installed: install LibreDWG (dwg2dxf) or the ODA File "
            "Converter, or upload the drawing as DXF"
        )
    converter = converters[0]
    target.unlink(missing_ok=True)
    if converter.name == "oda":
        log = _convert_oda(converter, source, target)
    else:
        log = _convert_libredwg(converter, source, target)
    if not target.is_file() or target.stat().st_size == 0:
        detail = "; ".join(_warnings(log)[:3]) or "no output produced"
        raise DrawingError(f"{converter.name} could not convert the DWG: {detail}")
    return ConversionRecord(
        converter=converter.name, converter_version=_version(converter), warnings=_warnings(log)
    )
