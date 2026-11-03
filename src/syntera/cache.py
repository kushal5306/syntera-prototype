"""Content-addressed reuse of identical runs (roadmap I2).

A run is keyed by the SHA-256 of what determines it: the run kind and its version, the
Syntera version, and the canonical bytes of the design document. The deterministic
pipeline gives identical outputs for identical input, so a stored run can be restored
instead of recomputed. Restored outputs are byte-identical to a fresh run's, and a
bundle that fails its hash manifest is ignored and recomputed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from syntera import __version__
from syntera.design import DESIGN_FILE, DesignDocument, design_bytes, load_design
from syntera.storage import Storage

ROUTE_DESIGN_KIND = "route-design/v1"
# Files a route-design run writes that later steps read; STEP and previews are rebuilt
# on demand by the tools that need them.
CACHED_SUFFIXES = (".json", ".dxf")


def run_key(kind: str, design: DesignDocument) -> str:
    digest = hashlib.sha256()
    for part in (kind.encode(), __version__.encode(), design_bytes(design)):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True)
class CachedRun:
    design: DesignDocument
    report: object
    hit: bool
    key: str


class RunCache:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.hits = 0
        self.misses = 0

    def route_design(self, design: DesignDocument, output: Path) -> CachedRun:
        """``syntera.multiline.route_design`` with reuse of identical earlier runs."""
        from syntera.multiline import DESIGN_REPORT_FILE, DesignRunReport, route_design

        key = run_key(ROUTE_DESIGN_KIND, design)
        stored = self.storage.get(key)
        if stored is not None and DESIGN_FILE in stored and DESIGN_REPORT_FILE in stored:
            output.mkdir(parents=True, exist_ok=True)
            for name, data in stored.items():
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            self.hits += 1
            report = DesignRunReport.model_validate_json(stored[DESIGN_REPORT_FILE])
            return CachedRun(load_design(output / DESIGN_FILE), report, True, key)
        run = route_design(design, output)
        files = {
            path.relative_to(output).as_posix(): path.read_bytes()
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.suffix in CACHED_SUFFIXES
        }
        self.storage.put(key, files)
        self.misses += 1
        return CachedRun(run.design, run.report, False, key)
