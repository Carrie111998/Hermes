"""Skill retrieval: 5-layer architecture.


Layer 1: Hard triggers — exact keyword → direct return (100% deterministic)
Layer 2: FTS5 BM25 — clean text search (name + description only)
Layer 3: Synonym dictionary — independent bonus scoring layer
Layer 4: Dense Embedding — sentence-transformers cosine similarity
Layer 5: RRF fusion — combine layers 2-4, return top-k

Usage:
    retriever = get_skill_retriever()
    if retriever.is_ready():
        hints = retriever.retrieve("help me debug this", top_k=5)
"""

import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────
_DEFAULT_TOP_K = 5


def _retrieval_config() -> dict:
    """Return optional retrieval settings from config.yaml.

    Missing or malformed config is fail-open: retrieval remains enabled with
    the built-in top-k default. Behavioural settings belong in config.yaml,
    never in the secret-only environment file.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        skills = config.get("skills", {}) if isinstance(config, dict) else {}
        retrieval = skills.get("retrieval", {}) if isinstance(skills, dict) else {}
        return retrieval if isinstance(retrieval, dict) else {}
    except Exception:
        return {}


def _retrieval_enabled(config: dict) -> bool:
    value = config.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return value is not False


def _retrieval_top_k(config: dict) -> int:
    value = config.get("top_k", _DEFAULT_TOP_K)
    if isinstance(value, bool):
        return _DEFAULT_TOP_K
    try:
        return max(1, min(50, int(value)))
    except (TypeError, ValueError):
        return _DEFAULT_TOP_K

# RRF parameters
_RRF_K = 60
_RRF_W_FTS5 = 0.45      # FTS5 text matching
_RRF_W_SYN = 0.35       # Synonym dictionary
_RRF_W_EMB = 0.20       # Dense embedding
# Minimum RRF score to include (filters noise)
_MIN_RRF_SCORE = 0.003
# Confidence threshold: top-1 must score above this to be returned.
# Below this, the query likely doesn't match any skill well.
_CONFIDENCE_THRESHOLD = 0.015  # Empirically determined — filters noise effectively

_SINGLETON: "SkillRetriever | None" = None
_SINGLETON_LOCK = threading.Lock()

# ── Hard Triggers (Layer 1) ────────────────────────────────────
# Maps exact substring → skill name. Order matters: first match wins.
# These bypass all ranking — if query contains the trigger, return directly.
#
# CUSTOMIZATION: Replace the examples below with your own skill mappings.
# Run `python scripts/generate_config.py` to auto-generate from your skill library.
# See templates/hard_triggers.example.py for the format.
_HARD_TRIGGERS: list[tuple[str, str]] = [
    # ── Example triggers (replace with your own) ──
    # Format: ("trigger_keyword", "skill-name"),
    #
    # Engineering
    ("debug", "systematic-debugging"),
    ("调试", "systematic-debugging"),
    ("代码审查", "zh-code-review"),
    ("code review", "zh-code-review"),
    ("TDD", "test-driven-development"),
    ("测试驱动", "test-driven-development"),
    # Skills management
    ("创建技能", "skill-creation-guide"),
    ("SKILL.md", "skill-creation-guide"),
    ("找技能", "find-skills"),
    ("安装技能", "find-skills"),
]


def _is_subsequence(chars: list[str], text: str, max_gap: int = 3) -> bool:
    """Check if all chars appear in order within text, with max gap between consecutive matches.

    >>> _is_subsequence(['d', 'e', 'b', 'u', 'g'], 'please debug this')
    True
    >>> _is_subsequence(['d', 'b', 'g'], 'debug')
    False  # 'b' appears before 'd' in subsequence order
    >>> _is_subsequence(['技', '能', '指', '南'], '梳理一下现有的技能，弄个说明指南')
    False  # gap of 4 between 能 and 指 exceeds max_gap=3
    """
    pos = -1  # position of last match
    for c in chars:
        idx = text.find(c, pos + 1)
        if idx == -1:
            return False
        if pos >= 0 and idx - pos - 1 > max_gap:
            return False
        pos = idx
    return True


def get_skill_retriever() -> "SkillRetriever":
    """Return the global singleton, creating it on first call."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = SkillRetriever()
    return _SINGLETON


