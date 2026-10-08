"""Configuration loading."""

from pathlib import Path

import yaml

from syntera.schemas import DemoConfig


def load_config(path: str | Path) -> DemoConfig:
    """Load and validate a YAML configuration; STEP paths resolve against its directory."""
    source = Path(path)
    with source.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if isinstance(raw, dict):
        for obstacle in raw.get("obstacles") or []:
            if isinstance(obstacle, dict) and obstacle.get("type") == "step" and "path" in obstacle:
                step_path = Path(str(obstacle["path"]))
                if not step_path.is_absolute():
                    obstacle["path"] = str((source.parent / step_path).resolve())
    return DemoConfig.model_validate(raw)
