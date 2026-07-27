"""OptMem memory provider for Hermes.

A portable, append-only, decay-compressed memory backend that plugs into
Hermes via the MemoryProvider interface. On-disk format is byte-compatible
with https://github.com/VictorTaelin/OptMem, so logs are interchangable
with the original ``memo`` tool.

Activation (profile-scoped, set in config.yaml):
    memory:
      provider: optmem
    plugins:
      optmem:
        memory_dir: $HERMES_HOME/optmem_memory   # optional, default below

Only ONE external memory provider may be active at a time (Hermes enforces
this). The builtin short-term ``memory`` tool keeps running alongside.

Design notes
------------
- The agent records durable facts with ``optmem_note`` (one line, <=280 chars).
- When a pair of memories forms, the provider surfaces a pending compression
  via ``prefetch`` (and ``optmem_nap``), and the agent performs the "nap":
  it merges the block into one line. This is Taelin's "nap, don't sleep".
- ``optmem_recall`` does accent-normalized BM25 search across all history.
- Builtin ``memory`` writes are mirrored automatically (on_memory_write).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .engine import OptMemEngine

logger = logging.getLogger(__name__)


def _load_plugin_config() -> dict:
    from hermes_cli.config import cfg_get
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return cfg_get(config, "plugins", "optmem", default={}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

NOTE_SCHEMA = {
    "name": "optmem_note",
    "description": (
        "Record one durable memory line to the OptMem append-only log "
        "(family facts, decisions, events of lasting effect). One line, "
        "max 280 chars. If a compression is due, do it (optmem_nap) before "
        "your next action. Use for things worth remembering forever — not "
        "ephemeral chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory, one line (<=280 chars).",
            },
        },
        "required": ["text"],
    },
}

RECALL_SCHEMA = {
    "name": "optmem_recall",
    "description": (
        "Search the entire OptMem history with accent-normalized BM25 "
        "(e.g. 'caçula' matches 'cacula'). Returns ranked memory lines. "
        "Use when you need an old fact, decision, or event."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "topk": {"type": "integer", "description": "Max results (default 5)."},
            "regex": {
                "type": "boolean",
                "description": "Use literal regex instead of BM25 (default false).",
            },
        },
        "required": ["query"],
    },
}

NAP_SCHEMA = {
    "name": "optmem_nap",
    "description": (
        "Apply a compression the provider asked for. Call optmem_nap with "
        "the block id and a one-line summary (<=280 chars) that keeps what "
        "has lasting effect and drops the rest. Invent nothing. Mirrors "
        "Taelin's 'nap, don't sleep'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lo": {"type": "integer", "description": "Block start id (inclusive)."},
            "hi": {"type": "integer", "description": "Block end id (inclusive)."},
            "summary": {"type": "string", "description": "One-line compression."},
        },
        "required": ["lo", "hi", "summary"],
    },
}

WAKE_SCHEMA = {
    "name": "optmem_wake",
    "description": (
        "Print the current OptMem context (recent memories verbatim, old ones "
        "decayed into summaries). Run at session start or when you need the "
        "full picture."
    ),
    "parameters": {"type": "object", "properties": {}},
}


class OptMemProvider(MemoryProvider):
    """Hermes MemoryProvider backed by the OptMem append-only engine."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_plugin_config()
        self._engine: Optional[OptMemEngine] = None
        self._memory_dir: Optional[str] = None

    @property
    def name(self) -> str:
        return "optmem"

    def is_available(self) -> bool:
        # Pure-local, no credentials. Always available.
        return True

    def get_config_schema(self):
        from hermes_constants import display_hermes_home
        default_dir = f"{display_hermes_home()}/optmem_memory"
        return [
            {
                "key": "memory_dir",
                "description": "Directory for LOG.txt + TREE/ (default: $HERMES_HOME/optmem_memory)",
                "default": default_dir,
            },
        ]

    def save_config(self, values, hermes_home):
        from pathlib import Path
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml
            existing = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["optmem"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception as e:
            logger.warning("OptMem save_config failed: %s", e)

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
        mem_dir = self._config.get("memory_dir", f"{home}/optmem_memory")
        if isinstance(mem_dir, str):
            mem_dir = mem_dir.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
        self._memory_dir = mem_dir
        self._engine = OptMemEngine(mem_dir)
        self._session_id = session_id

    # -- context ------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._engine is None:
            return ""
        n = self._engine.log_len()
        if n == 0:
            return (
                "# OptMem (permanent memory)\n"
                "Active and empty. Use optmem_note to store durable facts about "
                "the family, decisions, and events of lasting effect. They are "
                "never deleted and decay (older ones compress) over time."
            )
        pending = len(self._engine.pending_naps())
        msg = (
            f"# OptMem (permanent memory)\n"
            f"Active. {n} memories stored (append-only, decay-compressed). "
            f"Use optmem_recall to search all history, optmem_note to add, "
            f"optmem_wake to see the full context."
        )
        if pending:
            msg += f"\n{pending} compression(s) pending — do them via optmem_nap when asked."
        return msg

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._engine is None:
            return ""
        try:
            lines = []
            # Always surface the decayed context. This lets OptMem fully replace
            # the builtin short-term memory when memory_enabled is False — the
            # agent still sees the permanent context every turn via prefetch
            # (dynamic context, does NOT break the cached system prompt).
            wake = self._engine.wake_lines()
            if wake:
                lines.append("## OptMem context (permanent, decay-compressed)\n" + "\n".join(wake))
            # Pending nap first so the agent acts before context grows.
            nap = self._engine.next_nap()
            if nap:
                (lo, hi), prompt = nap
                lines.append(
                    f"[OptMem] Compression due for #{lo}-{hi}. Run:\n"
                    f"optmem_nap(lo={lo}, hi={hi}, summary=\\\"<one line>\\\")\\n"
                    f"{prompt}"
                )
            if query:
                results = self._engine.recall(query, topk=5)
                if results:
                    body = "\n".join(
                        f"- [{score:.2f}] #{mid} {date} {text}"
                        for score, mid, date, text in results
                    )
                    lines.append("## OptMem recall\n" + body)
            return "\n\n".join(lines)
        except Exception as e:
            logger.debug("OptMem prefetch failed: %s", e)
            return ""

    # -- writes -------------------------------------------------------------

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
        # OptMem stores explicit facts via optmem_note, not auto-sync.
        pass

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        """Mirror builtin memory writes into the permanent OptMem log."""
        if action == "add" and self._engine is not None and content:
            try:
                self._engine.append(content)
            except Exception as e:
                logger.debug("OptMem mirror failed: %s", e)

    # -- tools --------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [NOTE_SCHEMA, RECALL_SCHEMA, NAP_SCHEMA, WAKE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "optmem_note":
            return self._handle_note(args)
        if tool_name == "optmem_recall":
            return self._handle_recall(args)
        if tool_name == "optmem_nap":
            return self._handle_nap(args)
        if tool_name == "optmem_wake":
            return self._handle_wake(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def _handle_note(self, args: dict) -> str:
        try:
            text = args["text"].strip()
            mid = self._engine.append(text)
            out = {"saved_as": f"#{mid}", "status": "added"}
            nap = self._engine.next_nap()
            if nap:
                (lo, hi), _ = nap
                out["nap_due"] = {"lo": lo, "hi": hi}
                out["note"] = "Run optmem_nap for this block before your next action."
            return _json(out)
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_recall(self, args: dict) -> str:
        try:
            query = args["query"]
            topk = int(args.get("topk", 5))
            regex = bool(args.get("regex", False))
            hits = self._engine.recall(query, topk=topk, regex=regex)
            if not hits:
                return _json({"results": [], "count": 0})
            results = [
                {"score": round(s, 2), "id": mid, "date": date, "text": text}
                for s, mid, date, text in hits
            ]
            return _json({"results": results, "count": len(results)})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_nap(self, args: dict) -> str:
        try:
            lo = int(args["lo"])
            hi = int(args["hi"])
            summary = args["summary"].strip()
            ok = self._engine.apply_nap(lo, hi, summary)
            if not ok:
                return tool_error(f"#{lo}-{hi} was settled or forgotten meanwhile.")
            return _json({"status": "compressed", "block": f"{lo}-{hi}"})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_wake(self, args: dict) -> str:
        try:
            lines = self._engine.wake_lines()
            if not lines:
                return _json({"context": [], "note": "OptMem empty."})
            return _json({"context": lines, "count": len(lines)})
        except Exception as exc:
            return tool_error(str(exc))

    def shutdown(self) -> None:
        self._engine = None


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the OptMem memory provider with Hermes."""
    config = _load_plugin_config()
    provider = OptMemProvider(config=config)
    ctx.register_memory_provider(provider)
