"""Configuration loading."""

from pathlib import Path

import yaml

from syntera.rationale import Rationale
from syntera.rules.line import sized_config
from syntera.schemas import DemoConfig


def load_sized_config(path: str | Path) -> tuple[DemoConfig, list[Rationale]]:
    """Load a YAML configuration, sizing its line first when it has ``line_sizing``.

    STEP paths resolve against the file's directory. The returned records explain the
    values the rules chose; pass them to ``execute_pipeline`` so that no rule-chosen value
    is reported as stated by the user. A configuration without ``line_sizing`` validates
    unchanged and returns no records.
    """
    source = Path(path)
    with source.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        return DemoConfig.model_validate(raw), []
    for obstacle in raw.get("obstacles") or []:
        if isinstance(obstacle, dict) and obstacle.get("type") == "step" and "path" in obstacle:
            step_path = Path(str(obstacle["path"]))
            if not step_path.is_absolute():
                obstacle["path"] = str((source.parent / step_path).resolve())
    return sized_config(raw)


def load_config(path: str | Path) -> DemoConfig:
    """Load and validate a YAML configuration (sized from ``line_sizing`` when present)."""
    return load_sized_config(path)[0]
