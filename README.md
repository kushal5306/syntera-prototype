# Syntera prototype

Syntera is a deterministic proof of concept for routing a rigid tube through a
**fully synthetic** industrial skid. It inflates analytical obstacles, searches a
3D voxel graph, builds an exact swept CAD solid with circular bends, and verifies
the result independently with OpenCascade before reporting pass or fail.

Proprietary. All rights reserved; no open-source licence is granted. See [LICENSE](LICENSE).

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

Open <http://127.0.0.1:8000>. The route workbench shows a status strip (verdict, main
piping length, bends, clearance margin, peak stress utilisation, package envelope), the
3D model, and tabs for assurance checks, stress, route geometry, run comparison, inputs,
and review. Each generated run stays in the run bar so inputs can be compared side by side.

When CalculiX (`ccx`) is installed on the server, every run also solves its FEA decks.
The model can then be coloured by von Mises stress or displacement per load case, with
an adjustable deformation scale, a pin on the peak, and a table of critical regions
grouped by route segment. The review tab times expert review for the KPI 1 time study
(stored in the browser only). The browser receives display data only; all routing,
safety checks, and FEA acceptance remain in the deterministic Python core, and a run
whose decks could not be solved is shown as failing.

Keyboard: `1`-`4` views, `F` fit, `X` x-ray, `S` stress, `P` flow, `[` `]` previous or
next run. three.js r128 (MIT, `static/vendor/three.LICENSE`) is vendored so the
container works offline; web fonts fall back to system fonts when offline.

The demo writes `assembly.step`, `routed_assembly.step`, `route.json`,
`assurance_report.json`, `route_preview.png`, `layout.dxf` and `rationale.json`. Outputs
are intentionally ignored by Git.

`rationale.json` says why each value the run used has its value: one record per parameter
path with its source (`user`, `default`, `rule`, `calculation`, `drawing` or `inferred`),
the versioned rule id and inputs behind it, and a short explanation. Runs built from a
P&ID or a drawing record the sizing, clearance, grid and cost rules they applied, and the
P&ID plant folder adds equipment sizes, positions and the skid envelope. Records explain
values only; they never carry or change an assurance verdict.

A configuration may state a line's duty instead of a fixed outside diameter. With a
`line_sizing` block (medium, flow, design pressure and temperature), `demo`,
`fea-evaluate` and `web` size the line with `syntera.rules` and record the chosen OD, bend
radius and any wall or material the input did not state as `rule` values:

```bash
python -m syntera.cli demo --config examples/demo_skid_sized.yaml --output outputs/sized
```

In the web app, sizing records apply only while the tube, wall, material and safety factor
posted from the browser still equal the sized values; once one is edited, the posted values
are recorded as the user's. The rule tables are synthetic and not code-certified.

A configuration converts into a canonical design document, `design.json`: equipment,
nozzles, lines (tube, duty and analysis), routing parameters, the rationale for every
chosen value (paths under `parameters.` or `lines.<line id>.`) and SHA-256 hashes of the
YAML and STEP inputs. The JSON is canonical, so identical input gives byte-identical
files, and a run derived from the document writes the same `route.json` and
`rationale.json` as a run from the YAML. The document never holds an assurance verdict.

```bash
python -m syntera.cli design --config examples/demo_skid_sized.yaml --output outputs/design
python -m syntera.cli demo --config outputs/design/design.json --output outputs/from-design
```

Every run derives its configuration from a design document: `demo`, `fea-evaluate` and
`web` accept either a YAML configuration (converted on the fly) or a `design.json`, and
`--line` picks the line when a design has several. `demo` writes `design.json` next to
its outputs with the routed centreline recorded under `routes`; routes are outputs kept
for traceability, never evidence, and the verdict stays in `assurance_report.json`.
Identical design input gives byte-identical route, rationale, design, metrics, FEA deck
and comparison files. In the web app, a design record applies only while the posted
configuration still has the value it explains.

