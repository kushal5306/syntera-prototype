# Roadmap: Syntera as an intelligent CAD assistant

Goal: a scalable assistant that lets engineers and non-technical users view, create, edit
and question DWG/DXF and 3D designs, explains *why* each design value was chosen, and
supports design optimisation. Every milestone keeps the rules in `AGENTS.md`: safety-critical
checks stay deterministic, hard constraints are never traded against objectives, and AI
may propose but never issues an assurance pass.

## Principles

1. **The design model is the source of truth.** DWG/DXF, STEP and drawings are derived from
   it or imported into it, never the master.
2. **"Why" is recorded, not guessed.** Every value Syntera chooses carries a rationale record
   (source, rule, inputs, result). For foreign drawings, explanations are labelled as
   inference.
3. **The LLM only calls typed, deterministic tools.** Prompts become validated change sets
   that the user confirms; geometry is never written by a model.
4. **No credentials or licensed data in Git.** Anything needing an API key, a licensed
   converter or real standards data is optional, detected at runtime, and skipped in CI.
5. **Rule and material tables are synthetic and labelled** until real, licensed data is
   supplied. They must never be presented as code-certified.

## Status

Status values: `todo`, `in-review` (PR open), `done` (merged), `blocked` (reason given).

| # | ID | Milestone | Layer | Depends on | Status | PR |
|---|----|-----------|-------|------------|--------|----|
| 1 | A1 | Rationale schema | Design model | - | done | [#19](https://github.com/kushal5306/syntera-prototype/pull/19) |
| 2a | A2a | Deterministic engineering rule library: rules, tables, config sizing | Design model | A1 | in-review | [#22](https://github.com/kushal5306/syntera-prototype/pull/22) |
| 2b | A2b | Demo, compare and web runs size a line from flow | Design model | A2a | in-review | [#23](https://github.com/kushal5306/syntera-prototype/pull/23) |
| 3a | A3a | Canonical design document: schema, conversion, persistence, migration | Design model | A1 | in-review | [#24](https://github.com/kushal5306/syntera-prototype/pull/24) |
| 3b | A3b | CLI and web runs derive all outputs from the design document | Design model | A3a | todo | |
| 4 | B1 | Multi-line design from the design document | 3D engine | A3a | todo | |
| 5 | A4 | Change sets and design diff | Design model | A3a | todo | |
| 6 | D1 | DWG export through external converter | DWG I/O | - | todo | |
| 7 | C1 | Hidden-line projection from exact B-rep | 2D drawings | A3a | todo | |
| 8 | C2 | Drawing sheets, title block, auto dimensions | 2D drawings | C1 | todo | |
| 9 | C4 | Drawing-to-model traceability | 2D drawings | C2 | todo | |
| 10 | D2 | Richer drawing reading | DWG I/O | - | todo | |
| 11 | D3 | Semantic drawing graph | DWG I/O | D2 | todo | |
| 12 | E1 | Knowledge store | Knowledge | A2a | todo | |
| 13 | E2 | Local retrieval | Knowledge | E1, D3 | todo | |
| 14 | E3 | Deterministic explanation engine | Knowledge | E2 | todo | |
| 15 | B2 | Standard component library | 3D engine | B1 | todo | |
| 16 | C3 | Line isometrics and bill of materials | 2D drawings | B2, C2 | todo | |
| 17 | B3 | Supports and spool splitting | 3D engine | B2 | todo | |
| 18 | D4 | Round-trip DXF editing | DWG I/O | A4, D3 | todo | |
| 19 | F1 | Assistant tool API | Intelligence | A4, D3, E3 | todo | |
| 20 | F2 | LLM provider interface and agent loop | Intelligence | F1 | todo | |
| 21 | F3 | Drawing and design Q&A endpoint | Intelligence | F2 | todo | |
| 22 | F5 | Assistant evaluation suite | Intelligence | F3 | todo | |
| 23 | F4 | Prompt-to-design with confirmation | Intelligence | F3, B1 | todo | |
| 24 | G1 | Assistant panel in the web app | Web | F3 | todo | |
| 25 | G2 | Parameter editing with diff view | Web | A4 | todo | |
| 26 | G3 | Generated sheet viewer and downloads | Web | C2, D1 | todo | |
| 27 | G4 | Design history | Web | A4 | todo | |
| 28 | H1 | Optimisation problem schema | Optimisation | A4 | todo | |
| 29 | H2 | Verified optimiser | Optimisation | H1 | todo | |
| 30 | H3 | Optimisation report and view | Optimisation | H2 | todo | |
| 31 | I1 | Background jobs | Platform | - | todo | |
| 32 | I2 | Content-addressed run cache and storage interface | Platform | I1 | todo | |
| 33 | I3 | API versioning and contract tests | Platform | I1 | todo | |

## Milestones

Each milestone is sized for one working session. If it proves larger, split it into
`<ID>a`, `<ID>b` in the table and deliver the first part.

### Layer A: canonical design model and rationale

**A1 Rationale schema.** Pydantic `Rationale` records: parameter path, value, unit, source
(`user`, `default`, `rule`, `calculation`, `drawing`, `inferred`), rule id, inputs, a short
explanation template and references. Attach rationale to the values the existing pipeline
already chooses (OD, bend radius, clearance, routing costs, P&ID class defaults) and write
`rationale.json` next to `route.json`.
*Done when:* every chosen value in a demo run has a rationale entry; unit tests cover each
source type; an integration test checks `rationale.json` is deterministic.

**A2 Deterministic engineering rule library.** `syntera.rules`: pipe size from flow and
maximum velocity, minimum wall thickness from pressure (simple hoop-stress formula),
bend radius and clearance rules, material choice from a medium/temperature table. Each rule
is a pure function returning a value plus its rationale. Tables are synthetic and say so.
*Done when:* rules are versioned, unit-tested with hand-calculated cases, and the demo can
size a line from flow instead of a fixed OD.

Split into two parts:

- **A2a** `syntera.rules`: versioned pure rules (minimum bore from velocity, flow velocity,
  hoop-stress wall, smallest standard tube, long-radius bend, clearance, material by
  medium and temperature, allowable stress from yield), synthetic tables, and
  `sized_config()` that turns a `line_sizing` block into tube, wall and material values
  with rationale (`examples/demo_skid_sized.yaml`). Unit-tested with hand calculations.
- **A2b** wire `sized_config()` into `demo`, `compare`, `fea-evaluate` and the web app so
  their rationale records reach `rationale.json`; integration test that the sized demo
  routes, verifies and writes a deterministic `rationale.json` with the OD from a rule.

**A3 Canonical design document.** A versioned `design.json`: equipment, nozzles, lines,
routes, parameters, rationale and input hashes. Load, validate and save; CLI and web runs
derive all outputs from it. Existing YAML configs convert into it.
*Done when:* demo, compare and FEA run from a design document with byte-identical outputs
for identical input, and a schema version migration test exists.

Split into two parts:

- **A3a** `syntera.design`: versioned `DesignDocument` (equipment, nozzles, lines with
  tube, duty and analysis, parameters, routes, rationale under `parameters.` and
  `lines.<id>.`, input hashes), conversion from YAML (fixed OD or `line_sizing`),
  per-line run configuration and rationale, canonical save/load, content hash, a
  migration registry with tests, and `syntera design` to write `design.json`.
- **A3b** `demo`, `compare`, `fea-evaluate` and the web app accept and write a design
  document and derive every output from it; integration test of byte-identical outputs
  for demo, compare and FEA decks from identical design input.

**A4 Change sets and design diff.** Typed `ChangeSet` edits (set parameter, move
equipment, add or remove line) applied to a design, followed by regeneration and a diff
report: changed values, changed rationale, and which checks changed verdict.
*Done when:* applying and reverting a change set round-trips; diff report has tests.

### Layer B: 3D design engine

**B1 Multi-line design.** Route every line in a design document with earlier lines as
obstacles (reusing the P&ID layout routing), largest bore first, each verified with the
exact checks.
*Done when:* a three-line demo design routes and verifies deterministically.

**B2 Standard component library.** Parametric CadQuery elbows, tees, reducers and flanges
from dimension tables keyed by size and rating (synthetic tables). Components are placed at
bends and branches and included in STEP output and verification.
*Done when:* routed assemblies contain fittings, and verification covers them.

**B3 Supports and spool splitting.** Support placement from a maximum-span rule; spools
split by maximum length; supports passed to FEA as boundary conditions.
*Done when:* rules carry rationale, and FEA decks include supports when present.

### Layer C: 3D to 2D drawings

**C1 Hidden-line projection.** Project the exact routed assembly with OpenCascade
`HLRBRep` into plan, front, side and isometric views; visible and hidden edges go to
separate DXF layers.
*Done when:* projection is deterministic and tested on the demo assembly.

**C2 Drawing sheets.** ISO A-size frames, title block fields, automatic scale choice, view
placement, and automatic dimensions (overall size, nozzle positions, run lengths). The
title block repeats the assurance verdict and is never evidence.
*Done when:* a generated sheet DXF opens cleanly in ezdxf audit with no errors.

**C3 Line isometrics and bill of materials.** One isometric per line with run lengths and
fittings, plus a BOM table (fittings, cut lengths, material) from the design document.
*Done when:* BOM quantities match the design model in tests.

**C4 Drawing-to-model traceability.** Each generated DXF entity carries XDATA linking it
to a design-document id, so a selected entity resolves to its line, fitting or equipment.
*Done when:* round-trip test resolves every entity on a generated sheet.

### Layer D: DWG and DXF I/O

**D1 DWG export.** Write DWG by converting generated DXF with ODA File Converter when it is
installed (decision 12: external converter only, never linked). Report clearly when no
writer is available; record the converter used.
*Done when:* export works when the converter is present and tests skip cleanly when not.

**D2 Richer drawing reading.** Read paper-space layouts, blocks with attributes (title
blocks, BOM tables), xref names and normalised MTEXT into `summary.json`.
*Done when:* fixtures built with ezdxf cover each new element.

**D3 Semantic drawing graph.** Turn a read drawing into `drawing_graph.json`: equipment,
lines, labels associated by proximity, dimensions linked to the geometry they measure,
title-block facts. Query functions: find by tag, label of entity, dimensions of entity.
*Done when:* queries are deterministic and tested on fixtures and on Syntera's own sheets.

**D4 Round-trip DXF editing.** Apply a change set to an imported DXF (move, retag, change
layer or text) and write DXF/DWG back, preserving untouched entities.
*Done when:* untouched entities are byte-stable in tests.

### Layer E: knowledge

**E1 Knowledge store.** YAML entries for rules, materials and practices with source
citations (paraphrased, no copyrighted standard text), a schema and a loader.
*Done when:* the loader validates all entries and every A2 rule links to one.

**E2 Local retrieval.** Deterministic keyword retrieval (BM25, no network) across
knowledge entries, rationale and drawing graphs. An embedding backend may come later as an
optional interface.
*Done when:* ranked results are stable and tested.

**E3 Deterministic explanation engine.** `explain(target)` builds a structured explanation
from rationale, rules and knowledge citations. For foreign drawings it labels inference and
back-calculates where possible (for example, flow velocity from a stated diameter and
flow). Plain-language and engineer modes.
*Done when:* explanations for demo values are tested; inferred answers are always labelled.

### Layer F: intelligence

**F1 Assistant tool API.** Typed tools over deterministic functions: query design, query
drawing, explain, run checks, propose change, apply change, regenerate, compare. JSON
schema export. No tool can set or override an assurance verdict.
*Done when:* every tool has tests and a guard test proves assurance cannot be set.

**F2 LLM provider interface and agent loop.** Provider protocol, a scripted fake provider
for tests, and an optional Anthropic provider enabled only by `ANTHROPIC_API_KEY`. Agent
loop with a step limit and a logged transcript.
*Done when:* CI runs the loop with the fake provider and never needs a key.

**F3 Q&A endpoint.** `/api/assistant` answers questions about the current drawing or
design, citing rationale and entity ids.
*Done when:* scripted-provider integration tests pass.

**F5 Assistant evaluation suite.** Questions with expected facts over demo designs and
fixture drawings; runs with the fake provider in CI and with a real model on demand.
*Done when:* the suite runs in CI and reports per-question results.

**F4 Prompt-to-design.** A request becomes a proposed change set or design spec; the user
confirms; the deterministic pipeline generates and verifies; the assistant explains the
result. Nothing is applied without confirmation.
*Done when:* tests prove an unconfirmed proposal changes nothing.

### Layer G: web

**G1 Assistant panel.** Chat panel in the workbench and drawings page; cited entities are
highlighted in 2D and 3D.

**G2 Parameter editing.** Edit parameters through change sets with a before/after diff.

**G3 Sheet viewer and downloads.** View generated sheets and download DXF, DWG (when a
converter is available) and PDF.

**G4 Design history.** Keep versions of a design document and compare any two.

*Done when (all G):* web integration tests cover the new endpoints; the browser still
receives display data only.

### Layer H: optimisation

**H1 Optimisation problem schema.** Variables (line size, bend radius factor, equipment
positions), objectives (length, bends, cost, volume, peak stress) and constraints, all
validated. Hard constraints remain pass/fail checks, never penalties.

**H2 Verified optimiser.** Seeded search (grid and random first, then a small Bayesian
optimiser) where every candidate is fully generated and verified; failing candidates are
rejected. Produces a Pareto set.
*Done when:* the same seed gives the same result.

**H3 Optimisation report and view.** Report of the chosen design, its trade-offs and
sensitivity, with rationale; web view of the Pareto set.

### Layer I: platform

**I1 Background jobs.** Routing, FEA and optimisation as jobs with a status API
(in-process worker first, an interface for an external queue later).

**I2 Run cache and storage interface.** Content-addressed reuse of identical runs; a
storage interface with a local implementation and room for object storage.

**I3 API versioning and contract tests.** Versioned API paths and OpenAPI snapshot tests.

## Needs Kushal

These cannot be done by an unattended run:

- Review and merge roadmap pull requests.
- Install ODA File Converter where DWG export is wanted (D1), if its licence suits the use.
- Provide `ANTHROPIC_API_KEY` in the deployment for real assistant answers (F2 onwards).
- Supply real DWGs, standards data and material tables to replace synthetic ones.

## Daily run protocol

The scheduled task follows these steps each run:

1. Read `AGENTS.md` and this file on `main`. List open pull requests whose branch starts
   with `roadmap/` (use the GitHub REST API; GraphQL is unavailable).
2. Update statuses: merged roadmap PRs become `done`.
3. If an open roadmap PR has review comments asking for changes, address them on that
   branch first and stop if that fills the run.
4. If three roadmap PRs are already open, do not start new work: improve tests or docs on
   the open ones and report that review is needed.
5. Otherwise pick the first `todo` milestone whose dependencies are `done` or `in-review`.
   Branch `roadmap/<id>-<slug>` from `main`, or from the newest open roadmap branch when a
   dependency is only in review (and target the PR at that branch).
6. Implement it with tests. Run `pytest`, `ruff check .` and `ruff format --check .`; all
   must pass. Never weaken or delete an existing test or check to make work pass.
7. Set the milestone to `in-review` with the PR link in this file, commit, push and open a
   pull request describing what was built, how it was verified and what is left.
8. If blocked by something only Kushal can provide, mark the milestone `blocked` with the
   reason and move on to the next eligible milestone.
9. Never push to `main`, merge, force-push or rewrite history.
10. When every milestone is `done`, open no PR and report that the roadmap is finished.
