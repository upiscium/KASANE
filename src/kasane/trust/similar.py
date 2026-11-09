"""Deterministic lexical similarity for duplicate hints on proposal.

Core stays deterministic (#40): this only *suggests* existing knowledge; the
agent decides whether its proposal is new or should have been a corroboration.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from kasane.vault.models import KnowledgeRecord, KnowledgeStatus

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+")
_LIVE = frozenset(
    {KnowledgeStatus.CANDIDATE, KnowledgeStatus.VERIFIED, KnowledgeStatus.CANONICAL}
)
_BODY_PREFIX_CHARS = 600


@dataclass(frozen=True)
class SimilarKnowledge:
    knowledge_ref: str
    title: str
    status: KnowledgeStatus
    score: float

    def summary(self) -> dict[str, str | float]:
        return {
            "knowledge_ref": self.knowledge_ref,
            "title": self.title,
            "status": self.status.value,
            "score": round(self.score, 3),
        }


def tokens(text: str) -> frozenset[str]:
    """Lowercased words plus CJK character bigrams (Japanese has no spaces)."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    found: set[str] = set(_WORD.findall(normalized))
    for run in _CJK.findall(normalized):
        if len(run) == 1:
            found.add(run)
        found.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(found)


def fingerprint(title: str, tags: Iterable[str], body: str) -> frozenset[str]:
    return tokens(" ".join([title, *tags, body[:_BODY_PREFIX_CHARS]]))


def find_similar(
    title: str,
    tags: Iterable[str],
    body: str,
    records: Iterable[KnowledgeRecord],
    *,
    exclude_ref: str | None = None,
    threshold: float = 0.25,
    limit: int = 5,
) -> list[SimilarKnowledge]:
    """Live records whose Jaccard overlap with the proposal meets ``threshold``."""

    probe = fingerprint(title, tags, body)
    if not probe:
        return []
    scored: list[SimilarKnowledge] = []
    for record in records:
        if record.status not in _LIVE or record.knowledge_ref == exclude_ref:
            continue
        other = fingerprint(record.title, _string_tags(record.tags), record.body)
        if not other:
            continue
        score = len(probe & other) / len(probe | other)
        if score >= threshold:
            scored.append(
                SimilarKnowledge(
                    record.knowledge_ref, record.title, record.status, score
                )
            )
    scored.sort(key=lambda item: (-item.score, item.knowledge_ref))
    return scored[:limit]


def _string_tags(tags: object) -> list[str]:
    if isinstance(tags, list):
        return [tag for tag in tags if isinstance(tag, str)]
    return []
