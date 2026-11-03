"""Knowledge store: schema, loader and links to every rule Syntera uses (E1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from syntera.knowledge import KNOWN_RULE_IDS, KnowledgeError, load_knowledge
from syntera.rules import RULES
from syntera.rules.tables import MATERIAL_BANDS

SOURCE = Path(__file__).parents[2] / "src" / "syntera"
RULE_LITERAL = re.compile(r"\"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\.v[0-9]+)\"")

VALID = {
    "id": "kb.rule.example",
    "kind": "rule",
    "title": "Example rule",
    "summary": "A short paraphrased summary.",
    "detail": "A short paraphrased detail.",
    "rules": ["rules.bend_radius_long_radius.v1"],
    "synthetic": False,
    "citations": [{"kind": "textbook", "source": "Some textbook"}],
}


def _store(tmp_path: Path, *entries: dict) -> Path:
    folder = tmp_path / "kb"
    folder.mkdir(exist_ok=True)
    (folder / "entries.yaml").write_text(yaml.safe_dump(list(entries)), encoding="utf-8")
    return folder


def test_every_entry_validates_and_ids_are_sorted_and_unique():
    kb = load_knowledge()
    ids = [entry.id for entry in kb.entries]
    assert ids == sorted(set(ids))
    assert len(ids) >= 10
    assert all(entry.citations for entry in kb.entries)


def test_every_a2_rule_links_to_an_entry():
    kb = load_knowledge()
    for rule_id in RULES:
        assert kb.for_rule(rule_id), rule_id


def test_every_rule_id_in_the_code_is_registered_and_explained():
    in_code = set()
    for path in SOURCE.rglob("*.py"):
        in_code.update(RULE_LITERAL.findall(path.read_text(encoding="utf-8")))
    assert in_code <= KNOWN_RULE_IDS, sorted(in_code - KNOWN_RULE_IDS)
    kb = load_knowledge()
    linked = {rule for entry in kb.entries for rule in entry.rules}
    assert linked == KNOWN_RULE_IDS


def test_every_table_material_has_an_entry_marked_synthetic():
    kb = load_knowledge()
    for band in MATERIAL_BANDS:
        (entry,) = kb.for_material(band.material.name)
        assert entry.synthetic and entry.kind == "material"
    assert kb.get("kb.material.carbon_steel").title.startswith("Carbon steel")
    with pytest.raises(KnowledgeError, match="unknown knowledge entry"):
        kb.get("kb.rule.missing")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"rules": ["rules.made_up.v1"]}, "unknown rule ids"),
        ({"rules": []}, "must link at least one rule"),
        ({"kind": "material"}, "must name its material"),
        ({"synthetic": True}, "must cite the synthetic table"),
        ({"summary": 'It says "' + "word " * 20 + '" verbatim.'}, "paraphrase instead"),
        ({"summary": "x" * 401}, "at most 400"),
        ({"id": "rule.example"}, "pattern"),
        ({"citations": []}, "at least 1"),
    ],
)
def test_invalid_entries_are_refused_with_a_reason(tmp_path, change, message):
    with pytest.raises(KnowledgeError, match=message):
        load_knowledge(_store(tmp_path, {**VALID, **change}))


def test_duplicates_bad_yaml_and_empty_stores_are_refused(tmp_path):
    with pytest.raises(KnowledgeError, match="duplicate"):
        load_knowledge(_store(tmp_path, VALID, VALID))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="not valid YAML"):
        load_knowledge(bad)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(KnowledgeError, match="no knowledge entries"):
        load_knowledge(empty)
