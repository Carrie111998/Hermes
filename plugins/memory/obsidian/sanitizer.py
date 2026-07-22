"""FTS5-query-sanitizer (ren). Porterad från holographic-providern."""

from __future__ import annotations

# Svenska + engelska stoppord (håll litet; FTS5 OR-recall är målet).
_STOPWORDS = frozenset((
    "och",
    "att",
    "det",
    "som",
    "en",
    "ett",
    "på",
    "är",
    "för",
    "med",
    "av",
    "till",
    "den",
    "har",
    "jag",
    "om",
    "inte",
    "de",
    "vi",
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "is",
    "it",
    "for",
    "on",
    "with",
    "my",
))
_FTS_SPECIAL = '"()*^:-+'
_TRANS = str.maketrans("", "", _FTS_SPECIAL)


def sanitize_fts_query(query: str) -> str:
    """Gör en naturlig fråga till ett FTS5-säkert OR-uttryck.

    Tokenizes, drops stopwords and <2-char tokens, strips FTS5 special chars,
    phrase-quotes each token, and OR-joins the result. Falls back to raw query
    if no tokens survive (never crashes; caller may see 0 results on raw FTS5
    errors, so wire with try/except sqlite3.OperationalError at call site).
    """
    if not query:
        return ""
    tokens: list[str] = []
    for raw in query.lower().split():
        cleaned = raw.strip(".,;:!?\"'()[]{}#@<>").translate(_TRANS)
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        tokens.append(f'"{cleaned}"')
    if not tokens:
        return query
    return " OR ".join(tokens)
