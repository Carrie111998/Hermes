"""TiMEM memory plugin using the MemoryProvider interface.

TiMEM (https://docs.timem.cloud) is a temporal-hierarchical memory engine.
Conversations are ingested server-side into a five-level Temporal Memory
Tree (L1 raw interactions -> L5 persona), and recalled through semantic
search over the layered store.

This provider wires Hermes turns into TiMEM:
  - sync_turn()       -> POST the completed turn for async server-side
                         memory generation (L1-L5 extraction).
  - prefetch()        -> semantic search over layered memories, injected
                         as background context before each turn.
  - on_memory_write() -> mirror built-in memory-tool writes as L1 facts.
  - tools             -> timem_search / timem_add / timem_profile.

Auth is a single API key (TIMEM_API_KEY) from https://console.timem.cloud.
The SDK (timem-ai) is lazy-installed on first use via tools/lazy_deps.py.
"""

from __future__ import annotations

import json
import inspect
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.timem.cloud"
_DEFAULT_DOMAIN = "hermes"
_DEFAULT_CHARACTER_ID = "hermes"
_DEFAULT_USER_ID = "hermes-user"
_DEFAULT_MAX_RECALL_RESULTS = 8
_DEFAULT_SCORE_THRESHOLD = 0.5
_DEFAULT_API_TIMEOUT = 60.0
_PREFETCH_WAIT_SECS = 10.0
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120.0
_MIN_CAPTURE_LENGTH = 10
_API_KEY_URL = "https://console.timem.cloud/"

_TRIVIAL_RE = re.compile(
    r"^(ok|okay|thanks|thank you|got it|sure|yes|no|yep|nope|k|ty|thx|np)\.?$",
    re.IGNORECASE,
)
_CONTEXT_STRIP_RE = re.compile(r"<timem-context>[\s\S]*?</timem-context>\s*", re.DOTALL)


# ─── Config ──────────────────────────────────────────────────────────────────

def _default_config() -> dict:
    return {
        "user_id": "",
        "character_id": "",
        "domain": _DEFAULT_DOMAIN,
        "base_url": "",
        "max_recall_results": _DEFAULT_MAX_RECALL_RESULTS,
        "score_threshold": _DEFAULT_SCORE_THRESHOLD,
        "auto_recall": True,
        "auto_capture": True,
        "api_timeout": _DEFAULT_API_TIMEOUT,
    }


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return default


def _load_timem_config(hermes_home: str) -> dict:
    config = _default_config()
    config_path = Path(hermes_home) / "timem.json"
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({k: v for k, v in raw.items() if v is not None})
        except Exception:
            logger.debug("Failed to parse %s", config_path, exc_info=True)

    config["user_id"] = str(config.get("user_id", "") or "").strip()
    config["character_id"] = str(config.get("character_id", "") or "").strip()
    domain = str(config.get("domain", _DEFAULT_DOMAIN) or "").strip()
    config["domain"] = domain or _DEFAULT_DOMAIN
    config["base_url"] = str(config.get("base_url", "") or "").strip()
    try:
        config["max_recall_results"] = max(1, min(20, int(config.get("max_recall_results", _DEFAULT_MAX_RECALL_RESULTS))))
    except Exception:
        config["max_recall_results"] = _DEFAULT_MAX_RECALL_RESULTS
    try:
        config["score_threshold"] = max(0.0, min(1.0, float(config.get("score_threshold", _DEFAULT_SCORE_THRESHOLD))))
    except Exception:
        config["score_threshold"] = _DEFAULT_SCORE_THRESHOLD
    config["auto_recall"] = _as_bool(config.get("auto_recall"), True)
    config["auto_capture"] = _as_bool(config.get("auto_capture"), True)
    try:
        config["api_timeout"] = max(1.0, min(60.0, float(config.get("api_timeout", _DEFAULT_API_TIMEOUT))))
    except Exception:
        config["api_timeout"] = _DEFAULT_API_TIMEOUT
    return config


def _save_timem_config(values: dict, hermes_home: str) -> None:
    config_path = Path(hermes_home) / "timem.json"
    existing = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except Exception:
            existing = {}
    existing.update(values)
    from utils import atomic_json_write
    atomic_json_write(config_path, existing, mode=0o600, sort_keys=True)