class SkillRetriever:
    """5-layer skill retrieval: Hard Trigger → FTS5 → Synonym → Embedding → RRF."""

    def __init__(self) -> None:
        self._ready = False
        self._loading = False
        self._error: str | None = None
        self._skill_names: list[str] = []
        self._skill_descs: list[str] = []
        self._doc_tokens: list[list[str]] = []
        self._idf: dict[str, float] = {}
        # Synonym structures
        self._synonyms: dict[str, list[str]] = {}       # skill → [syn, ...]
        self._synonym_index: dict[str, list[tuple[str, float]]] = {}  # token → [(skill, weight)]
        self._skill_paths: dict[str, Path] = {}          # skill_name → SKILL.md path
        # Embedding
        self._emb_matrix = None
        self._model = None
        self._jieba_initialized = False
        config = _retrieval_config()
        self._top_k = _retrieval_top_k(config)
        if not _retrieval_enabled(config):
            logger.info("Skill retrieval disabled by skills.retrieval.enabled")
            return
        # Start background initialization immediately
        threading.Thread(target=self._lazy_init, daemon=True).start()

    def is_ready(self) -> bool:
        return self._ready

    def is_loading(self) -> bool:
        return self._loading

    def error(self) -> str | None:
        return self._error

    def retrieve(self, query: str, top_k: int | None = None) -> list[str]:
        """Return top-k skill names matching the query.

        For detailed match info (including layer and content), use
        ``retrieve_detailed()`` instead.
        """
        result = self.retrieve_detailed(query, top_k)
        return result["skills"]

    def retrieve_detailed(self, query: str, top_k: int | None = None) -> dict:
        """Return detailed match info: skills, layer, and content.

        Returns:
            {
                "skills": ["skill-name", ...],
                "layer": "L1" | "L2-5" | "none",
                "skill_name": "skill-name"  # only for L1
            }
        """
        if not query or not query.strip():
            return {"skills": [], "layer": "none"}

        # ── Layer 1: Hard triggers (instant return, no ranking) ──
        hard_hit = self._hard_trigger(query)
        if hard_hit:
            logger.info("Skill retriever L1 hard trigger → %s", hard_hit)
            return {"skills": [hard_hit], "layer": "L1", "skill_name": hard_hit}

        # ── Layers 2-5: Full retrieval pipeline ──
        if not self._ready:
            if not self._loading:
                logger.info("Skill retriever: initializing on first call...")
                self._lazy_init()
            else:
                for _ in range(300):
                    if not self._loading:
                        break
                    time.sleep(0.1)
            if not self._ready:
                logger.warning("Skill retriever not ready after init attempt")
                return {"skills": [], "layer": "none"}

        k = top_k or self._top_k
        try:
            result = self._retrieve_inner(query, k)
            if result:
                # Layer 6: utility re-ranking — prefer skills that
                # historically WORKED for this task type over skills that
                # only matched textually. No-signal skills sort after
                # scored ones, so sparse telemetry never distorts results.
                try:
                    from tools.skill_usage import utility_rerank
                    result = utility_rerank(result)
                except Exception:
                    # Best-effort: telemetry failure never breaks retrieval
                    pass
                logger.info("Skill retriever: %d skills for [%s]", len(result), query[:50])
            return {"skills": result, "layer": "L2-5" if result else "none"}
        except Exception as e:
            logger.warning("Skill retrieval failed: %s", e)
            return {"skills": [], "layer": "none"}

    def get_skill_content(self, skill_name: str) -> str | None:
        """Read and return the full SKILL.md content for a skill."""
        # Try cached path first
        path = self._skill_paths.get(skill_name)
        if path and path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                pass

        # Fallback: search skills directories directly
        from hermes_constants import get_hermes_home

        for base_dir in [
            get_hermes_home() / "skills",
            get_hermes_home() / "hermes-agent" / "skills",
        ]:
            if not base_dir.exists():
                continue
            # Pattern 1: skills/<skill_name>/SKILL.md (flat structure)
            direct = base_dir / skill_name / "SKILL.md"
            if direct.exists():
                try:
                    return direct.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Pattern 2: skills/<category>/<skill_name>/SKILL.md (nested)
            for cat_dir in base_dir.iterdir():
                if not cat_dir.is_dir():
                    continue
                skill_md = cat_dir / skill_name / "SKILL.md"
                if skill_md.exists():
                    try:
                        return skill_md.read_text(encoding="utf-8")
                    except Exception:
                        pass
        return None

    @staticmethod
    def _hard_trigger(query: str) -> str | None:
        """Layer 1: 3-tier matching against hard trigger table.

        Tier 1: Exact substring match (fastest, 100% precise)
                 Collects ALL matches, picks longest trigger (most specific),
                 then earliest position in query (closest to intent).
        Tier 2: Subsequence match — trigger CJK chars appear in order in query
                 with max 3-char gap between consecutive matched chars.
        Tier 3: Regex fuzzy — trigger segments with optional gaps (0-3 chars)
        """
        q = query.strip()
        if not q:
            return None

        # Tier 1: Collect ALL exact substring matches, pick the best one
        tier1_matches: list[tuple[str, str, int]] = []  # (trigger, skill, position)
        for trigger, skill in _HARD_TRIGGERS:
            idx = q.find(trigger)
            if idx != -1:
                tier1_matches.append((trigger, skill, idx))

        if tier1_matches:
            # Longest trigger wins (most specific), ties broken by earliest position
            tier1_matches.sort(key=lambda x: (-len(x[0]), x[2]))
            return tier1_matches[0][1]

        # Tier 2+3: Only for triggers with 2+ CJK characters
        #           and NO ASCII letters (ASCII-containing triggers like
        #           "Python数据" are too ambiguous for fuzzy matching)
        cjk_pattern = re.compile('[\u4e00-\u9fff]')
        ascii_pattern = re.compile(r'[a-zA-Z]')
        for trigger, skill in _HARD_TRIGGERS:
            cjk_chars = cjk_pattern.findall(trigger)
            if len(cjk_chars) < 2:
                continue
            # Skip fuzzy matching for mixed CJK/ASCII triggers
            if ascii_pattern.search(trigger):
                continue

            # Tier 2: Subsequence — all CJK chars of trigger appear in order
            if _is_subsequence(cjk_chars, q, max_gap=3):
                logger.debug("L1 subsequence match: %r in %r → %s", trigger, q, skill)
                return skill

            # Tier 3: Regex fuzzy — insert .{0,3} between trigger segments
            segments = re.findall('[\u4e00-\u9fff]+', trigger)
            if len(segments) >= 2:
                pattern = r'.{0,3}'.join(re.escape(s) for s in segments)
                try:
                    if re.search(pattern, q):
                        logger.debug("L1 regex fuzzy match: %r ~ %r → %s", pattern, q, skill)
                        return skill
                except re.error:
                    pass

        return None

    # ── Initialization ──────────────────────────────────────────

    def _lazy_init(self) -> None:
        self._loading = True
        try:
            t0 = time.time()
            self._load_skills()
            self._load_synonyms()
            self._build_fts5_index()
            self._load_embedding_model()
            t1 = time.time()
            self._ready = True
            logger.info(
                "Skill retriever ready: %d skills, %d synonyms, %.1fs",
                len(self._skill_names),
                sum(len(v) for v in self._synonyms.values()),
                t1 - t0,
            )
        except Exception as e:
            self._error = str(e)
            logger.warning("Skill retriever init failed: %s", e)
        finally:
            self._loading = False

    def _load_skills(self) -> None:
        from hermes_constants import get_hermes_home
        skills_dir = get_hermes_home() / "skills"
        hermes_agent_skills = get_hermes_home() / "hermes-agent" / "skills"

        seen = set()
        for base_dir in [skills_dir, hermes_agent_skills]:
            if not base_dir.exists():
                continue
            for cat_dir in sorted(base_dir.iterdir()):
                if not cat_dir.is_dir():
                    continue
                # Mixed layout support: a dir may be a flat skill
                # (skills/<name>/SKILL.md) OR a category wrapper
                # (skills/<category>/<name>/SKILL.md).
                direct_md = cat_dir / "SKILL.md"
                if direct_md.exists():
                    self._add_skill(cat_dir.name, direct_md, seen)
                    continue
                for skill_dir in sorted(cat_dir.iterdir()):
                    if not skill_dir.is_dir():
                        continue
                    skill_md = skill_dir / "SKILL.md"
                    if not skill_md.exists():
                        continue
                    self._add_skill(skill_dir.name, skill_md, seen)

    def _add_skill(self, name: str, skill_md: Path, seen: set) -> None:
        if name in seen:
            return
        try:
            content = skill_md.read_text(encoding="utf-8")
            desc = self._extract_description(content)
            seen.add(name)
            self._skill_names.append(name)
            self._skill_descs.append(desc)
            self._skill_paths[name] = skill_md
        except Exception:
            pass

    @staticmethod
    def _extract_description(content: str) -> str:
        if not content.startswith("---"):
            return ""
        parts = content.split("---", 2)
        if len(parts) < 3:
            return ""
        for line in parts[1].split("\n"):
            if line.strip().startswith("description:"):
                return line.strip()[12:].strip().strip("\"'")
        return ""

    def _load_synonyms(self) -> None:
        """Load synonym dictionary — Layer 3 source data."""
        import jieba

        synonym_file = Path(__file__).parent / "skill_synonyms.yaml"
        if not synonym_file.exists():
            logger.debug("No synonym dictionary found at %s", synonym_file)
            return

        try:
            content = synonym_file.read_text(encoding="utf-8")
            current_skill = None
            for line in content.split("\n"):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("- "):
                    if current_skill:
                        synonym = stripped[2:].strip().strip('"').strip("'")
                        if synonym:
                            self._synonyms.setdefault(current_skill, []).append(synonym)
                elif ":" in stripped and not stripped.startswith("-"):
                    skill_name = stripped.split(":")[0].strip()
                    if skill_name and not skill_name.startswith("_"):
                        current_skill = skill_name
                    else:
                        current_skill = None

            # Build reverse index for Layer 3
            valid_names = set(self._skill_names)
            for skill_name, syn_list in self._synonyms.items():
                if skill_name not in valid_names:
                    continue
                for syn in syn_list:
                    # Index jieba tokens of the synonym
                    tokens = [t.strip() for t in jieba.cut(syn) if len(t.strip()) > 1]
                    for token in tokens:
                        self._synonym_index.setdefault(token, []).append(
                            (skill_name, 1.0)
                        )
                    # Also index the full synonym as a single unit
                    if len(syn) > 1:
                        self._synonym_index.setdefault(syn, []).append(
                            (skill_name, 1.5)
                        )

            logger.info(
                "Loaded %d synonym entries for %d skills, %d index tokens",
                sum(len(v) for v in self._synonyms.values()),
                len(self._synonyms),
                len(self._synonym_index),
            )
        except Exception as e:
            logger.warning("Failed to load synonym dictionary: %s", e)

    def _build_fts5_index(self) -> None:
        """Layer 2: Build clean FTS5 index (name + description only)."""
        import jieba

        if not self._jieba_initialized:
            jieba.initialize()
            self._jieba_initialized = True

        all_doc_freq: Counter = Counter()
        for i, desc in enumerate(self._skill_descs):
            name = self._skill_names[i]
            text = f"{name} {desc}"
            tokens = [
                t.strip() for t in jieba.cut(text)
                if len(t.strip()) > 1 and not t.strip().isdigit()
            ]
            self._doc_tokens.append(tokens)
            for token in set(tokens):
                all_doc_freq[token] += 1

        n = len(self._skill_names)
        self._idf = {
            t: math.log((n + 1) / (df + 1)) + 1
            for t, df in all_doc_freq.items()
        }

    def _load_embedding_model(self) -> None:
        """Layer 4: Load dense embedding model."""
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
        except ImportError:
            logger.warning("sentence-transformers not installed; FTS5+Syn only")
            self._emb_matrix = None
            return

        config = _retrieval_config()
        model_name = str(
            config.get(
                "embedding_model",
                "shibing624/text2vec-base-chinese-paraphrase",
            )
        ).strip() or "shibing624/text2vec-base-chinese-paraphrase"
        try:
            self._model = SentenceTransformer(model_name)
            texts = [f"{n}, {d}" for n, d in zip(self._skill_names, self._skill_descs)]
            self._emb_matrix = self._model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False
            )
            logger.info("Embedding model loaded: %s, shape: %s", model_name, self._emb_matrix.shape)
        except Exception as e:
            logger.warning("Embedding model load failed (%s); FTS5+Syn only", e)
            self._emb_matrix = None

    # ── Retrieval Pipeline ──────────────────────────────────────

    def _retrieve_inner(self, query: str, k: int) -> list[str]:
        """Layers 2-5: FTS5 + Synonym + Embedding → RRF fusion.

        Returns empty list when confidence is too low — meaning the query
        doesn't match any skill well, and the LLM should use general knowledge.
        """
        fts5_results = self._fts5_search(query, k * 3)
        syn_results = self._syn_search(query, k * 3)
        emb_results = self._emb_search(query, k * 3)

        fused = self._rrf_fusion(fts5_results, syn_results, emb_results)

        if not fused:
            return []

        top_score = fused[0][1]

        # Confidence check: top-1 must exceed floor (filter pure noise only)
        if top_score < _CONFIDENCE_THRESHOLD:
            logger.debug(
                "Skill retriever: top-1 score %.4f < floor %.4f, no signal",
                top_score, _CONFIDENCE_THRESHOLD,
            )
            return []

        # No gap check — when multiple skills score close, return all as hints
        # and let the LLM decide which (if any) to load.

        result = []
        for idx, score in fused[:k]:
            if score >= _MIN_RRF_SCORE:
                result.append(self._skill_names[idx])
        return result

    def _fts5_search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Layer 2: Clean BM25 search — name + description only, no synonym mixing."""
        import jieba

        query_tokens = [
            t.strip() for t in jieba.cut(query)
            if len(t.strip()) > 1
        ]
        scores: dict[int, float] = defaultdict(float)

        for qt in query_tokens:
            q_idf = self._idf.get(qt, 1.0)
            for i, doc_tok in enumerate(self._doc_tokens):
                tf = doc_tok.count(qt)
                if tf > 0:
                    scores[i] += q_idf * tf / (tf + 1.5)
                # Direct string match bonus in name+desc
                if qt in f"{self._skill_names[i]} {self._skill_descs[i]}":
                    scores[i] += q_idf * 0.5

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:k]

    def _syn_search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Layer 3: Synonym dictionary matching — independent scoring."""
        import jieba

        query_tokens = [
            t.strip() for t in jieba.cut(query)
            if len(t.strip()) > 1
        ]
        scores: dict[int, float] = defaultdict(float)

        # Token-level synonym matching
        for qt in query_tokens:
            if qt in self._synonym_index:
                q_idf = self._idf.get(qt, 1.0)
                for skill_name, weight in self._synonym_index[qt]:
                    try:
                        idx = self._skill_names.index(skill_name)
                        scores[idx] += weight * q_idf
                    except ValueError:
                        pass

        # Full-phrase synonym matching (multi-char synonyms in query)
        for syn_key, entries in self._synonym_index.items():
            if len(syn_key) > 2 and syn_key in query:
                for skill_name, weight in entries:
                    try:
                        idx = self._skill_names.index(skill_name)
                        scores[idx] += weight * 2.0
                    except ValueError:
                        pass

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:k]

    def _emb_search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Layer 4: Dense embedding cosine similarity."""
        if self._model is None or self._emb_matrix is None:
            return []

        import numpy as np

        query_emb = self._model.encode([query], normalize_embeddings=True)
        scores = np.dot(self._emb_matrix, query_emb.T).flatten()
        top_idx = np.argsort(-scores)[:k]
        return [(int(idx), float(scores[idx])) for idx in top_idx]

    @staticmethod
    def _rrf_fusion(
        fts5_results: list[tuple[int, float]],
        syn_results: list[tuple[int, float]],
        emb_results: list[tuple[int, float]],
        k: int = _RRF_K,
    ) -> list[tuple[int, float]]:
        """Layer 5: Reciprocal Rank Fusion of three ranked lists."""
        fused: dict[int, float] = {}

        for rank, (idx, _) in enumerate(fts5_results, start=1):
            fused[idx] = fused.get(idx, 0.0) + _RRF_W_FTS5 / (k + rank)

        for rank, (idx, _) in enumerate(syn_results, start=1):
            fused[idx] = fused.get(idx, 0.0) + _RRF_W_SYN / (k + rank)

        for rank, (idx, _) in enumerate(emb_results, start=1):
            fused[idx] = fused.get(idx, 0.0) + _RRF_W_EMB / (k + rank)

        return sorted(fused.items(), key=lambda x: -x[1])
