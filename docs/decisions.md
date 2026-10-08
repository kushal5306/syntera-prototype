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

