"""Doubao Vector memory provider.

A lightweight local vector-memory provider for Hermes Agent that uses the
Volces Ark OpenAI-compatible embeddings endpoint with doubao-embedding-vision.

The provider is intentionally local-first: only the text being embedded is sent
to Ark; vectors and metadata are stored under the active HERMES_HOME so profiles
remain isolated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
_DEFAULT_MODEL = "doubao-embedding-vision"
_DEFAULT_MAX_RESULTS = 8
_DEFAULT_MIN_SCORE = 0.18
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_TEXT_CHARS = 6000
_DEFAULT_MAX_ITEMS = 2000
_INDEX_VERSION = 1


def _default_config() -> dict:
    return {
        "base_url": _DEFAULT_BASE_URL,
        "model": _DEFAULT_MODEL,
        "max_results": _DEFAULT_MAX_RESULTS,
        "min_score": _DEFAULT_MIN_SCORE,
        "timeout": _DEFAULT_TIMEOUT,
        "max_text_chars": _DEFAULT_MAX_TEXT_CHARS,
        "max_items": _DEFAULT_MAX_ITEMS,
        "auto_capture": True,
        "auto_recall": True,
        "capture_assistant": False,
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


def _safe_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except Exception:
        return default


def _safe_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return default


def _load_config(hermes_home: str) -> dict:
    config = _default_config()
    path = Path(hermes_home) / "doubao_vector_memory.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({k: v for k, v in raw.items() if v is not None})
        except Exception:
            logger.debug("Failed to parse %s", path, exc_info=True)

    config["base_url"] = str(config.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    config["model"] = str(config.get("model") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    config["max_results"] = _safe_int(config.get("max_results"), _DEFAULT_MAX_RESULTS, 1, 20)
    config["min_score"] = _safe_float(config.get("min_score"), _DEFAULT_MIN_SCORE, -1.0, 1.0)
    config["timeout"] = _safe_float(config.get("timeout"), _DEFAULT_TIMEOUT, 1.0, 120.0)
    config["max_text_chars"] = _safe_int(config.get("max_text_chars"), _DEFAULT_MAX_TEXT_CHARS, 200, 30000)
    config["max_items"] = _safe_int(config.get("max_items"), _DEFAULT_MAX_ITEMS, 10, 20000)
    config["auto_capture"] = _as_bool(config.get("auto_capture"), True)
    config["auto_recall"] = _as_bool(config.get("auto_recall"), True)
    config["capture_assistant"] = _as_bool(config.get("capture_assistant"), False)
    return config


def _save_config(values: dict, hermes_home: str) -> None:
    path = Path(hermes_home) / "doubao_vector_memory.json"
    existing = _default_config()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing.update(raw)
        except Exception:
            pass
    existing.update(values or {})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _text_hash(text: str, target: str = "") -> str:
    h = hashlib.sha256()
    h.update(target.encode("utf-8"))
    h.update(b"\0")
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


def _clean_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Strip memory context fences from captured assistant/user text.
    text = text.replace("<memory-context>", "").replace("</memory-context>", "")
    text = text.replace("<doubao-vector-memory>", "").replace("</doubao-vector-memory>", "")
    return text[:limit]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class _DoubaoEmbeddingClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return f"{self.base_url}/embeddings"

    def embed(self, text: str) -> List[float]:
        payload = {"model": self.model, "input": text}
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        emb = (((data or {}).get("data") or [{}])[0] or {}).get("embedding") or []
        if not isinstance(emb, list) or not emb:
            raise RuntimeError("empty embedding returned")
        return [float(x) for x in emb]


class _LocalVectorStore:
    def __init__(self, path: Path, max_items: int):
        self.path = path
        self.max_items = max_items
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = []
        self._loaded = False

    def load(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self._items = []
                self._loaded = True
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                items = raw.get("items", []) if isinstance(raw, dict) else []
                self._items = [i for i in items if isinstance(i, dict) and isinstance(i.get("embedding"), list)]
                self._loaded = True
            except Exception:
                logger.warning("Failed to load Doubao vector index %s", self.path, exc_info=True)
                self._items = []
                self._loaded = True

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": _INDEX_VERSION, "updated_at": time.time(), "items": self._items[-self.max_items:]}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self.path.chmod(0o600)
            except Exception:
                pass

    def upsert(self, item: Dict[str, Any]) -> None:
        with self._lock:
            if not self._loaded:
                self.load()
            item_id = item.get("id")
            self._items = [i for i in self._items if i.get("id") != item_id]
            self._items.append(item)
            if len(self._items) > self.max_items:
                self._items = self._items[-self.max_items:]
            self.save()

    def remove_by_text(self, text_or_id: str) -> int:
        needle = (text_or_id or "").strip()
        if not needle:
            return 0
        with self._lock:
            if not self._loaded:
                self.load()
            before = len(self._items)
            self._items = [
                i for i in self._items
                if needle not in str(i.get("text", "")) and needle != str(i.get("id", ""))
            ]
            removed = before - len(self._items)
            if removed:
                self.save()
            return removed

    def search(self, query_embedding: List[float], limit: int, min_score: float) -> List[Dict[str, Any]]:
        with self._lock:
            if not self._loaded:
                self.load()
            scored = []
            for item in self._items:
                score = _cosine(query_embedding, item.get("embedding") or [])
                if score >= min_score:
                    copy = dict(item)
                    copy["score"] = score
                    scored.append(copy)
            scored.sort(key=lambda x: (x.get("score", -1.0), x.get("created_at", 0.0)), reverse=True)
            return scored[:limit]

    @property
    def count(self) -> int:
        with self._lock:
            if not self._loaded:
                self.load()
            return len(self._items)


STORE_SCHEMA = {
    "name": "doubao_vector_store",
    "description": "Store explicit text into the local Doubao embedding vector memory.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Text to store."},
            "target": {"type": "string", "description": "Optional category, e.g. memory/user/project."},
            "metadata": {"type": "object", "description": "Optional metadata."},
        },
        "required": ["content"],
    },
}

SEARCH_SCHEMA = {
    "name": "doubao_vector_search",
    "description": "Search local Doubao embedding vector memory by semantic similarity.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Maximum result count, 1-20."},
        },
        "required": ["query"],
    },
}

STATS_SCHEMA = {
    "name": "doubao_vector_stats",
    "description": "Show Doubao vector memory status and index size.",
    "parameters": {"type": "object", "properties": {}},
}


class DoubaoVectorMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._config = _default_config()
        self._api_key = ""
        self._client: Optional[_DoubaoEmbeddingClient] = None
        self._store: Optional[_LocalVectorStore] = None
        self._active = False
        self._write_enabled = True
        self._hermes_home = ""
        self._session_id = ""
        self._index_path: Optional[Path] = None

    @property
    def name(self) -> str:
        return "doubao_vector"

    def is_available(self) -> bool:
        return bool(self._resolve_api_key())

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "Volces Ark API key for doubao-embedding-vision. Leave empty to reuse providers.custom.api_key.",
                "secret": True,
                "required": False,
                "env_var": "DOUBAO_EMBEDDING_API_KEY",
            },
            {"key": "base_url", "description": "OpenAI-compatible embedding base URL", "default": _DEFAULT_BASE_URL},
            {"key": "model", "description": "Embedding model", "default": _DEFAULT_MODEL},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        clean = {k: v for k, v in (values or {}).items() if k != "api_key" and v is not None}
        _save_config(clean, hermes_home)

    def _resolve_api_key(self) -> str:
        env_key = os.environ.get("DOUBAO_EMBEDDING_API_KEY", "").strip()
        if env_key:
            return env_key
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            providers = cfg.get("providers", {}) or {}
            custom = providers.get("custom", {}) or {}
            key = str(custom.get("api_key") or "").strip()
            if key:
                return key
            model_key = str((cfg.get("model", {}) or {}).get("api_key") or "").strip()
            if model_key:
                return model_key
        except Exception:
            pass
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "")
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        if not self._hermes_home:
            from hermes_constants import get_hermes_home
            self._hermes_home = str(get_hermes_home())
        self._config = _load_config(self._hermes_home)
        self._api_key = self._resolve_api_key()
        agent_context = kwargs.get("agent_context", "")
        self._write_enabled = agent_context not in {"cron", "flush", "subagent"}
        self._index_path = Path(self._hermes_home) / "doubao_vector_memory" / "index.json"
        self._store = _LocalVectorStore(self._index_path, int(self._config["max_items"]))
        self._store.load()
        self._active = bool(self._api_key)
        if self._active:
            self._client = _DoubaoEmbeddingClient(
                api_key=self._api_key,
                base_url=str(self._config["base_url"]),
                model=str(self._config["model"]),
                timeout=float(self._config["timeout"]),
            )
        else:
            self._client = None

    def system_prompt_block(self) -> str:
        if not self._active or not self._store:
            return ""
        return (
            "# Doubao Vector Memory\n"
            f"Active semantic memory provider using {self._config['model']}. "
            f"Local indexed items: {self._store.count}. "
            "Relevant recalled memories may appear in <doubao-vector-memory>; use silently when relevant."
        )

    def _embed(self, text: str) -> List[float]:
        if not self._client:
            raise RuntimeError("Doubao vector memory is not configured")
        return self._client.embed(text)

    def _store_text(self, text: str, *, target: str = "memory", metadata: Optional[dict] = None) -> dict:
        if not self._active or not self._client or not self._store:
            return {"saved": False, "error": "Doubao vector memory is not configured"}
        clean = _clean_text(text, int(self._config["max_text_chars"]))
        if not clean:
            return {"saved": False, "error": "empty content"}
        embedding = self._embed(clean)
        item_id = _text_hash(clean, target)
        item = {
            "id": item_id,
            "target": target or "memory",
            "text": clean,
            "embedding": embedding,
            "metadata": metadata or {},
            "session_id": self._session_id,
            "created_at": time.time(),
            "model": self._config["model"],
        }
        self._store.upsert(item)
        return {"saved": True, "id": item_id, "dim": len(embedding), "preview": clean[:120]}

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or not self._config.get("auto_recall") or not self._store or not query.strip():
            return ""
        try:
            q = _clean_text(query, 1200)
            q_emb = self._embed(q)
            results = self._store.search(
                q_emb,
                limit=int(self._config["max_results"]),
                min_score=float(self._config["min_score"]),
            )
            if not results:
                return ""
            lines = []
            for item in results:
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                score = round(float(item.get("score", 0.0)) * 100)
                target = item.get("target", "memory")
                lines.append(f"- [{score}%][{target}] {text[:500]}")
            if not lines:
                return ""
            return "<doubao-vector-memory>\n## Doubao Semantic Recall\n" + "\n".join(lines) + "\n</doubao-vector-memory>"
        except Exception:
            logger.debug("Doubao vector prefetch failed", exc_info=True)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages=None) -> None:
        if not self._write_enabled or not self._config.get("auto_capture"):
            return
        clean_user = _clean_text(user_content, int(self._config["max_text_chars"]))
        if clean_user:
            try:
                self._store_text(
                    clean_user,
                    target="turn:user",
                    metadata={"type": "turn", "role": "user", "session_id": session_id or self._session_id},
                )
            except Exception:
                logger.debug("Doubao vector sync_turn user capture failed", exc_info=True)
        if self._config.get("capture_assistant"):
            clean_assistant = _clean_text(assistant_content, int(self._config["max_text_chars"]))
            if clean_assistant:
                try:
                    self._store_text(
                        clean_assistant,
                        target="turn:assistant",
                        metadata={"type": "turn", "role": "assistant", "session_id": session_id or self._session_id},
                    )
                except Exception:
                    logger.debug("Doubao vector sync_turn assistant capture failed", exc_info=True)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self._write_enabled:
            return
        if action == "remove" and self._store:
            self._store.remove_by_text(content or (metadata or {}).get("old_text", ""))
            return
        if action not in {"add", "replace"}:
            return
        try:
            self._store_text(
                content,
                target=f"explicit:{target or 'memory'}",
                metadata={"type": "explicit_memory", "action": action, **(metadata or {})},
            )
        except Exception:
            logger.debug("Doubao vector on_memory_write failed", exc_info=True)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # sync_turn already captures user turns. Keep this hook lightweight.
        return

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs) -> None:
        self._session_id = str(new_session_id or self._session_id)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [STORE_SCHEMA, SEARCH_SCHEMA, STATS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "doubao_vector_store":
            content = str(args.get("content") or "").strip()
            if not content:
                return tool_error("content is required")
            metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
            target = str(args.get("target") or "manual")
            try:
                return json.dumps(self._store_text(content, target=target, metadata=metadata), ensure_ascii=False)
            except Exception as exc:
                return tool_error(f"Doubao vector store failed: {exc}")
        if tool_name == "doubao_vector_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_error("query is required")
            try:
                limit = _safe_int(args.get("limit"), int(self._config["max_results"]), 1, 20)
                q_emb = self._embed(_clean_text(query, 1200))
                results = self._store.search(q_emb, limit=limit, min_score=-1.0) if self._store else []
                payload = [
                    {
                        "id": item.get("id", ""),
                        "target": item.get("target", ""),
                        "score": round(float(item.get("score", 0.0)), 4),
                        "content": str(item.get("text", ""))[:800],
                    }
                    for item in results
                ]
                return json.dumps({"results": payload, "count": len(payload)}, ensure_ascii=False)
            except Exception as exc:
                return tool_error(f"Doubao vector search failed: {exc}")
        if tool_name == "doubao_vector_stats":
            return json.dumps({
                "active": self._active,
                "model": self._config.get("model"),
                "base_url": self._config.get("base_url"),
                "index_path": str(self._index_path or ""),
                "count": self._store.count if self._store else 0,
            }, ensure_ascii=False)
        return tool_error(f"Unknown tool: {tool_name}")

    def backup_paths(self) -> List[str]:
        if self._index_path:
            return [str(self._index_path.parent)]
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(DoubaoVectorMemoryProvider())
