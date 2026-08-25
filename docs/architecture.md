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

