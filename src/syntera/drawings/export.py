"""DXF to DWG export through the ODA File Converter, when it is installed (roadmap D1).

Syntera writes drawings as DXF. DWG is produced only by converting that DXF with the ODA
File Converter run as a separate program (decision 12: an external converter, never a
linked library). The converter is optional and user-installed under ODA's own terms;
when it is absent, export is refused with a clear message and DXF stays the deliverable.
Every export records the converter, target release and SHA-256 of input and output.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from syntera.drawings.convert import (
    DWG_RELEASES,
    DrawingError,
    _run,
    _warnings,
    dwg_version,
)

ODA_EXECUTABLE = "ODAFileConverter"
# ODA output-version names and the DWG header code each one writes.
DWG_TARGETS = {
    "ACAD2018": "AC1032",
    "ACAD2013": "AC1027",
    "ACAD2010": "AC1024",
    "ACAD2007": "AC1021",
    "ACAD2004": "AC1018",
    "ACAD2000": "AC1015",
}
DEFAULT_TARGET = "ACAD2018"
NO_WRITER = (
    "no DWG writer is installed: install the ODA File Converter (ODAFileConverter on PATH) "
    "if its licence suits your use, or use the DXF, which every CAD package opens"
)


class DwgExportRecord(BaseModel):
    """Provenance of one DWG export."""

    model_config = ConfigDict(extra="forbid")

    converter: str
    converter_path: str
    target: str
    release_code: str
    release: str
    source_sha256: str
    sha256: str
    warnings: list[str]


@dataclass(frozen=True)
class DwgWriter:
    name: str
    executable: str


def dwg_writer() -> DwgWriter | None:
    """The installed DWG writer, or ``None`` when DWG export is unavailable."""
    path = shutil.which(ODA_EXECUTABLE)
    return DwgWriter(name="oda", executable=path) if path else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_dwg(source: Path, target: Path, release: str = DEFAULT_TARGET) -> DwgExportRecord:
    """Convert the DXF ``source`` to the DWG ``target`` with the ODA File Converter."""
    if release not in DWG_TARGETS:
        raise DrawingError(f"unknown DWG release {release}; choose one of {sorted(DWG_TARGETS)}")
    if source.suffix.lower() != ".dxf" or not source.is_file():
        raise DrawingError(f"{source}: a DXF file is required")
    writer = dwg_writer()
    if writer is None:
        raise DrawingError(NO_WRITER)
    data = source.read_bytes()
    target.unlink(missing_ok=True)
    # ODA converts whole folders, so stage the one input in an isolated folder.
    with tempfile.TemporaryDirectory() as work:
        inbox, outbox = Path(work, "in"), Path(work, "out")
        inbox.mkdir()
        outbox.mkdir()
        (inbox / "drawing.dxf").write_bytes(data)
        completed = _run(
            [writer.executable, str(inbox), str(outbox), release, "DWG", "0", "1", "*.DXF"]
        )
        log = completed.stderr + completed.stdout
        produced = outbox / "drawing.dwg"
        if not produced.is_file() or produced.stat().st_size == 0:
            detail = "; ".join(_warnings(log)[:3]) or "no output produced"
            raise DrawingError(f"oda could not write the DWG: {detail}")
        written = produced.read_bytes()
    code = dwg_version(written)
    if code != DWG_TARGETS[release]:
        raise DrawingError(
            f"oda wrote a {DWG_RELEASES.get(code, code)} DWG, not the requested {release}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(written)
    return DwgExportRecord(
        converter=writer.name,
        converter_path=writer.executable,
        target=release,
        release_code=code,
        release=DWG_RELEASES[code],
        source_sha256=_sha256(data),
        sha256=_sha256(written),
        warnings=_warnings(log),
    )
