"""Turn an uploaded DWG or DXF into a stored, content-addressed drawing record."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from syntera.drawings.convert import DWG_RELEASES, DrawingError, convert_dwg, dwg_version
from syntera.drawings.models import DrawingSummary
from syntera.drawings.reader import read_drawing

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SUMMARY_FILE = "summary.json"
SVG_FILE = "drawing.svg"
DXF_FILE = "drawing.dxf"
DRAWING_ID = re.compile(r"^[0-9a-f]{16}$")


def source_format(filename: str, data: bytes) -> str:
    """Identify DWG or DXF from the file content; the extension must agree."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".dwg", ".dxf"}:
        raise DrawingError("only .dwg and .dxf drawings are accepted")
    if suffix == ".dwg":
        dwg_version(data)
        return "dwg"
    head = data[:2048]
    if head.startswith(b"AutoCAD Binary DXF") or b"SECTION" in head:
        return "dxf"
    raise DrawingError("file is not a DXF drawing (no SECTION header)")


def _display_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    return re.sub(r"[^\w.\- ]", "_", name)[:120] or "drawing"


def load_summary(folder: Path) -> DrawingSummary:
    return DrawingSummary.model_validate_json((folder / SUMMARY_FILE).read_text(encoding="utf-8"))


def ingest_drawing(data: bytes, filename: str, root: Path) -> DrawingSummary:
    """Convert (if DWG), read and render one drawing under ``root/<drawing_id>``.

    The id is derived from the file content, so re-uploading the same file reuses the
    existing record and the outputs are identical for identical input.
    """
    if not data:
        raise DrawingError("the uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DrawingError(f"drawing exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
    kind = source_format(filename, data)
    digest = hashlib.sha256(data).hexdigest()
    folder = root / digest[:16]
    if (folder / SUMMARY_FILE).is_file():
        return load_summary(folder)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        return _process(data, filename, kind, digest, folder)
    except DrawingError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as error:  # ezdxf can fail deep inside malformed entities
        shutil.rmtree(folder, ignore_errors=True)
        raise DrawingError(f"drawing could not be processed: {error}") from error


def _process(data: bytes, filename: str, kind: str, digest: str, folder: Path) -> DrawingSummary:
    source = folder / f"source.{kind}"
    source.write_bytes(data)
    dxf = folder / DXF_FILE
    conversion = None
    version = None
    if kind == "dwg":
        version = dwg_version(data)
        conversion = convert_dwg(source, dxf)
    else:
        dxf.write_bytes(data)
    facts = read_drawing(dxf)
    (folder / SVG_FILE).write_text(facts.pop("svg"), encoding="utf-8")
    summary = DrawingSummary(
        drawing_id=digest[:16],
        filename=_display_name(filename),
        source_format=kind,
        source_sha256=digest,
        dwg_version=f"{version} ({DWG_RELEASES[version]})" if version else None,
        conversion=conversion,
        **facts,
    )
    (folder / SUMMARY_FILE).write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def list_drawings(root: Path) -> list[DrawingSummary]:
    if not root.is_dir():
        return []
    folders = sorted(path for path in root.iterdir() if DRAWING_ID.match(path.name))
    return [load_summary(folder) for folder in folders if (folder / SUMMARY_FILE).is_file()]
