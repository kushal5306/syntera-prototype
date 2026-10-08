# Syntera prototype

Syntera is a deterministic proof of concept for routing a rigid tube through a
**fully synthetic** industrial skid. It inflates analytical obstacles, searches a
3D voxel graph, builds an exact swept CAD solid with circular bends, and verifies
the result independently with OpenCascade before reporting pass or fail.

Licensing is undecided. No open-source licence is granted.

## Setup

Python 3.11 is the target runtime (3.11-3.13 are accepted).

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run

### Docker (recommended)

```bash
docker compose up --build
```

Open <http://127.0.0.1:8090>. Generated STEP, JSON, and PNG artifacts persist
under `outputs/web/latest` on the host. Stop the application with:

```bash
docker compose down
```

The container runs as a non-root user, includes a health check, and maps host
port 8090 to container port 8000. Override the host port when needed:

```bash
SYNTERA_PORT=9000 docker compose up
```

PowerShell:

```powershell
$env:SYNTERA_PORT=9000
docker compose up
```

### Native Python

```bash
python -m syntera.cli demo --config examples/demo_skid.yaml --output outputs/demo
pytest
ruff check .
ruff format --check .
```

### Interactive 3D application

```bash
python -m syntera.cli web --config examples/demo_skid.yaml --output outputs/web
```

Open <http://127.0.0.1:8000>. The browser application can orbit, pan, zoom,
show/hide and inspect assembly components, edit tube/port/obstacle inputs, generate
a route, review exact assurance results, and download the generated STEP and JSON
artifacts. The browser receives display meshes only; all routing and safety checks
remain in the deterministic Python core.

The demo writes `assembly.step`, `routed_assembly.step`, `route.json`,
`assurance_report.json`, and `route_preview.png`. Outputs are intentionally
ignored by Git.

### Importing equipment geometry from STEP

An obstacle may reference a STEP file instead of an analytical box or cylinder:

```yaml
obstacles:
  - type: step
    name: process-vessel
    path: geometry/vessel.step     # relative paths resolve against the YAML file
    translation: [300, 200, 120]   # mm, applied after import
    defeaturing:
      max_feature_radius: 10       # fill holes and rounds with radius below 10 mm
      envelope: exact              # or bounding_box for a fast, coarse envelope
```

STEP units are converted to millimetres on import. The file must contain closed, valid
solids; unreadable files, surface-only models, and invalid solids fail closed with a
diagnostic. Defeaturing is conservative: a hole or round is removed only when the result
is a valid solid that fully contains the original, so concave rounds and removals that
would delete material are rejected. Routing occupancy uses this simplified solid, while
the exact assurance checks always run against the unmodified imported geometry. Each run
writes `import_report.json` with the source SHA-256, feature counts, and volumes. The web
application only opens STEP files named in its server-side configuration.

## Architecture

Pydantic validates an explicitly millimetre-based input. CadQuery creates synthetic
equipment. A NumPy grid marks workspace boundaries and analytical obstacles inflated
by tube radius plus clearance. A custom six-neighbour A* uses deterministic ordering
and includes path length, turn, proximity, and enforced port-direction costs while
retaining collision, clearance, direction, and bend feasibility as hard constraints.
The compressed route becomes tangent straights and exact circular arcs, swept into a
solid tube. A separate OpenCascade pass tests solid intersection and shape distance.
FastAPI exposes that same pipeline to a dependency-free WebGL browser interface.
See [docs/architecture.md](docs/architecture.md) for detail.

## Assurance scope and limitations

- Analytical obstacles are axis-aligned boxes and cylinders. STEP obstacles may be
  translated but not rotated, and defeaturing only removes cylindrical, spherical, and
  toroidal features that OpenCascade can delete as a group of adjacent curved faces
  (for example, blind holes are currently kept).
- Occupancy for exact STEP obstacles costs one OpenCascade distance query per voxel.
- Ports and workspace extents must align with voxel resolution.
- Routing uses orthogonal moves and 90-degree bends.
- The generated object is the tube's exterior envelope, not a hollow manufacturing model.
- STEP verification uses exact B-rep collision/distance, but routing completeness is
  resolution-dependent.
- The preview is diagnostic only and is never evidence for a pass.
- This research prototype is not certified for production or safety-critical use.

## Next milestones

Phase 2 has started with conservative STEP import/defeaturing. It should continue with
automatic mesh controls, a
versioned CalculiX input-deck generator, material/load-case schemas, solver execution
isolation, mesh-convergence studies, result provenance, and acceptance thresholds.
Only after those deterministic foundations should orchestration interfaces be considered.
