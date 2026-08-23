"""Korean-aware tokenisation for the FTS5 index.

SQLite's built-in tokenisers have no idea where a Korean word ends: they
treat ``임대차보증금반환청구`` as one indivisible token, so a search for
``보증금 반환`` matches nothing. The standard fix is character bigrams —
index ``임대``, ``대차``, ``차보``… and query the same way, which gives
substring matching with BM25 ranking and no external dependency.

Latin words, digits and legal citation shapes (제618조, 2018다255648) are
kept whole so they still match exactly.
"""

from __future__ import annotations

import re

_HANGUL = r"가-힣ᄀ-ᇿ㄰-㆏"
_TOKEN_RE = re.compile(rf"[{_HANGUL}]+|[A-Za-z]+|[0-9]+")

# Single-syllable Korean particles/suffixes carry no retrieval signal and
# blow up the index, so a lone bigram made only of these is dropped.
_STOP_BIGRAMS = frozenset({"습니", "니다", "합니", "하는", "에서", "으로", "입니", "있는", "해서"})


def hangul_bigrams(word: str) -> list[str]:
    if len(word) <= 1:
        return [word]
    return [word[i : i + 2] for i in range(len(word) - 1)]


def index_tokens(text: str) -> list[str]:
    """Tokens to store in (or query) the FTS index."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text or ""):
        word = match.group(0)
        if re.fullmatch(rf"[{_HANGUL}]+", word):
            if len(word) <= 4:
                # Short words are meaningful on their own (계약, 임대차).
                tokens.append(word)
            grams = [g for g in hangul_bigrams(word) if g not in _STOP_BIGRAMS]
            tokens.extend(grams)
        else:
            tokens.append(word.lower())
    return tokens


def index_blob(text: str) -> str:
    """Space-joined token string, which is what FTS5 actually indexes."""
    return " ".join(index_tokens(text))


def match_query(text: str, max_terms: int = 60) -> str:
    """Build an FTS5 MATCH expression from a natural-language question.

    OR-joined so a long question still retrieves partial matches; BM25 then
    sorts by how many rare terms actually hit.
    """
    seen: list[str] = []
    for token in index_tokens(text):
        if token not in seen:
            seen.append(token)
        if len(seen) >= max_terms:
            break
    if not seen:
        return ""
    return " OR ".join(f'"{token}"' for token in seen)