def _resolve_base_url(config_value: Any = "") -> str:
    raw = (
        str(config_value or "").strip()
        or os.environ.get("TIMEM_BASE_URL", "").strip()
    )
    return (raw or _DEFAULT_BASE_URL).rstrip("/") or _DEFAULT_BASE_URL


# ─── Result normalization ────────────────────────────────────────────────────

def _memory_text(item: Any) -> str:
    """Extract display text from one memory item (shape varies by layer/version)."""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return str(item).strip()
    for key in ("content", "memory", "text", "summary"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner in ("text", "content", "summary", "description"):
                inner_val = value.get(inner)
                if isinstance(inner_val, str) and inner_val.strip():
                    return inner_val.strip()
            try:
                return json.dumps(value, ensure_ascii=False)[:500]
            except Exception:
                return str(value)[:500]
    return ""


def _extract_memories(response: Any) -> List[dict]:
    """Normalize a search response into a list of memory dicts."""
    if not response:
        return []
    if isinstance(response, list):
        raw = response
    elif isinstance(response, dict):
        raw = None
        for key in ("memories", "results", "items", "data"):
            value = response.get(key)
            if isinstance(value, list):
                raw = value
                break
            if isinstance(value, dict):
                for inner in ("memories", "results", "items"):
                    inner_val = value.get(inner)
                    if isinstance(inner_val, list):
                        raw = inner_val
                        break
                if raw is not None:
                    break
        if raw is None:
            return []
    else:
        return []

    out = []
    for item in raw:
        text = _memory_text(item)
        if not text:
            continue
        entry = {"text": text}
        if isinstance(item, dict):
            entry["id"] = str(item.get("id") or item.get("memory_id") or "")
            layer = item.get("layer") or item.get("layer_type") or ""
            if layer:
                entry["layer"] = str(layer)
            for score_key in ("score", "retrieval_score", "similarity"):
                if item.get(score_key) is not None:
                    try:
                        entry["score"] = float(item[score_key])
                    except Exception:
                        pass
                    break
        out.append(entry)
    return out


def _format_prefetch_context(memories: List[dict], max_results: int) -> str:
    seen: set = set()
    lines = []
    for item in memories:
        text = item.get("text", "")
        if not text or text in seen:
            continue
        seen.add(text)
        bits = []
        if item.get("layer"):
            bits.append(f"[{item['layer']}]")
        if item.get("score") is not None:
            try:
                bits.append(f"[{round(float(item['score']) * 100)}%]")
            except Exception:
                pass
        prefix = " ".join(bits)
        lines.append(f"- {prefix} {text}".strip() if prefix else f"- {text}")
        if len(lines) >= max_results:
            break
    if not lines:
        return ""
    intro = (
        "The following is background context recalled from TiMEM long-term "
        "memory. Use it silently when relevant. Do not force memories into "
        "the conversation."
    )
    body = "\n".join(lines)
    return f"<timem-context>\n{intro}\n\n## Relevant Memories\n{body}\n</timem-context>"


def _clean_text_for_capture(text: str) -> str:
    return _CONTEXT_STRIP_RE.sub("", text or "").strip()


def _is_trivial_message(text: str) -> bool:
    return bool(_TRIVIAL_RE.match((text or "").strip()))


# ─── SDK wrapper ─────────────────────────────────────────────────────────────

class _TimemClient:
    """Thin, thread-safe wrapper around ONE timem-ai sync client.

    The timem-ai synchronous client wraps an internal asyncio event loop
    that is NOT thread-safe: concurrent calls from different threads race on
    the same loop and time out.  We therefore (a) give every ``_TimemClient``
    its own ``TiMEMClient`` instance (own loop) and (b) serialize calls on
    this client with a lock.  The provider uses two ``_TimemClient``
    instances -- one for reads, one for writes -- so a slow write never
    blocks a recall.
    """

    def __init__(self, api_key: str, base_url: str, timeout: float,
                 user_id: str, character_id: str, domain: str):
        # Lazy-install the timem-ai SDK on demand. ensure() honors
        # security.allow_lazy_installs and redirects to the durable target on
        # sealed Docker venvs. On failure we fall through so the raw import
        # below produces the canonical ImportError message.
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.timem", prompt=False)
        except Exception:
            pass
        from timem import TiMEMClient
        # The timem-ai SDK logs verbose Chinese retry/timeout noise to the
        # console ("请求最终失败 ...") at ERROR level. We own the circuit
        # breaking and best-effort semantics and log failures ourselves, so
        # silence the SDK entirely (CRITICAL suppresses even its .error()).
        logging.getLogger("timem").setLevel(logging.CRITICAL)

        self._user_id = user_id
        self._character_id = character_id
        self._domain = domain
        self._lock = threading.Lock()
        self._client = TiMEMClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            verify_ssl=True,
            enable_monitoring=False,
            enable_circuit_breaker=False,
        )

    def search(self, query: str, *, limit: int, score_threshold: float) -> List[dict]:
        with self._lock:
            response = self._client.search_memories(
                user_id=self._user_id,
                query_text=query,
                character_id=self._character_id,
                score_threshold=score_threshold,
                limit=limit,
            )
        return _extract_memories(response)

    def ingest_turn(self, session_id: str, messages: List[dict],
                    metadata: Optional[dict] = None) -> dict:
        # Async on the server: returns an acceptance with task_id. We do NOT
        # poll -- memory generation completes in the background server-side.
        with self._lock:
            return self._client.generate_memory(
                character_id=self._character_id,
                session_id=session_id,
                messages=messages,
                user_id=self._user_id,
                domain=self._domain,
                metadata=metadata,
            )

    def add_fact(self, content: dict, *, tags: Optional[List[str]] = None,
                 session_id: Optional[str] = None) -> dict:
        with self._lock:
            return self._client.add_memory(
                user_id=self._user_id,
                domain=self._domain,
                content=content,
                layer_type="L1",
                tags=tags,
                session_id=session_id,
            )

    def get_profile(self) -> dict:
        with self._lock:
            result = self._client.get_profile(
                user_id=self._user_id, expert_id=self._character_id,
            )
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        try:
            cl = getattr(self._client, "close", None)
            # The timem-ai sync client exposes an async close(); calling it
            # without awaiting emits a RuntimeWarning and leaks the coroutine.
            if cl is not None and not inspect.iscoroutinefunction(cl):
                cl()
        except Exception:
            pass


