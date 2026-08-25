# Syntera prototype contributor guide

## Architecture

Typed Pydantic schemas feed a deterministic pipeline: synthetic CadQuery geometry,
NumPy occupancy, custom A* routing, CAD tube construction, exact geometry verification,
and JSON/STEP reporting. All dimensions are millimetres.

## Commands

```bash
python -m pip install -e ".[dev]"
python -m syntera.cli demo --config examples/demo_skid.yaml --output outputs/demo
pytest
ruff check .
ruff format --check .
```

## Standards and tests

Use typed, small modules; deterministic iteration/tie-breaking; clear failure diagnostics;
and no proprietary data, credentials, caches, or generated CAD in Git. Add unit tests for
new constraints and an integration test for output changes. Safety-critical engineering
checks must remain deterministic and must never be delegated to probabilistic AI. Hard
constraints may not be traded against routing objectives.

