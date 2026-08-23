"""Deterministic, local BM25 ranking for skill routing metadata."""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SCALAR_FIELDS = ("qualified_name", "name", "category", "description")
_SEQUENCE_FIELDS = (
    "triggers",
    "tags",
    "related_skills",
    "required_commands",
    "required_environment_variables",
)
_K1 = 1.5
_B = 0.75


def _tokenize(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _TOKEN_RE.findall(normalized)


def _normalized_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return []
    cleaned = {str(item).strip() for item in values if str(item).strip()}
    return sorted(cleaned, key=lambda item: (item.casefold(), item))


def _canonical_document(skill: Mapping[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {
        field: str(skill.get(field) or "").strip() for field in _SCALAR_FIELDS
    }
    for field in _SEQUENCE_FIELDS:
        document[field] = _normalized_values(skill.get(field))
    return document


def _document_tokens(document: Mapping[str, Any]) -> list[str]:
    text: list[str] = [str(document[field]) for field in _SCALAR_FIELDS]
    for field in _SEQUENCE_FIELDS:
        text.extend(document[field])
    return _tokenize(" ".join(text))


def _tie_key(document: Mapping[str, Any]) -> tuple[str, ...]:
    values = (
        str(document["qualified_name"]),
        str(document["category"]),
        str(document["name"]),
    )
    return tuple(part for value in values for part in (value.casefold(), value))


def _serialize_documents(documents: list[dict[str, Any]]) -> str:
    ordered = sorted(documents, key=_tie_key)
    return json.dumps(
        ordered,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@lru_cache(maxsize=1)
def _build_index(
    serialized_documents: str,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[tuple[str, ...], ...],
    tuple[tuple[str, int], ...],
    float,
    str,
]:
    """Build and cache one immutable BM25 corpus from canonical metadata."""
    documents = tuple(json.loads(serialized_documents))
    tokenized = tuple(tuple(_document_tokens(document)) for document in documents)
    document_frequencies: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequencies.update(set(tokens))
    average_length = (
        sum(len(tokens) for tokens in tokenized) / len(tokenized) if tokenized else 0.0
    )
    fingerprint = hashlib.sha256(serialized_documents.encode("utf-8")).hexdigest()[:16]
    return (
        documents,
        tokenized,
        tuple(sorted(document_frequencies.items())),
        average_length,
        fingerprint,
    )


def rank_skills(
    skills: Iterable[Mapping[str, Any]], query: str, limit: int
) -> dict[str, Any]:
    """Rank canonical skill metadata with deterministic Okapi BM25."""
    skill_list = list(skills)
    canonical_documents = [_canonical_document(skill) for skill in skill_list]
    serialized_documents = _serialize_documents(canonical_documents)
    (
        documents,
        tokenized,
        document_frequency_items,
        average_length,
        index_fingerprint,
    ) = _build_index(serialized_documents)
    query_tokens = _tokenize(query)
    total = len(documents)
    document_frequencies = dict(document_frequency_items)

    query_frequencies = Counter(query_tokens)
    scored: list[tuple[bool, float, dict[str, Any]]] = []
    normalized_query_name = " ".join(query_tokens)
    for document, tokens in zip(documents, tokenized):
        frequencies = Counter(tokens)
        score = 0.0
        for term, query_frequency in query_frequencies.items():
            frequency = frequencies[term]
            if not frequency:
                continue
            document_frequency = document_frequencies[term]
            inverse_document_frequency = math.log(
                1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            length_normalization = 1.0 - _B
            if average_length:
                length_normalization += _B * len(tokens) / average_length
            score += (
                query_frequency
                * inverse_document_frequency
                * (frequency * (_K1 + 1.0))
                / (frequency + _K1 * length_normalization)
            )

        exact_name = " ".join(_tokenize(str(document["name"]))) == normalized_query_name
        scored.append((exact_name, score, document))

    scored.sort(key=lambda item: (not item[0], -item[1], _tie_key(item[2])))
    ranked = []
    display_descriptions = {
        _tie_key(document): str(
            skill.get("display_description", document["description"])
        )
        for skill, document in zip(skill_list, canonical_documents)
    }
    for rank, (_, score, document) in enumerate(scored[: max(0, limit)], start=1):
        ranked.append({
            "rank": rank,
            "name": document["name"],
            "category": document["category"] or None,
            "description": display_descriptions[_tie_key(document)],
            "score": round(score, 6),
        })

    return {
        "skills": ranked,
        "total_candidates": total,
        "index_fingerprint": index_fingerprint,
    }
