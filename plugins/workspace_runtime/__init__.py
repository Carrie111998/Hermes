"""workspace_runtime plugin — automatic Workspace Discovery for Hermes.

Wires two behaviours:

1. ``on_session_start`` hook — runs Workspace discovery at every fresh
   session, loads the approved four-file bootstrap for a complete Workspace,
   and stores immutable session-scoped context.

2. ``pre_llm_call`` hook — on the first turn of a session
   (``turn_id == 0``), augments the user message body with a stable
   ``<workspace-runtime-verdict>`` block describing the discovery
   outcome. Subsequent turns are NOT augmented (the verdict has been
   surfaced once).

The plugin does NOT mutate the system prompt and does NOT touch the
prefix cache. The first-turn user-message augmentation is the lowest-
impact place to surface a verdict block while keeping Hermes' prompt
construction byte-stable across turns. The verdict block uses stable
prefixes (``<workspace-runtime-verdict>``), so model-side pattern
matching remains reliable.

Why this scope, NOT something bigger:

We explicitly do NOT implement Workspace OS. Workspace OS remains the
canonical operating system that lives in
``/home/taras/projects/GOVERNANCE/`` and ``/home/taras/projects/workspace-os/``.
This plugin's job is to tell the runtime (Hermes) whether Workspace OS
is applicable to the current cwd. If applicable, the model reads
``workspace-os/docs/BOOTSTRAP-PROCEDURE.md`` (its canonical entry point
in this repo) to apply the cold-start procedure.

See:

- ``/home/taras/projects/.project-state/workspace-runtime-cold-start-2026-07-25/RESUME.md``
- ``/home/taras/projects/workspace-os/docs/BOOTSTRAP-PROCEDURE.md``
- ``/home/taras/projects/GOVERNANCE/BOOTSTRAP.md``
- ``/home/taras/projects/GOVERNANCE/CONTEXT-ROUTING.md``
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import discovery as _discovery
from .discovery import (
    DiscoveryVerdict,
    VerdictState,
    render_verdict_block,
    write_telemetry,
)

logger = logging.getLogger(__name__)

# Per-session immutable runtime state. The hook contract always supplies a
# session ID; no cwd-keyed or process-global fallback is permitted.
_verdict_by_session: Dict[str, DiscoveryVerdict] = {}
_context_by_session: Dict[str, str] = {}
_augmented_sessions: Dict[str, bool] = {}
_lock = threading.Lock()

# Optional cross-reference target. When set, the verdict block includes
# a reference to the canonical cold-start procedure.
_DEFAULT_BOOTSTRAP_PROCEDURE_PATH = (
    "/home/taras/projects/workspace-os/docs/BOOTSTRAP-PROCEDURE.md"
)


def _store(session_id: str, verdict: DiscoveryVerdict, context: str) -> None:
    """Store one immutable discovery result for one Hermes session."""
    with _lock:
        _verdict_by_session.setdefault(session_id, verdict)
        _context_by_session.setdefault(session_id, context)


def _retrieve(session_id: Optional[str]) -> Optional[DiscoveryVerdict]:
    if not session_id:
        return None
    with _lock:
        return _verdict_by_session.get(session_id)


def _retrieve_context(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    with _lock:
        return _context_by_session.get(session_id)


def _mark_augmented(session_id: Optional[str]) -> None:
    with _lock:
        if session_id is not None:
            _augmented_sessions[session_id] = True


def _already_augmented(session_id: Optional[str]) -> bool:
    if session_id is None:
        return False
    with _lock:
        return _augmented_sessions.get(session_id, False)


# -----------------------------------------------------------------------------
# Hooks
# -----------------------------------------------------------------------------


def on_session_start(**kwargs: Any) -> None:
    """Discover Workspace at session start.

    Idempotent: if a verdict already exists for this session_id, the
    cached verdict is reused (allows recovery if `on_session_start` fires
    twice). The verdict is also written to telemetry.

    kwargs is the dict passed by ``agent/conversation_loop.py:472-477``:
      {
        "session_id": str,
        "model": str,
        "platform": str,
      }

    Note that cwd is not passed. We use ``Path.cwd()`` (which see
    ``_safe_cwd`` helper for sandboxed environments).
    """
    session_id = kwargs.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        logger.warning("workspace_runtime: session start missing session_id; skipping discovery")
        return
    cached = _retrieve(session_id)
    if cached is not None:
        write_telemetry(cached, session_id=session_id)
        return
    try:
        verdict = _discovery.discover()
    except Exception as exc:  # noqa: BLE001 — never raise from a hook
        logger.exception("workspace_runtime: discover() raised; using ERROR verdict")
        from .discovery import DiscoveryVerdict, VerdictState
        verdict = DiscoveryVerdict(
            state=VerdictState.ERROR,
            cwd=_safe_cwd(),
            error_message=f"{type(exc).__name__}: {exc}",
        )

    _store(session_id, verdict, _build_session_context(verdict))
    write_telemetry(verdict, session_id=session_id)
    logger.info(
        "workspace_runtime: session_id=%s cwd=%s state=%s answerable=%s duration_ms=%d",
        session_id,
        verdict.cwd.as_posix(),
        verdict.state.value,
        verdict.questions_answerable,
        verdict.duration_ms,
    )


def pre_llm_call(**kwargs: Any) -> Any:
    """Augment the first user message of the session with the verdict.

    kwargs is the dict passed by ``agent/conversation_loop.py:1820-1860``.
    Relevant keys: ``session_id``, ``user_message``, ``turn_id``.

    Return value semantics (Hermes expects):
        - None / empty → no augmentation
        - dict with key "context" → append context to the API-bound user turn
    """
    session_id = kwargs.get("session_id")
    user_message = kwargs.get("user_message")
    turn_id = kwargs.get("turn_id")
    if not user_message or not isinstance(user_message, str):
        return None
    if _already_augmented(session_id):
        return None
    # turn_id is an int 0..n. Only the very first user turn gets the block.
    try:
        t0 = int(turn_id or 0)
    except (TypeError, ValueError):
        t0 = 0
    if t0 != 0:
        return None

    verdict = _retrieve(session_id)
    if verdict is None:
        return None
    context = _retrieve_context(session_id)
    if context is None:
        return None
    _mark_augmented(session_id)
    logger.debug(
        "workspace_runtime: augmented first user message (state=%s, +%d chars)",
        verdict.state.value,
        len(context),
    )
    return {"context": context}


# -----------------------------------------------------------------------------
# Session context assembly
# -----------------------------------------------------------------------------


def _build_session_context(verdict: DiscoveryVerdict) -> str:
    verdict_block = render_verdict_block(verdict)
    if verdict.state != VerdictState.INSIDE or verdict.root is None:
        return verdict_block
    try:
        bootstrap = _discovery.load_bootstrap_context(verdict.root, verdict.cwd)
        rendered = _discovery.render_bootstrap_context(bootstrap)
        return f"{verdict_block}\n\n{rendered}"
    except Exception as exc:  # noqa: BLE001 — hook context must fail closed
        logger.exception("workspace_runtime: canonical bootstrap load failed")
        failure = DiscoveryVerdict(
            state=VerdictState.ERROR,
            cwd=verdict.cwd,
            root=verdict.root,
            error_message=f"BootstrapLoadError: {type(exc).__name__}: {exc}",
        )
        return render_verdict_block(failure)


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------


def register(ctx) -> None:
    """Hook registration. Called by hermes_cli.plugins.PluginManager."""
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)


# -----------------------------------------------------------------------------
# Helpers exposed for tests
# -----------------------------------------------------------------------------


def _safe_cwd() -> Path:
    try:
        return Path.cwd().resolve()
    except (OSError, RuntimeError):
        return Path("/")
