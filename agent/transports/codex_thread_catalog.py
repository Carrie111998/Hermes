"""Read the native Codex task catalog through ``codex app-server``.

The Codex CLI, desktop app, and app-server runtime all write to the same
thread store.  ``thread/list`` is the supported way to enumerate that store;
reading rollout JSONL directly misses state-db repairs and newer thread
metadata.  This module deliberately keeps the catalog read-only.  Resuming a
thread is owned by :class:`CodexAppServerSession`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agent.transports.codex_app_server import CodexAppServerClient


@dataclass(frozen=True)
class CodexThreadSummary:
    """Small, display-safe projection of one native Codex thread."""

    thread_id: str
    cwd: str
    preview: str
    name: str = ""
    source: str = "unknown"
    status: str = "unknown"
    updated_at: float = 0.0
    is_pinned: bool = False

    @property
    def title(self) -> str:
        return self.name or self.preview or "(empty task)"


def _nonempty_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _normalize_thread(raw: Any) -> Optional[CodexThreadSummary]:
    if not isinstance(raw, dict):
        return None
    thread_id = _nonempty_string(raw.get("id"))
    if not thread_id:
        return None
    status = raw.get("status")
    status_name = (
        _nonempty_string(status.get("type"))
        if isinstance(status, dict)
        else _nonempty_string(status)
    )
    source = raw.get("source")
    source_name = _nonempty_string(source)
    updated_at = raw.get("recencyAt") or raw.get("updatedAt") or 0
    try:
        updated_at = float(updated_at)
    except (TypeError, ValueError):
        updated_at = 0.0
    return CodexThreadSummary(
        thread_id=thread_id,
        cwd=_nonempty_string(raw.get("cwd")),
        preview=_nonempty_string(raw.get("preview")),
        name=_nonempty_string(raw.get("name")),
        source=source_name or "unknown",
        status=status_name or "unknown",
        updated_at=updated_at,
        is_pinned=raw.get("isPinned") is True,
    )


def list_codex_threads(
    *,
    limit: int = 20,
    search_term: Optional[str] = None,
    cwd: Optional[str] = None,
    cursor: Optional[str] = None,
    codex_bin: str = "codex",
    codex_home: Optional[str] = None,
    timeout: float = 15.0,
    client_factory=CodexAppServerClient,
) -> tuple[list[CodexThreadSummary], Optional[str]]:
    """Return native Codex tasks newest-first plus an optional next cursor.

    ``useStateDbOnly=False`` intentionally asks Codex to scan and repair its
    rollout store.  This is what makes the result match the desktop task list
    even when a rollout has not reached Codex's state DB yet.
    """
    client = client_factory(codex_bin=codex_bin, codex_home=codex_home)
    try:
        client.initialize(
            client_name="hermes",
            client_title="Hermes Agent",
            client_version=_get_hermes_version(),
            capabilities={
                "experimentalApi": True,
                "requestAttestation": False,
            },
        )
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "archived": False,
            "useStateDbOnly": False,
        }
        if search_term and search_term.strip():
            params["searchTerm"] = search_term.strip()
        if cwd and cwd.strip():
            params["cwd"] = cwd.strip()
        if cursor and cursor.strip():
            params["cursor"] = cursor.strip()
        result = client.request("thread/list", params, timeout=timeout)
        raw_data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(raw_data, list):
            raise ValueError("codex thread/list returned a malformed response")
        rows = [row for item in raw_data if (row := _normalize_thread(item))]
        next_cursor = (
            _nonempty_string(result.get("nextCursor"))
            if isinstance(result, dict)
            else ""
        )
        return rows, next_cursor or None
    finally:
        client.close()


def resolve_codex_thread(
    rows: list[CodexThreadSummary], target: str
) -> Optional[CodexThreadSummary]:
    """Resolve a one-based picker number, exact ID, unique prefix, or name."""
    cleaned = (target or "").strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        index = int(cleaned)
        if 1 <= index <= len(rows):
            return rows[index - 1]
        return None

    exact = [row for row in rows if row.thread_id == cleaned]
    if len(exact) == 1:
        return exact[0]
    prefixed = [row for row in rows if row.thread_id.startswith(cleaned)]
    if len(prefixed) == 1:
        return prefixed[0]
    named = [row for row in rows if row.title.casefold() == cleaned.casefold()]
    if len(named) == 1:
        return named[0]
    return None


def _get_hermes_version() -> str:
    try:
        from hermes_cli import __version__

        return str(__version__)
    except Exception:
        return "unknown"
