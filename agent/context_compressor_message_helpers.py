"""Message-role and persistence-marker helpers for context compaction."""

from typing import Any, Dict, List, Optional

_DB_PERSISTED_MARKER = "_db_persisted"


def _fresh_compaction_message_copy(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a message for compaction assembly without persistence markers.

    Live cached-gateway transcripts stamp ``_db_persisted`` during incremental
    flushes.  Shallow ``.copy()`` propagates that marker into the post-rotation
    compressed list, so ``_flush_messages_to_session_db`` skips every row when
    writing to the new child session (#57491).

    This strips at the copy site (clearest intent, and cheap), but the
    authoritative guarantee is the single terminal sweep in ``compress()``
    (``_strip_persistence_markers``): no message may leave ``compress()``
    carrying ``_db_persisted`` regardless of how many intermediate copy sites
    a future refactor adds.
    """
    fresh = msg.copy()
    fresh.pop(_DB_PERSISTED_MARKER, None)
    return fresh


def _template_visible_role(message: Any) -> Optional[str]:
    """Role as counted by strict chat-template alternation checks.

    Mistral-family templates (Devstral, Mistral Small 3.x, Magistral)
    enforce user/assistant alternation at render time but EXEMPT the tool
    flow from the check: ``tool`` results and assistant messages carrying
    ``tool_calls`` are skipped. A summary role chosen against the *literal*
    neighbouring roles can therefore still violate alternation as the
    template sees it. The canonical failure: the protected head ends
    ``[user, assistant(tool_calls), tool]``, so the literal last role is
    ``tool`` and the summary is pinned to ``role="user"`` -- but the last
    role the template counts is ``user``, the template sees user -> user,
    and llama.cpp / Mistral-hosted backends reject the ENTIRE request with
    a Jinja alternation error (HTTP 500). Because the summary persists in
    the stored conversation, every retry replays the same poisoned history
    and the session is unrecoverable.

    Returns ``None`` for messages the alternation check skips.
    """
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role == "tool":
        return None
    if role == "assistant" and message.get("tool_calls"):
        return None
    return role


def _strip_persistence_markers(messages: List[Dict[str, Any]]) -> None:
    """Enforce the compaction invariant: no assembled message carries a
    session-store persistence marker.

    ``compress()`` copies protected head/tail messages out of the live
    cached-gateway transcript, which stamps ``_db_persisted`` on every message
    over the life of the session.  If any copied dict keeps that marker, the
    rotation flush to the child session skips it and the compacted transcript is
    lost from ``state.db`` (#57491).  Stripping at each copy site is necessary
    but *positional* — a copy site added after the assembly loops would re-leak.
    This single terminal sweep makes the guarantee structural instead: run it
    once on the fully-assembled list so the invariant holds no matter where the
    copies happened.  Mutates in place (the dicts are compaction-local copies).
    """
    for msg in messages:
        if isinstance(msg, dict):
            msg.pop(_DB_PERSISTED_MARKER, None)
