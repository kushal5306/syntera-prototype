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
7. **Licensing undecided.** No open-source licence is included.
8. **Shell FEA of the tube wall.** S8R mid-surface shells capture hoop stress and
   bend ovalization that beam elements would hide, while staying a structured,
   dependency-free mesh. Solid meshing waits for robust STEP import.
9. **One deck per load case.** Independent steps avoid CalculiX load carry-over
   between steps and keep each acceptance result traceable to one input file.
10. **Acceptance outside the solver.** Thresholds live in the validated schema and are
    checked by deterministic Python against parsed results; missing results fail.