A design with several lines routes with `route-design`: lines go largest outside diameter
first (equal sizes by line id, rule `design.largest_bore_first.v1`, recorded as
`routing_order` in each line's `rationale.json`). Each line is routed and verified by the
single-line pipeline with all equipment, conservative boxes around every pipe already
routed and reserved space at the nozzles of lines still to come as obstacles. Every pair
of tube solids is then checked with exact geometry (no intersection, gap at least the
larger clearance). Outputs: `lines/<id>/` per line, `design.json` with every route,
`design_report.json` and `routed_design.step`. Shared nozzles (tees) are refused for now.

```bash
python -m syntera.cli route-design --design examples/demo_three_lines.design.json --output outputs/three-lines
```

A change set (`changes.json`) edits a design with typed operations: `set_parameter`
(a path such as `lines.main.tube.minimum_clearance` or `parameters.voxel_resolution`; ids,
references, provenance and outputs cannot be set), `move_equipment` (its nozzles move
with it), `add_line` and `remove_line`. The whole result is validated as a design, set
values get a `user` rationale record, and stored routes are cleared. `change` writes the
new `design.json`, `revert_changes.json` (which restores the original byte for byte) and
`design_diff.json` (changed values and rationale). With `--regenerate` it routes and
verifies the design before and after and lists every check whose verdict changed. A
change set edits inputs only; verdicts always come from the deterministic checks.

```bash
python -m syntera.cli change --design examples/demo_skid.yaml --changes examples/demo_skid.changes.json --output outputs/changed --regenerate
```

DWG export converts a generated DXF with the ODA File Converter when `ODAFileConverter`
is on `PATH` (installed by the user under ODA's terms; never bundled or linked). Without
it, export is refused with a clear message and the DXF remains the deliverable. Each
export writes `<name>.export.json` with the converter, DWG release and SHA-256 of the DXF
and the DWG; `/api/drawings/capabilities` reports `dwg_export`.

```bash
python -m syntera.cli export-dwg --input outputs/demo/layout.dxf --output outputs/demo/layout.dwg
```

`project` makes exact hidden-line views of any STEP assembly with OpenCascade's
`HLRBRep`: plan, front, right and isometric. Visible and hidden edges are discretised,
rounded and sorted (identical geometry gives a byte-identical `projection.json`), tagged
with the solid they came from, and written to `views.dxf` with visible edges on
`HLR-VISIBLE` and hidden edges on the dashed `HLR-HIDDEN` layer.

```bash
python -m syntera.cli project --step outputs/demo/routed_assembly.step --output outputs/views
```

`sheet` turns a `demo` or `route-design` output folder into an ISO drawing sheet:
`sheet.dxf` with frame, title block, the four hidden-line views and dimensions (overall
size of each orthographic view, nozzle positions from the model origin in plan, and
centreline run lengths in plan), plus `sheet.json` recording every decision. The sheet
is the smallest of A3, A2, A1, A0 on which the views fit at an ISO 5455 scale no smaller
than 1:10, at the largest such scale (rule `drawing.sheet_and_scale.v1`). The title block
repeats the verdict from the run's report and states that the sheet is a review drawing,
not evidence.

Every entity on a sheet carries XDATA (application `SYNTERA`) naming what it shows:
`equipment:<id>`, `nozzle:<id>` or `line:<id>` from the design document, `view:<name>`
for overall dimensions and view labels, or `drawing:<part>` for the frame, title block
and notes. The views are projected from the design's own exact shapes, so a selected
edge or dimension resolves to its line, nozzle or equipment
(`syntera.drawings.trace.resolve`, `trace_dxf`).

```bash
python -m syntera.cli sheet --run outputs/demo --output outputs/sheet
```

The knowledge store (`src/syntera/knowledge/entries/*.yaml`) explains every rule,
material and practice Syntera uses, in its own words, with citations that name where
each idea comes from without quoting it. Every rule id written into a rationale record
links to an entry (a test scans the code to keep this true), entries for synthetic
tables say so, and long quotations are refused by the loader.

```bash
python -m syntera.cli knowledge --id kb.rule.hoop_stress_wall
```

`search` ranks knowledge entries, design rationale records and drawing-graph entities
together with Okapi BM25 over a fixed tokeniser: no network and no learned model, ties
broken by document id, so the same corpus and query always give the same ranking.

```bash
python -m syntera.cli search --query "why is the bend radius 30 mm" --design examples/demo_skid_sized.yaml
```

`explain` answers why a value is what it is, from its rationale record, the rule that set
it, that rule's knowledge entry and citations, and the values it depends on. `plain` mode
is for anyone; `engineer` mode adds inputs, formulae, rule ids and references. For a
foreign drawing it explains an entity from its labels and dimensions, or back-calculates
flow velocity from a stated diameter (OD label, diameter dimension or DN size) and a given
flow; every drawing explanation is labelled as inference. Explanations never state or
change a verdict.

```bash
python -m syntera.cli explain --design examples/demo_skid_sized.yaml --path tube.outer_diameter --mode engineer
python -m syntera.cli explain --graph outputs/drawings/<id>/drawing_graph.json --handle 2F --flow 36 --wall 3.9
```

A `fittings:` block (in a YAML config, or on a design line) adds standard components:
an elbow at every bend (a long-radius elbow when the bend radius is 1.5 x OD, otherwise
a pipe bend) and, when `end_connection: flanged`, a flange at each end, sized from
synthetic tables by tube OD and rating (`PN16` or `PN40`). Tees and reducers are in the
library for branch and size-change points. Every fitting is checked with exact geometry
(clearance, collision, workspace); a failing fitting fails the assurance. `fittings.json`
lists the fittings, the straight cut lengths between them and the checks, and
`routed_assembly.step` then holds straight pipe, elbows and flanges. In multi-line
designs, flanges are obstacles for later lines and part of the pair checks.

```bash
python -m syntera.cli demo --config examples/demo_skid_fittings.yaml --output outputs/fittings
```

`isometrics` takes a routed `demo` or `route-design` folder (or a design with stored
routes) and writes `bom.json` plus one `iso_<line>.dxf` per line. The bill of materials
is derived from the design document: fittings placed as the pipeline places them,
straight cut lengths, material from the line's analysis, and totals by item, size and
material. Each isometric sheet shows the line's centreline in isometric projection with
corner-to-corner run lengths, fitting marks and its BOM; every entity carries its
`SYNTERA` reference. The BOM is a review take-off, not a purchasing document.

```bash
python -m syntera.cli isometrics --run outputs/fittings --output outputs/isometrics
```

A `supports:` block places rest supports and splits spools by rule. The maximum span is
given, or calculated so a simply supported span of pipe and contents sags no more than
`deflection_limit_mm` (rule `rules.support_span_deflection.v1`). Supports go on straight
runs, as far apart as that span allows, with the ends anchored at their ports. Spools are
cut on straight runs, at most `maximum_spool_length_mm` long and `cut_clearance_mm` clear
of every bend. `supports.json` records positions, spools and their rationale, and every
CalculiX load case holds each support's node ring vertically. A line that cannot be
supported within the span fails the assurance.

```bash
python -m syntera.cli demo --config examples/demo_skid_supports.yaml --output outputs/supports
```

`layout.dxf` is a general-arrangement drawing in millimetres: plan, front elevation and
right elevation (third-angle) of the equipment, the nozzles and the routed tube drawn
along its exact tangent/arc centreline at its outer diameter, with overall dimensions
and a title block that repeats the assurance verdict. It is a review deliverable, not
evidence. In the web app, **Open layout in Drawings** sends it to the drawing viewer.

### CalculiX analysis decks

When the configuration has an `analysis` section and the route passes assurance,
the demo also writes `fea/`: a shared `mesh.inp`, one `<load_case>.inp` per load
case, and `fea_manifest.json` (deck format version, input SHA-256, mesh summary,
and acceptance limits). Solve the decks with CalculiX, then apply the thresholds:

```bash
python -m syntera.cli demo --config examples/demo_skid.yaml --output outputs/demo
(cd outputs/demo/fea && for deck in design_pressure self_weight thermal_rise; do ccx -i "$deck"; done)
python -m syntera.cli fea-evaluate --config examples/demo_skid.yaml --output outputs/demo
```

`fea-evaluate` writes `fea/fea_acceptance_report.json` and exits non-zero unless
every load case is within the allowable von Mises stress (yield strength divided by
the safety factor) and the displacement limit. A missing or unreadable result file,
or a manifest whose input hash no longer matches the configuration and route, fails.

Analysis units are mm, N, s, tonne, MPa, and K: steel density is about
`7.85e-9` tonne/mm³ and standard gravity is `9810` mm/s².

### Comparing a design with a baseline skid (KPI 2)

A baseline file describes the reference package in millimetres: its overall dimensions,
main piping length and bends, and the cost rates used to price both sides. After a demo
run (and, when the config has an `analysis` section, solving the decks and running
`fea-evaluate`), compare the run with it:

```bash
python -m syntera.cli compare --baseline examples/baseline_skid.yaml --output outputs/demo
```

`comparison.json` records the package volume, footprint and height ratios, the main
piping length ratio and saving, and the estimated piping cost saving, together with the
baseline file's SHA-256. KPI 2 counts as met only when the volume ratio is below 1 and the
design is valid: assurance passed and, if FEA decks were generated, their acceptance
report exists, passes and matches the decks. Otherwise the reasons are listed and the
command exits non-zero. `examples/baseline_skid.yaml` is a synthetic reference for CI;
replace it with figures from the vendor's general-arrangement drawing.

### Logging engineering time (KPI 1)

KPI 1 divides the hours a benchmark task takes with Syntera, review and correction
included, by the hours the manual workflow takes. Engineers record each phase they work
on; Syntera records its own compute time when given a log:

```bash
python -m syntera.cli time start --log outputs/time_log.jsonl --task one-line --phase review
python -m syntera.cli demo --config examples/demo_skid.yaml --output outputs/demo \
  --time-log outputs/time_log.jsonl --task one-line
python -m syntera.cli time stop --log outputs/time_log.jsonl --task one-line --phase review
python -m syntera.cli kpi1 --log outputs/time_log.jsonl \
  --baseline examples/manual_baseline.yaml --output outputs/demo
```

The log is append-only JSON Lines with UTC timestamps. A task's time is the union of its
intervals, so a review that overlaps a regeneration counts once. `kpi1_report.json` gives
the hours and ratio per task and pooled over measured tasks, with the SHA-256 of the log
and the baseline. KPI 1 counts as met only when the log is well formed (every phase
started once and stopped), every logged task has a manual baseline, every baseline task
has been measured, and the ratio is below 1. The command exits non-zero on a malformed
log. In the web application the Review tab's timer and every routing run are logged on
the server against the chosen benchmark task, and the tab shows the live ratio.
`examples/manual_baseline.yaml` holds labelled estimates for three benchmark tasks;
replace them with timed manual hours and set `estimate: false`.

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

### Viewing DWG and DXF drawings

Open <http://127.0.0.1:8000/drawings> (or **Drawings** in the workbench header) and drop a
`.dwg` or `.dxf` file, up to 50 MB. The server converts DWG to DXF, reads it with ezdxf,
and renders model space as an SVG you can pan and zoom (scroll, drag, Fit). Drawings are
shown as on paper: a white sheet, with white, yellow and other light CAD colours darkened
just enough to stay readable. The side panel
lists what was read: DWG release, converter, units, layers, dimension values, and text notes
(click a note to find it on the sheet). The same can be produced offline:

```bash
python -m syntera.cli drawing --input path/to/plan.dwg --output outputs/drawings
```

Each drawing is stored under `<output>/drawings/<id>/` with `drawing.dxf`, `drawing.svg`, and
`summary.json`; the id is the first 16 hex digits of the file's SHA-256, so the same file
always gives the same record.

Besides the drawn space's entities, dimensions and notes, `summary.json` lists every
paper-space layout (tab order, entities, viewports, block inserts), every block insert
that carries attributes in any space with its attribute values and a keyword-rule role
(`title_block`, `bom_row` or `other`), external references (name, path, overlay, insert
count), and MTEXT normalised to plain lines without formatting codes.

Each drawing also gets `drawing_graph.json`, a semantic graph of the drawn space:
geometry nodes (kind from the layer name, or from the design reference on Syntera's own
sheets), labels attached to the nearest geometry within five text heights (inside a
closed outline counts as touching; the smallest outline wins), dimensions linked to the
geometry their definition points touch, and title-block attribute values. Queries in
`syntera.drawings.graph`: `find_by_tag`, `label_of`, `dimensions_of`. Label links are
proximity associations, not design facts.

```bash
python -m syntera.cli drawing-graph --input outputs/sheet/sheet.dxf --output outputs/graph --tag main
```

DWG is a closed format, so conversion uses an external program, chosen in this order:

- **ODA File Converter** (`ODAFileConverter` on `PATH`): the most faithful reader. It is free
  to download from the Open Design Alliance but is not open source, so it is never bundled;
  install it yourself if its licence suits your use.
- **LibreDWG** `dwg2dxf` (GPLv3): built into the Docker image from the pinned GNU release and
  run as a separate program. Reads R13 to AutoCAD 2018 drawings well. Custom objects, ACIS
  solids, and some newer entities can be skipped; converter messages are shown with the
  drawing so missing content is visible. LibreDWG's DWG writer is not used.

Without either converter, DXF uploads still work and DWG uploads fail with an explanation.
The rendered view is for review, not a measurement: fonts and linetypes are approximated,
and entities far outside the drawing (for example, a mis-scaled block) are left out of the
default view and listed. Units come from `$INSUNITS`; unitless drawings are flagged.

### 3D assembly and piping check from a drawing

On the Drawings page, **Generate 3D and check piping** turns a plan layout into a 3D
assembly and reviews its piping as drawn:

- Equipment outlines (closed polylines, circles, blocks) on layers named like `EQUIP`,
  `VESSEL`, `PUMP` or `TANK` become boxes and vertical cylinders. Pipe runs on `PIPE-CL` or
  `PIPE`/`PIPING` layers are joined into lines and reduced to their corner points; nozzles
  come from `NOZZLE` layers. Other layer names can be mapped under **Layer roles**.
- A plan fixes X and Y only. Heights come from a front elevation when the drawing has
  views titled PLAN and FRONT ELEVATION (as Syntera's own `layout.dxf` does); otherwise
  equipment is 1000 mm tall and pipes run at 500 mm. Pipe OD, bend radius and clearance
  are read from pipe widths, nozzle circles or notes such as `OD 20`, else defaulted.
  Every value is listed with its source (drawing, default or user) and can be edited.
- Each drawn run is swept into an exact CAD tube and verified with the same checks as a
  routed tube (collision, clearance, bend radius, port alignment), naming any equipment
  it clashes with. Syntera then routes the same nozzles itself. Length, bends and an
  estimated cost (editable rates per metre and per bend) are compared, and a saving is
  only claimed for a Syntera route that passes every check.
- **3D assembly** shows equipment, the drawn pipe (amber) and Syntera's route (teal);
  **Open in route workbench** loads the inferred model there. STEP files of both routes,
  the layout DXF and `piping_check.json` are downloadable.

P&IDs and other schematics show connectivity, not position, so a DWG/DXF P&ID is refused
with an explanation; use the DEXPI route below for those. Offline:

```bash
python -m syntera.cli drawing-check --input path/to/plan.dxf --output outputs/drawings \
  --options '{"equipment_height_mm": 1200, "cost_per_metre": 150}'
```

### Proposed 3D layout from a P&ID (DEXPI XML)

A P&ID records what connects to what, not where anything sits, so a 3D model of it has to
be designed rather than read. **P&ID to 3D (DEXPI XML)** on the Drawings page does that
from a DEXPI (Proteus XML) export, which most P&ID tools can write:

- Equipment tags, classes, nozzles and sizes (tank length and capacity, tube length, plate
  size, column height and diameter) are read; missing sizes use stated class defaults.
  Graphics positions are ignored.
- The piping graph is reduced to runs between nozzles and battery limits (off-page
  connectors and open ends at the skid edge). Valves, reducers and tees are passed
  through, blind-flanged stubs and drains dropped, and a header with branches is routed
  as a chain through its terminals. Where two runs share a nozzle, the second joins the
  first through a tee: a short branch square to the first pipe, so the two never overlap.
- Equipment is placed on a grid with 1500 mm access gaps and rearranged by pairwise swaps
  to shorten connected nozzles. Each nozzle faces what it connects to, sits on the
  equipment's actual surface (the curved wall of a tank, not its bounding box) and within
  its height.
- Lines are routed largest bore first with OD from the nominal size, bend radius 1.5 x OD
  and clearance half the OD. Each routed pipe is an obstacle for later lines, and every
  route is verified with the exact checks. Lines that cannot be routed are listed.
- The finished plant is then audited as one assembly (below). If lines are unrouted, the
  audit fails or a line takes a long detour, the plant is routed again in other line
  orders (problem lines first, shortest first) and the best pass is kept: fewest
  unrouted lines, then fewest audit failures, then least pipe. Every pass is listed.

The result is a 3D view, a plant STEP file, `plant_layout.json` (total pipe length, bends,
cost, footprint, audit, routing passes) and per-line workbench links. Offline:

```bash
python -m syntera.cli pid-3d --input path/to/pid.xml --output outputs/pid
```

### Design audit of a whole assembly

Per-route checks verify each pipe against what it was routed around. The design audit
looks at the finished assembly the way a reviewer would: every solid against every other
solid, with sizes measured from the CAD solids rather than taken from the router. Rules:

| Rule | What is measured |
| --- | --- |
| Completeness | every required line has a pipe (P&ID plants) |
| Clash | exact boolean volume on every pair; joined parts may meet only within one OD of the joint |
| Clearance | exact surface distance between every unjoined pair with a pipe; max(25 mm, OD/2) by default |
| Connections | each pipe ends on its port along the port axis, each nozzle touches its equipment, no pipe ends in mid air |
| Bend radius | major radius of every toroidal face of each pipe solid against 1.5 x OD |
| Outside diameter | radius of every cylindrical and toroidal face against the line size |
| Access | at least 800 mm between equipment solids |
| Open ends (advisory) | imported models: pipe ends that meet nothing (battery limit, vent, drain or a missing joint) |
| Detour (advisory) | routed length over the axis-aligned port distance above 1.5 |

Every finding names the solids, the measured value, the requirement and a location.
**Audit STEP assembly** on the Drawings page runs it on any STEP file, for example a
vendor's skid model: single swept-tube solids up to DN600 are taken as pipe, everything
else as equipment, and pipe end caps that lie on another solid are its joints. Offline:

```bash
python -m syntera.cli audit-step --input path/to/assembly.step --output outputs/audit
```

## Architecture

Pydantic validates an explicitly millimetre-based input. CadQuery creates synthetic
equipment. A NumPy grid marks workspace boundaries and analytical obstacles inflated
by tube radius plus clearance. A custom six-neighbour A* uses deterministic ordering
and includes path length, turn, proximity, and enforced port-direction costs while
retaining collision, clearance, direction, and bend feasibility as hard constraints.
The compressed route becomes tangent straights and exact circular arcs, swept into a
solid tube. A separate OpenCascade pass tests solid intersection and shape distance.
FastAPI exposes that same pipeline, plus solved CalculiX fields, to a three.js browser workbench.
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
- FEA models the tube mid-surface with structured S8R shells, linear-elastic
  isotropic material, small-displacement statics, and both ends fully clamped at
  their ports. Each load case is solved independently; combinations, supports along
  the run, fatigue, and code-based stress classification are not modelled.
- Peak von Mises includes clamp-edge stress concentrations, which is conservative.
  Mesh-convergence studies are not yet automated.
- Drawing intake reads model space only; paper-space layouts, xrefs, and 3D solids in
  DWG/DXF files are not interpreted. The drawing piping check reads each pipe run at one
  elevation (risers drawn only in elevations are not rebuilt), checks square bends only,
  treats each run separately (other pipes are not obstacles), checks at most six runs,
  and needs nozzles clear of equipment by the required clearance for Syntera to route.
- The design audit is geometric: it does not check pipe supports and spans, flange
  ratings, valve access heights, insulation, thermal growth or code stress. STEP audits
  classify pipe by shape, so a round vessel smaller than the pipe size limit (DN600 by
  default, `--max-pipe-od`) is taken as pipe.
- This research prototype is not certified for production or safety-critical use.

## Next milestones

The intelligent CAD assistant roadmap, with milestones per architecture layer and the
daily run protocol, is in [docs/roadmap.md](docs/roadmap.md).

Phase 2 now includes conservative STEP import/defeaturing, material/load-case schemas,
a structured tube shell mesh with quality limits, a versioned CalculiX input-deck
generator with input-hash provenance, and acceptance thresholds. It should still add
solver execution isolation, automated mesh-convergence studies, and fuller result provenance.
Only after those deterministic foundations should orchestration interfaces be considered.
