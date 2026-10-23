"""Deterministic engineering rule library.

Pure, versioned functions that choose design values (line size, wall thickness, bend
radius, clearance, material) and return each value with its rationale. Rules propose
values only; they never set, relax or override an assurance verdict. Their tables are
synthetic demonstration data (see ``syntera.rules.tables``) and are not code-certified.
"""

from syntera.rules.line import LineSizing, LineSizingSpec, size_line, sized_config
from syntera.rules.sizing import RULE_LIBRARY_VERSION, RULES, RuleError, RuleResult

__all__ = [
    "RULES",
    "RULE_LIBRARY_VERSION",
    "LineSizing",
    "LineSizingSpec",
    "RuleError",
    "RuleResult",
    "size_line",
    "sized_config",
]
