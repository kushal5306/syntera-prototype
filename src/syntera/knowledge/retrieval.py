"""Deterministic local retrieval: BM25 over knowledge, rationale and drawing graphs (E2).

Nothing leaves the machine and nothing is learned: documents are tokenised with a fixed
rule, scored with Okapi BM25, and ties are broken by document id, so the same corpus and
query always give the same ranking. An embedding backend can implement the same
:class:`Retriever` protocol later; this keyword index stays the reference.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from syntera.knowledge.store import KnowledgeBase
from syntera.rationale import Rationale

if TYPE_CHECKING:
    from syntera.drawings.graph import DrawingGraph

K1 = 1.5
B = 0.75
SCORE_DIGITS = 6
TOKEN = re.compile(r"[a-z0-9_]+(?:[.\-][a-z0-9_]+)*")
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in",
        "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were",
        "what", "which", "why", "with", "how", "does", "do", "did", "not", "no",
    }
)  # fmt: skip

DocumentKind = Literal["knowledge", "rationale", "drawing"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Document(StrictModel):
    id: str = Field(min_length=1)
    kind: DocumentKind
    title: str
    text: str
    # what the document points at: a knowledge id, rationale path or drawing handle
    target: str


class Hit(StrictModel):
    id: str
    kind: DocumentKind
    title: str
    target: str
    score: float
    matched: tuple[str, ...]


class Retriever(Protocol):
    def search(self, query: str, limit: int = 10) -> list[Hit]: ...


def _stem(token: str) -> str:
    """A tiny fixed suffix rule so 'bends' finds 'bend'; never language-model based."""
    for suffix in ("ies", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix) and not token.endswith("ss"):
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN.findall(text.lower()):
        pieces = re.split(r"[._\-]", raw)
        parts = [raw, *pieces] if len(pieces) > 1 else [raw]
        for part in parts:
            if part and part not in STOPWORDS and (len(part) > 1 or part.isdigit()):
                tokens.append(_stem(part))
    return tokens


class BM25Index:
    """Okapi BM25 over a fixed document list (sorted by id; ids must be unique)."""

    def __init__(self, documents: Iterable[Document]):
        docs = sorted(documents, key=lambda document: document.id)
        ids = [document.id for document in docs]
        if len(set(ids)) != len(ids):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate document ids: {duplicates}")
        self.documents: tuple[Document, ...] = tuple(docs)
        self._terms = [Counter(tokenize(f"{d.title} {d.text}")) for d in docs]
        self._lengths = [sum(terms.values()) for terms in self._terms]
        self._average = (sum(self._lengths) / len(docs)) if docs else 0.0
        frequency: Counter[str] = Counter()
        for terms in self._terms:
            frequency.update(terms.keys())
        total = len(docs)
        self._idf = {
            term: math.log((total - count + 0.5) / (count + 0.5) + 1.0)
            for term, count in frequency.items()
        }

    def __len__(self) -> int:
        return len(self.documents)

    def search(
        self, query: str, limit: int = 10, kinds: Sequence[DocumentKind] | None = None
    ) -> list[Hit]:
        terms = list(dict.fromkeys(tokenize(query)))
        hits: list[Hit] = []
        for index, document in enumerate(self.documents):
            if kinds is not None and document.kind not in kinds:
                continue
            counts, length = self._terms[index], self._lengths[index]
            score = 0.0
            matched = []
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                matched.append(term)
                norm = K1 * (1 - B + B * length / (self._average or 1.0))
                score += self._idf[term] * frequency * (K1 + 1) / (frequency + norm)
            if score > 0:
                hits.append(
                    Hit(
                        id=document.id,
                        kind=document.kind,
                        title=document.title,
                        target=document.target,
                        score=round(score, SCORE_DIGITS),
                        matched=tuple(matched),
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return hits[:limit]


# ---- corpora ----------------------------------------------------------------------------


def knowledge_documents(kb: KnowledgeBase) -> list[Document]:
    documents = []
    for entry in kb.entries:
        parts = [
            entry.summary,
            entry.detail,
            " ".join(entry.keywords),
            " ".join(entry.rules),
            " ".join(entry.materials),
            " ".join(citation.source for citation in entry.citations),
        ]
        documents.append(
            Document(
                id=f"knowledge:{entry.id}",
                kind="knowledge",
                title=entry.title,
                text=" ".join(part for part in parts if part),
                target=entry.id,
            )
        )
    return documents


def rationale_documents(records: Iterable[Rationale], scope: str) -> list[Document]:
    """One document per record; ``scope`` names the run or design they belong to."""
    documents = []
    for record in records:
        words = record.path.replace(".", " ").replace("_", " ")
        text = f"{words} {record.text()} {record.rule_id or ''} {record.source}"
        documents.append(
            Document(
                id=f"rationale:{scope}:{record.path}",
                kind="rationale",
                title=f"{record.path} = {record.value}",
                text=text,
                target=record.path,
            )
        )
    return documents


def drawing_documents(graph: DrawingGraph, drawing: str) -> list[Document]:
    """Nodes (with their labels and tags), dimensions and the title block of a graph."""
    labels = {label.handle: label.text for label in graph.labels}
    documents = []
    for node in graph.nodes:
        texts = [labels[h] for h in node.labels if h in labels]
        text = " ".join([node.kind, node.layer, node.type, *node.tags, *texts, node.ref or ""])
        title = ", ".join(node.tags) or f"{node.type} on {node.layer}"
        documents.append(
            Document(
                id=f"drawing:{drawing}:{node.handle}",
                kind="drawing",
                title=title,
                text=text,
                target=node.handle,
            )
        )
    for dimension in graph.dimensions:
        value = "" if dimension.measurement is None else f"{dimension.measurement:g}"
        documents.append(
            Document(
                id=f"drawing:{drawing}:{dimension.handle}",
                kind="drawing",
                title=f"{dimension.kind} dimension {value}".strip(),
                text=f"dimension {dimension.kind} {value} {dimension.text} {dimension.ref or ''}",
                target=dimension.handle,
            )
        )
    if graph.title_block:
        fields = " ".join(f"{key} {value}" for key, value in graph.title_block.items())
        documents.append(
            Document(
                id=f"drawing:{drawing}:title-block",
                kind="drawing",
                title="title block",
                text=f"title block {fields}",
                target="title-block",
            )
        )
    return documents


def build_index(
    kb: KnowledgeBase,
    rationale: Iterable[tuple[str, Iterable[Rationale]]] = (),
    graphs: Iterable[tuple[str, DrawingGraph]] = (),
) -> BM25Index:
    """One index over the knowledge store, scoped rationale records and drawing graphs."""
    documents = knowledge_documents(kb)
    for scope, records in rationale:
        documents += rationale_documents(records, scope)
    for name, graph in graphs:
        documents += drawing_documents(graph, name)
    return BM25Index(documents)