# ─── Tool schemas ────────────────────────────────────────────────────────────

SEARCH_SCHEMA = {
    "name": "timem_search",
    "description": (
        "Semantic search over TiMEM layered long-term memory (L1 raw "
        "interactions up to L5 persona). Use when you need context about "
        "the user or past sessions that is not already in the conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Max results (1-20, default 8)."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "timem_add",
    "description": (
        "Store an explicit fact in TiMEM long-term memory. Use for lasting "
        "user facts, preferences, and decisions worth recalling later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for later filtering.",
            },
        },
        "required": ["content"],
    },
}

PROFILE_SCHEMA = {
    "name": "timem_profile",
    "description": (
        "Fetch the user profile TiMEM computed from L5 persona memories. "
        "Use to ground personalization decisions."
    ),
    "parameters": {"type": "object", "properties": {}},
}


# ─── Provider ────────────────────────────────────────────────────────────────

class TimemMemoryProvider(MemoryProvider):
    """TiMEM temporal-hierarchical memory provider."""

    def __init__(self):
        self._read_client: Optional[_TimemClient] = None
        self._write_client: Optional[_TimemClient] = None
        self._config: dict = _default_config()
        self._session_id = ""
        self._read_only = False
        self._init_error = ""
        self._lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_result = ""
        self._sync_thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._breaker_opened_at = 0.0

    @property
    def name(self) -> str:
        return "timem"

    # -- Availability / lifecycle --------------------------------------------

    def is_available(self) -> bool:
        # Config-presence only — do NOT gate on the timem SDK being
        # importable. The SDK is lazy-installed at client construction
        # (_TimemClient.__init__ -> tools.lazy_deps.ensure). Mirrors
        # honcho/mem0/supermemory.
        return bool(os.environ.get("TIMEM_API_KEY", "").strip())

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        hermes_home = kwargs.get("hermes_home") or os.path.expanduser("~/.hermes")
        self._config = _load_timem_config(hermes_home)

        # Cron/flush system prompts would pollute the user's memory tree.
        agent_context = str(kwargs.get("agent_context", "primary") or "primary")
        self._read_only = agent_context not in ("primary", "")

        user_id = (
            self._config["user_id"]
            or str(kwargs.get("user_id", "") or "").strip()
            or _DEFAULT_USER_ID
        )
        character_id = (
            self._config["character_id"]
            or str(kwargs.get("agent_identity", "") or "").strip()
            or _DEFAULT_CHARACTER_ID
        )

        api_key = os.environ.get("TIMEM_API_KEY", "").strip()
        try:
            self._read_client = _TimemClient(
                api_key=api_key,
                base_url=_resolve_base_url(self._config["base_url"]),
                timeout=self._config["api_timeout"],
                user_id=user_id,
                character_id=character_id,
                domain=self._config["domain"],
            )
            # Writes use a SEPARATE client/event loop so a slow server-side
            # generation never blocks recalls on the read client.
            self._write_client = _TimemClient(
                api_key=api_key,
                base_url=_resolve_base_url(self._config["base_url"]),
                timeout=self._config["api_timeout"],
                user_id=user_id,
                character_id=character_id,
                domain=self._config["domain"],
            )
            self._init_error = ""
        except Exception as exc:
            self._read_client = None
            self._write_client = None
            self._init_error = str(exc).strip()[:200] or "client construction failed"
            logger.warning("TiMEM client init failed: %s", self._init_error)

    def system_prompt_block(self) -> str:
        if self._read_client is None:
            return ""
        return (
            "## TiMEM Long-Term Memory\n"
            "TiMEM stores layered memories (L1 raw interactions -> L5 persona) "
            "across sessions. Relevant recall is injected automatically each "
            "turn. Use timem_search for explicit lookups, timem_add to store "
            "lasting facts, and timem_profile for the computed user profile."
        )

    # -- Circuit breaker -------------------------------------------------------

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() - self._breaker_opened_at > _BREAKER_COOLDOWN_SECS:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures == _BREAKER_THRESHOLD:
            self._breaker_opened_at = time.monotonic()
            logger.warning(
                "TiMEM circuit breaker opened after %d consecutive failures",
                _BREAKER_THRESHOLD,
            )

    # -- Recall (prefetch) -----------------------------------------------------

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._read_client is None or not self._config["auto_recall"]:
            return
        if self._is_breaker_open() or not (query or "").strip():
            return
        cleaned = _clean_text_for_capture(query)[:2000]
        if not cleaned:
            return

        def _recall():
            try:
                memories = self._read_client.search(
                    cleaned,
                    limit=self._config["max_recall_results"],
                    score_threshold=self._config["score_threshold"],
                )
                with self._lock:
                    self._prefetch_result = _format_prefetch_context(
                        memories, self._config["max_recall_results"]
                    )
                self._record_success()
            except Exception:
                logger.debug("TiMEM prefetch failed", exc_info=True)
                self._record_failure()
                with self._lock:
                    self._prefetch_result = ""

        with self._lock:
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                return
            self._prefetch_thread = threading.Thread(
                target=_recall, daemon=True, name="timem-prefetch"
            )
            self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._read_client is None or not self._config["auto_recall"]:
            return ""
        thread = self._prefetch_thread
        if thread is None:
            # No queued recall yet (first turn) — kick one off and wait briefly.
            self.queue_prefetch(query, session_id=session_id)
            thread = self._prefetch_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        with self._lock:
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_thread = None
        return result

    # -- Capture (sync_turn / mirror) -------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if self._write_client is None or self._read_only or not self._config["auto_capture"]:
            return
        if self._is_breaker_open():
            return
        user_text = _clean_text_for_capture(user_content)
        assistant_text = _clean_text_for_capture(assistant_content)
        if len(user_text) < _MIN_CAPTURE_LENGTH or _is_trivial_message(user_text):
            return
        payload = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
        sid = session_id or self._session_id

        def _sync():
            try:
                self._write_client.ingest_turn(sid, payload, metadata={"source": "hermes"})
                self._record_success()
            except Exception:
                logger.debug("TiMEM sync_turn failed", exc_info=True)
                self._record_failure()

        with self._lock:
            if self._sync_thread and self._sync_thread.is_alive():
                # Don't stack threads; drop this turn rather than block.
                return
            self._sync_thread = threading.Thread(
                target=_sync, daemon=True, name="timem-sync"
            )
            self._sync_thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._write_client is None or self._read_only or not self._config["auto_capture"]:
            return
        if action == "remove" or self._is_breaker_open():
            return
        text = (content or "").strip()
        if len(text) < _MIN_CAPTURE_LENGTH:
            return

        def _mirror():
            try:
                self._write_client.add_fact(
                    {
                        "type": "hermes_builtin_memory",
                        "action": action,
                        "target": target,
                        "text": text,
                    },
                    tags=["hermes", "builtin-memory"],
                    session_id=self._session_id or None,
                )
                self._record_success()
            except Exception:
                logger.debug("TiMEM on_memory_write mirror failed", exc_info=True)
                self._record_failure()

        threading.Thread(target=_mirror, daemon=True, name="timem-mirror").start()

    # -- Tools -------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA, PROFILE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._read_client is None and self._write_client is None:
            err = self._init_error or "not initialized"
            return json.dumps({"error": f"TiMEM client not initialized: {err}"})
        if self._is_breaker_open():
            return json.dumps({
                "error": "TiMEM temporarily unavailable (multiple consecutive "
                         "failures). Will retry automatically."
            })

        if tool_name == "timem_search":
            query = (args.get("query") or "").strip()
            if not query:
                return tool_error("Missing required parameter: query")
            if self._read_client is None:
                return tool_error("TiMEM read client not initialized")
            try:
                limit = max(1, min(int(args.get("limit", self._config["max_recall_results"])), 20))
            except Exception:
                limit = self._config["max_recall_results"]
            try:
                memories = self._read_client.search(
                    query, limit=limit,
                    score_threshold=self._config["score_threshold"],
                )
                self._record_success()
                if not memories:
                    return json.dumps({"result": "No relevant memories found."})
                return json.dumps(
                    {"results": memories, "count": len(memories)},
                    ensure_ascii=False,
                )
            except Exception as e:
                self._record_failure()
                return tool_error(f"TiMEM search failed: {str(e)[:200]}")

        elif tool_name == "timem_add":
            content = (args.get("content") or "").strip()
            if not content:
                return tool_error("Missing required parameter: content")
            if self._write_client is None:
                return tool_error("TiMEM write client not initialized")
            tags = args.get("tags")
            if not isinstance(tags, list):
                tags = None
            else:
                tags = [str(t) for t in tags if t][:10]
            # Fired into the background: TiMEM generates memory server-side
            # (async, can be slow), so we never block the turn on it.
            sid = self._session_id or None

            def _add():
                try:
                    self._write_client.add_fact(
                        {"type": "explicit_fact", "text": content},
                        tags=(tags or []) + ["hermes"],
                        session_id=sid,
                    )
                    self._record_success()
                except Exception:
                    logger.debug("TiMEM add_fact failed", exc_info=True)
                    self._record_failure()

            threading.Thread(target=_add, daemon=True, name="timem-add").start()
            return json.dumps({
                "result": "Fact submitted to TiMEM for async generation.",
                "queued": True,
            })

        elif tool_name == "timem_profile":
            if self._read_client is None:
                return tool_error("TiMEM read client not initialized")
            try:
                profile = self._read_client.get_profile()
                self._record_success()
                if not profile:
                    return json.dumps({"result": "No profile computed yet."})
                return json.dumps({"profile": profile}, ensure_ascii=False)
            except Exception as e:
                self._record_failure()
                return tool_error(f"TiMEM profile fetch failed: {str(e)[:200]}")

        return tool_error(f"Unknown tool: {tool_name}")

    # -- Setup / config -----------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "TiMEM API key (from the TiMEM console)",
                "secret": True,
                "required": True,
                "env_var": "TIMEM_API_KEY",
                "url": _API_KEY_URL,
            },
            {
                "key": "base_url",
                "description": "TiMEM Engine URL (leave default for TiMEM Cloud)",
                "default": _DEFAULT_BASE_URL,
            },
            {
                "key": "user_id",
                "description": "Stable user identifier for memory scoping",
                "default": _DEFAULT_USER_ID,
            },
            {
                "key": "character_id",
                "description": "Agent/character identifier (memory namespace per agent)",
                "default": _DEFAULT_CHARACTER_ID,
            },
            {
                "key": "domain",
                "description": "Business domain tag for memories",
                "default": _DEFAULT_DOMAIN,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        allowed = {"base_url", "user_id", "character_id", "domain",
                   "max_recall_results", "score_threshold",
                   "auto_recall", "auto_capture", "api_timeout"}
        filtered = {k: v for k, v in (values or {}).items() if k in allowed}
        if filtered:
            _save_timem_config(filtered, hermes_home)

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        for client in (self._read_client, self._write_client):
            if client:
                client.close()
        self._read_client = None
        self._write_client = None


def register(ctx) -> None:
    """Register TiMEM as a memory provider plugin."""
    ctx.register_memory_provider(TimemMemoryProvider())
