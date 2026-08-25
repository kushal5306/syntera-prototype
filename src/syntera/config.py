"""Configuration loading."""

from pathlib import Path

import yaml

from syntera.schemas import DemoConfig


def load_config(path: str | Path) -> DemoConfig:
    """Load and validate a YAML demonstration configuration."""
    with Path(path).open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    return DemoConfig.model_validate(raw)
