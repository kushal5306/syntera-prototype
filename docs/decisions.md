# Engineering decisions

1. **Millimetres only.** The schema requires the literal unit `mm`; implicit conversion
   would make unsafe ambiguity possible.
2. **Custom A*.** A small implementation exposes direction/run-length state and stable
   tie-breaking more clearly than adapting a generic graph library.
3. **Six-neighbour routing.** Axis-aligned motion creates manufacturable straight runs
   and unambiguous 90-degree elbows for this milestone.
4. **Analytical inflation.** Distances to box and cylinder solids avoid voxel morphology
   approximations when classifying nodes.
5. **Independent CAD verification.** Grid acceptance is necessary but never sufficient.
6. **Fail closed.** Invalid configuration, no path, or CAD construction failure returns
   a diagnostic and a failing assurance report rather than questionable geometry.
7. **Proprietary licence.** All rights reserved; see `LICENSE`. No open-source licence is granted.
8. **Shell FEA of the tube wall.** S8R mid-surface shells capture hoop stress and
   bend ovalization that beam elements would hide, while staying a structured,
   dependency-free mesh. Solid meshing waits for robust STEP import.
9. **One deck per load case.** Independent steps avoid CalculiX load carry-over
   between steps and keep each acceptance result traceable to one input file.
10. **Acceptance outside the solver.** Thresholds live in the validated schema and are
    checked by deterministic Python against parsed results; missing results fail.
11. **Material-only defeaturing.** Simplifying imported geometry may add material but never
    remove it; each feature removal is checked by boolean containment, and verification
    uses the unmodified import so defeaturing cannot hide a collision.
12. **DWG through an external converter.** DWG is read only by converting it to DXF with a
    separate program (ODA File Converter when installed, else LibreDWG), never by linking
    a DWG library. This keeps GPL and proprietary converter code out of the Syntera process,
    records which converter produced each DXF, and lets a deployment swap converters.
13. **Drawings become 3D only through stated assumptions.** A plan cannot define heights,
    so every inferred value carries its source (drawing, default or user) in the piping
    check report. Unrecognised drawings and schematics are refused rather than guessed,
    and a route saving is reported only for a Syntera route that passes every exact check.
14. **P&IDs become 3D only as a proposed layout.** P&ID graphics positions are never read
    as plant positions. Equipment is placed and lines routed deterministically, each route
    passes the same exact checks, and the report says the layout is a proposal.
15. **Rationale is recorded, not guessed.** Every value a run uses carries a record of its
    source, rule id and inputs, written by the same deterministic code that chose it.
    Callers that build a configuration (from a P&ID or a drawing) pass their own records,
    so a rule-chosen value is never reported as the user's. Records explain values and are
    never evidence for, or inputs to, an assurance verdict.
16. **Rules propose values, never verdicts.** `syntera.rules` chooses design values (line
    size, wall, bend radius, clearance, material) with pure, versioned functions that
    return their rationale; changing a rule's behaviour bumps its id version. Values the
    input states are kept; a stated value that breaks a rule's hard limit (a wall below
    the hoop-stress minimum, two sources for one size) is refused, never overridden. Rule
    tables are synthetic until licensed data is supplied, and acceptance still comes only
    from the exact checks and FEA thresholds.
17. **The design document is the source of truth.** `design.json` is versioned, validated
    on load and written as canonical JSON, so identical input gives identical bytes and
    a content hash identifies a design. Run configurations are derived from it per line;
    rationale paths are namespaced (`parameters.`, `lines.<id>.`) so several lines can
    share one document. Older documents are upgraded by explicit one-step migrations and
    a newer or unmigratable version is refused. The document holds design values and
    their rationale only, never an assurance verdict.
