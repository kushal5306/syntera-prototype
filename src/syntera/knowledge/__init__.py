"""Knowledge store: rules, materials and practices with paraphrased citations (E1).

Entries are YAML files under ``syntera/knowledge/entries``. Each one explains a rule,
material or practice in Syntera's own words and cites where the idea comes from; no
standard or copyrighted text is reproduced. Entries describing synthetic tables say so.
Knowledge explains values; it never decides whether a design passes.
"""

from syntera.knowledge.store import (
    KNOWN_RULE_IDS,
    Citation,
    KnowledgeBase,
    KnowledgeEntry,
    KnowledgeError,
    load_knowledge,
)

__all__ = [
    "KNOWN_RULE_IDS",
    "Citation",
    "KnowledgeBase",
    "KnowledgeEntry",
    "KnowledgeError",
    "load_knowledge",
]
