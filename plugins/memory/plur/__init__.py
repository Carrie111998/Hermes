"""PLUR memory plugin — bundled MemoryProvider interface.

Local-first persistent memory via the PLUR engram system. Stores knowledge
as typed engrams on disk (plain YAML), searches with BM25 + BGE embeddings
(Reciprocal Rank Fusion), and syncs across machines via git. Zero API calls,
zero cloud required.

Requires: ``plur-hermes>=0.18.1`` (pip) + ``plur`` CLI (npm install -g @plur-ai/cli).

Config via environment variables:
  PLUR_PATH          — path to engram store (default: ~/.plur)
  PLUR_INJECT_MODE   — "fast" (BM25-only, default) or "hybrid" (BM25+embeddings)
  PLUR_INJECTION_FEEDBACK — "true" (default) to send relevance feedback after each turn

Config via config.yaml:
  memory:
    provider: plur
    plur:
      plur_path: ""        # override PLUR_PATH
      plur_inject_mode: fast  # "fast" or "hybrid"

Tools exposed to the model: plur_learn, plur_recall, plur_inject, plur_list,
plur_forget, plur_feedback, plur_capture, plur_timeline, plur_status,
plur_sync, plur_ingest, plur_packs_list, plur_packs_install, plur_packs_export,
plur_promote, plur_stores_add, plur_stores_list, plur_similarity_search (18 total).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

try:
    from plur_hermes.memory_provider import PlurMemoryProvider as _PlurMemoryProvider
    _PLUR_HERMES_AVAILABLE = True
except ImportError:
    _PLUR_HERMES_AVAILABLE = False
    _PlurMemoryProvider = None  # type: ignore[assignment,misc]


class PlurProvider(MemoryProvider):
    """Bundled PLUR memory provider for Hermes.

    Thin wrapper around ``plur_hermes.memory_provider.PlurMemoryProvider``
    that satisfies the Hermes bundled-plugin discovery contract.  The full
    implementation lives in plur-hermes so it is shared between this bundled
    path and the pip entry-point path (``hermes_agent.memory_providers``).

    When ``plur-hermes`` is installed alongside this plugin, both paths are
    mutually compatible and may run in the same process.  The bundled path
    takes precedence (bundled > user > project > entry-point per the memory
    plugin discovery order), so users who install plur-hermes separately will
    land here when this provider is selected via config.
    """

    name = "plur"

    def __init__(self) -> None:
        if not _PLUR_HERMES_AVAILABLE:
            raise ImportError(
                "plur-hermes is not installed. "
                "Run: pip install 'plur-hermes>=0.18.1'"
            )
        self._impl: _PlurMemoryProvider = _PlurMemoryProvider()  # type: ignore[misc]

    # -- Delegation to PlurMemoryProvider ------------------------------------

    def is_available(self) -> bool:
        return self._impl.is_available()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._impl.initialize(session_id, **kwargs)

    def system_prompt_block(self) -> str:
        return self._impl.system_prompt_block()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._impl.prefetch(query, session_id=session_id)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._impl.sync_turn(
            user_content,
            assistant_content,
            session_id=session_id,
            messages=messages,
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._impl.on_session_end(messages)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._impl.get_tool_schemas()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return self._impl.handle_tool_call(tool_name, args, **kwargs)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return self._impl.get_config_schema()

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        self._impl.save_config(values, hermes_home)
