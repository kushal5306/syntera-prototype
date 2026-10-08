# Architecture

The pipeline has one-way data flow:

```text
YAML -> Pydantic schemas -> analytical CAD + inflated occupancy
                              |
                              v
                     direction-aware A*
                              |
                              v
                 tangent/arc CAD tube sweep
                              |
                              v
            independent exact B-rep verification -> JSON/STEP
                              |
                              v
        (if assured) S8R shell mesh -> CalculiX decks + manifest
                              |
                              v
               FastAPI -> tessellated display meshes -> WebGL UI
```

All coordinates and tolerances use millimetres. Occupancy nodes are tube-centreline
positions. The hard inflation radius is `outer_diameter / 2 + minimum_clearance`;
workspace faces receive the same treatment. A* carries incoming direction and straight
run length, so it rejects reversals and corners that cannot accommodate the requested
bend radius. Its fixed neighbour order and monotonic tie counter make repeated runs
deterministic.

The CAD stage replaces each orthogonal corner with a tangent quarter-circle of the
specified radius and sweeps a circular profile along the resulting wire. Verification
does not trust the occupancy result: OpenCascade calculates tube/obstacle intersections,
exact shape distances, tube bounding-box clearance from workspace faces, and endpoint
direction alignment.

The web layer calls the same pipeline as the CLI. It may visualize tessellated
meshes and collect typed inputs, but it cannot issue an assurance pass itself.
Download endpoints expose only a fixed allowlist beneath the configured output
directory.

## Analysis decks

`syntera.analysis` runs only after the route passes assurance. It rebuilds the same
centreline as the CAD sweep (`geometry/centreline.py`), samples it at corner and
mid-side stations, and carries a parallel-transport frame through each bend so the
structured S8R grid has no twist. The initial frame axis is the first cardinal axis
least aligned with the starting direction, so meshes are reproducible. Element
normals point outward; CalculiX applies a positive shell pressure along the normal,
so internal pressure is a positive `P` load. Meshes whose worst element aspect ratio
exceeds the configured limit are rejected and no decks are written.

Each load case becomes its own deck that includes `mesh.inp`, clamps both port rings
(`NSTART`, `NEND`, DOF 1-6), and requests `.dat` displacement and stress output.
Decks carry a format version and the SHA-256 of the validated configuration plus
routed centreline, and contain no timestamps, so identical inputs give identical bytes.
`fea-evaluate` parses the `.dat` files, computes von Mises stress at every
integration point, and compares peaks with the manifest thresholds.
