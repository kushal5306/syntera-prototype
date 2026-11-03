"""Deterministic local BM25 retrieval over knowledge, rationale and drawings (E2)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from syntera.design import design_from_yaml
from syntera.knowledge import load_knowledge
from syntera.knowledge.retrieval import (
    BM25Index,
    Document,
    build_index,
    knowledge_documents,
    rationale_documents,
    tokenize,
)

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def _doc(doc_id: str, text: str, kind="knowledge") -> Document:
    return Document(id=doc_id, kind=kind, title=doc_id, text=text, target=doc_id)


def test_tokenizer_is_a_fixed_rule():
    assert tokenize("Why are the Bends 1.5D on P-101?") == [
        "bend",
        "1.5d",
        "1",
        "5d",
        "p-101",
        "101",
    ]
    assert tokenize("wall_thickness rules.x.v1") == [
        "wall_thickness", "wall", "thickness", "rules.x.v1", "rule", "v1",
    ]  # fmt: skip
    assert tokenize("the of and") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("why is the bend radius 1.5 times OD", "kb.rule.long_radius_bend"),
        ("minimum wall thickness for pressure", "kb.rule.hoop_stress_wall"),
        ("stainless steel for water", "kb.material.stainless_316"),
        ("clearance between pipes", "kb.rule.clearance"),
        ("how is the drawing scale chosen", "kb.practice.drawing_scale"),
        ("which line is routed first", "kb.practice.largest_bore_first"),
        ("maximum velocity of steam", "kb.rule.maximum_velocity"),
        ("rules.wall_thickness_hoop_stress.v1", "kb.rule.hoop_stress_wall"),
    ],
)
def test_knowledge_questions_rank_the_right_entry_first(query, expected):
    index = BM25Index(knowledge_documents(load_knowledge()))
    assert index.search(query, 1)[0].target == expected


def test_ranking_is_stable_under_input_order_and_ties_break_by_id():
    docs = [_doc(f"d{i}", text) for i, text in enumerate(
        ["pipe bend radius", "pipe clearance", "bend", "pump nozzle", "pipe bend radius"]
    )]  # fmt: skip
    expected = BM25Index(docs).search("pipe bend radius", 10)
    for seed in range(5):
        shuffled = docs[:]
        random.Random(seed).shuffle(shuffled)
        assert BM25Index(shuffled).search("pipe bend radius", 10) == expected
    assert [hit.id for hit in expected[:2]] == ["d0", "d4"]  # identical text: id order
    assert expected[0].score == expected[1].score > expected[2].score
    assert expected[0].matched == ("pipe", "bend", "radiu")


def test_limit_kinds_no_match_and_duplicates():
    docs = [_doc("a", "pipe bend"), _doc("b", "pipe", kind="rationale"), _doc("c", "pump")]
    index = BM25Index(docs)
    assert [hit.id for hit in index.search("pipe", 1)] == ["b"]
    assert [hit.id for hit in index.search("pipe", kinds=["knowledge"])] == ["a"]
    assert index.search("valve") == []
    assert BM25Index([]).search("pipe") == []
    with pytest.raises(ValueError, match="duplicate document ids"):
        BM25Index([_doc("a", "x"), _doc("a", "y")])


def test_design_rationale_is_searchable_with_its_rule():
    design = design_from_yaml(SIZED)
    index = build_index(load_knowledge(), [("demo", design.rationale)])
    hits = index.search("outer diameter tube size from flow", 5, kinds=["rationale"])
    assert hits[0].target == "lines.main.tube.outer_diameter"
    assert hits[0].id == "rationale:demo:lines.main.tube.outer_diameter"
    assert len(rationale_documents(design.rationale, "demo")) == len(design.rationale)
