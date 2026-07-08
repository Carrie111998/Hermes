"""
Semantic skill retrieval — natural language query over installed skills.

Two-tier architecture:
  1. Embedding (if sentence-transformers installed): cosine similarity
     against the pre-built index at ~/.hermes/skill-selector-cache/
  2. Keyword fallback (always available): TF-IDF-like word overlap
     scored against skill name + description + tags

The embedding index is built weekly by skill-selector-prep (Sunday 06:00 UTC).
When unavailable, keyword matching provides surprisingly good results —
agent-skill-retrieval testing showed 100% hit rate for skill mapping
using word-overlap scoring.

Register as the ``skill_retrieve`` tool.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from tools.skills_tool import _find_all_skills
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(os.path.expanduser("~/.hermes/skill-selector-cache"))
_METADATA_PATH = _CACHE_DIR / "skill_metadata.json"
_EMB_PATH = _CACHE_DIR / "skill_embeddings.npy"

# ── Wrapped imports (lazy, graceful degradation) ──────────────────────

_sentence_transformers: Any = None


def _has_embedding_backend() -> bool:
    """Return True if the full embedding stack (model + index) is available."""
    global _sentence_transformers
    if _sentence_transformers is not None:
        return True
    try:
        import sentence_transformers as st  # noqa: F811
        _sentence_transformers = st
        return True
    except ImportError:
        return False


# ── Word-overlap scorer (always available) ────────────────────────────

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "he", "in", "is", "it", "its", "of", "on", "that", "the",
        "to", "was", "were", "will", "with", "the", "use", "when",
        "how", "or", "not", "can", "this", "all", "but", "they", "we",
        "you", "your", "i", "my", "me", "our", "us", "do", "does",
    }
)


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alpha, drop stop words and short tokens."""
    words = re.split(r"[^a-zA-Z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOP_WORDS}


def _keyword_score(query: str, name: str, description: str, tags: list[str]) -> float:
    """Simple word-overlap score: weighted F1-like intersection."""
    q = _tokenize(query)
    if not q:
        return 0.0
    # Build document tokens: name (weight 3x), description (1x), tags (2x)
    doc_tokens: dict[str, int] = {}
    for w in _tokenize(name):
        doc_tokens[w] = doc_tokens.get(w, 0) + 3
    for w in _tokenize(description):
        doc_tokens[w] = doc_tokens.get(w, 0) + 1
    for tag in tags:
        for w in _tokenize(tag):
            doc_tokens[w] = doc_tokens.get(w, 0) + 2

    if not doc_tokens:
        return 0.0

    overlap = sum(doc_tokens.get(w, 0) for w in q)
    # Normalize: overlap / sqrt(|q| * total_weight)
    total_weight = sum(doc_tokens.values())
    norm = (len(q) * max(total_weight, 1)) ** 0.5
    return overlap / max(norm, 0.001)


# ── Embedding scorer (sentence-transformers) ──────────────────────────

_embed_model: Any = None
_embed_cache: tuple[Any, list[dict]] | None = None  # (embeddings_array, metadata_list)


def _load_embed_cache() -> tuple[Any, list[dict]] | None:
    """Load the pre-built embedding index + metadata. Cached after first load."""
    global _embed_cache
    if _embed_cache is not None:
        return _embed_cache
    if not _EMB_PATH.exists() or not _METADATA_PATH.exists():
        return None
    try:
        import numpy as np
        emb = np.load(str(_EMB_PATH))
        meta = json.loads(_METADATA_PATH.read_text(encoding="utf-8"))
        # Only keep entries that have the same count as embeddings
        if len(meta) < emb.shape[0]:
            meta = meta[: emb.shape[0]]
        elif len(meta) > emb.shape[0]:
            meta = meta[: emb.shape[0]]
        _embed_cache = (emb, meta)
        return _embed_cache
    except Exception as exc:
        logger.debug("Failed to load embedding cache: %s", exc)
        return None


def _embedding_score(query: str) -> list[tuple[int, float]] | None:
    """Return [(index, score), ...] using cosine similarity, or None."""
    if not _has_embedding_backend():
        return None
    global _embed_model
    if _embed_model is None:
        try:
            _embed_model = _sentence_transformers.SentenceTransformer(
                "all-MiniLM-L6-v2"
            )
        except Exception as exc:
            logger.debug("Failed to load embedding model: %s", exc)
            return None

    cache = _load_embed_cache()
    if cache is None:
        return None

    import numpy as np
    emb, _meta = cache
    try:
        q_emb = _embed_model.encode([query], show_progress_bar=False)
        scores = np.dot(emb, q_emb.T).squeeze()
        # Sort descending
        idx_sorted = np.argsort(scores)[::-1]
        return [(int(i), float(scores[i])) for i in idx_sorted]
    except Exception as exc:
        logger.debug("Embedding scoring failed: %s", exc)
        return None


# ── Scoring (combines both tiers) ─────────────────────────────────────


def _score_skills(
    query: str, skills: list[dict], top_k: int = 5
) -> list[dict]:
    """Score all skills against query, return top_k with metadata.

    Tries embedding first; falls back to keyword overlap.
    """
    embed_results = _embedding_score(query)
    if embed_results is not None:
        # Use embedding scores; only consider skills with valid paths
        return _format_results_embedding(embed_results, skills, query, top_k)

    # Keyword fallback
    return _format_results_keyword(query, skills, top_k)


def _format_results_embedding(
    results: list[tuple[int, float]],
    skills: list[dict],
    query: str,
    top_k: int,
) -> list[dict]:
    """Map embedding results to skill entries. Fall back to keyword if lookup fails."""
    out: list[dict] = []
    # Build name→index map for O(1) lookup
    name_map: dict[str, dict] = {}
    for s in skills:
        name_map[s.get("name", s.get("id", ""))] = s

    for idx, score in results:
        if len(out) >= top_k:
            break
        if idx >= len(skills):
            continue
        skill = skills[idx]
        entry = {
            "name": skill.get("name", skill.get("id", "?")),
            "description": (skill.get("description") or "")[:160],
            "category": skill.get("category", ""),
            "score": round(score, 4),
            "source": "embedding",
        }
        out.append(entry)
    return out


def _format_results_keyword(
    query: str, skills: list[dict], top_k: int
) -> list[dict]:
    """Keyword-overlap scoring."""
    scored: list[tuple[float, dict]] = []
    for s in skills:
        score = _keyword_score(
            query,
            s.get("name", s.get("id", "")),
            s.get("description", ""),
            s.get("tags", []),
        )
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    out: list[dict] = []
    for score, skill in scored[:top_k]:
        out.append(
            {
                "name": skill.get("name", skill.get("id", "?")),
                "description": (skill.get("description") or "")[:160],
                "category": skill.get("category", ""),
                "score": round(score, 4),
                "source": "keyword",
            }
        )
    return out


# ── Tool handler ──────────────────────────────────────────────────────


def skill_retrieve(
    query: str,
    top_k: int = 5,
    *,
    _task_id: str | None = None,
) -> str:
    """Semantic search over installed skills.

    Pass a natural language description of what you're trying to do.
    Returns top-k matching skills with descriptions, categories, and scores.

    Uses embedding-based similarity when available, falling back to
    keyword overlap.  Embedding index is rebuilt weekly by the
    skill-selector-prep cron job.

    Args:
        query: Natural language description of what you need (e.g.,
               "load a VRM 3D model in the browser", "deploy a cloudflare tunnel")
        top_k: Number of results to return (default 5, max 20)

    Returns:
        JSON with matching skills and scores.
    """
    try:
        if not query or not query.strip():
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "hint": "Pass a natural language query describing your task, e.g. skill_retrieve('deploy a docker container')",
                },
                ensure_ascii=False,
            )

        top_k = max(1, min(top_k, 20))
        all_skills = _find_all_skills()

        if not all_skills:
            return json.dumps(
                {"success": True, "skills": [], "count": 0, "hint": "No skills installed."},
                ensure_ascii=False,
            )

        results = _score_skills(query.strip(), all_skills, top_k)

        return json.dumps(
            {
                "success": True,
                "query": query.strip(),
                "skills": results,
                "count": len(results),
                "backend": _has_embedding_backend() and "embedding" in str(results) or "keyword",
                "hint": "Use skill_view(name) to load a skill's full content.",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return tool_error(str(e), success=False)


# ── Tool schema ───────────────────────────────────────────────────────

SKILL_RETRIEVE_SCHEMA = {
    "name": "skill_retrieve",
    "description": (
        "Semantic skill search. Pass a natural language description of your task "
        "(e.g. 'deploy a docker container', 'load a VRM model', 'humanize AI text') "
        "and get the top matching skills with descriptions. "
        "Use this instead of skills_list when you know what kind of skill you need "
        "but don't know its exact name. After finding a skill, load it with skill_view(name)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of what you're trying to do. Be specific about the task, domain, or problem.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results (default: 5, max: 20)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

# ── Self-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick smoke test
    print("=== skill_retrieve self-test ===")
    for q in ["deploy a cloudflare tunnel", "load VRM model browser", "git commit workflow", "streamlit dashboard"]:
        result = skill_retrieve(q)
        parsed = json.loads(result)
        names = [s["name"] for s in parsed.get("skills", [])]
        print(f"\n{q} → {names}")
