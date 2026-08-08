"""Route explicit complex next-move proposals to Kanban.

The model-facing marker is deliberately opt-in.  Ordinary prose is never
classified by keywords, so conversational answers stay in the chat lane.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Optional, Tuple

_MARKER_RE = re.compile(
    r"<!--\s*hermes-next-move\s*:\s*(\{.*?\})\s*-->", re.DOTALL | re.IGNORECASE
)


def inspect_next_move(text: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return visible text and a validated explicit proposal, if present."""
    if not isinstance(text, str):
        return str(text or ""), None
    match = _MARKER_RE.search(text)
    if not match:
        return text, None
    try:
        proposal = json.loads(match.group(1))
    except (TypeError, ValueError):
        return text, None
    if not isinstance(proposal, dict) or proposal.get("proposal") is not True:
        return text, None
    steps = proposal.get("steps")
    title = proposal.get("title")
    if not isinstance(title, str) or not title.strip() or not isinstance(steps, list):
        return text, None
    if not all(isinstance(step, str) and step.strip() for step in steps):
        return text, None
    visible = (text[: match.start()] + text[match.end() :]).strip()
    return visible, proposal


def is_complex_next_move(proposal: Optional[Dict[str, Any]]) -> bool:
    """Require explicit plan evidence plus a multi-step/durable threshold."""
    if not proposal:
        return False
    steps = proposal.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        return False
    flags = ("durable", "asynchronous", "side_effecting", "restart_surviving")
    return len(steps) >= 3 or any(proposal.get(flag) is True for flag in flags)


def _idempotency_key(proposal: Dict[str, Any], session_id: Optional[str]) -> str:
    explicit = proposal.get("idempotency_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    canonical = json.dumps(
        {"session_id": session_id, "proposal": proposal},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "next-move:" + hashlib.sha256(canonical.encode()).hexdigest()


def route_next_move(
    text: Any,
    *,
    session_id: Optional[str],
    create_fn: Callable[..., str],
    assignee: str = "default",
    recursive: bool = False,
) -> Dict[str, Any]:
    """Create one Kanban task for a complex explicit proposal.

    ``create_fn`` is injected so the policy is testable without opening the
    board.  The caller owns the existing Kanban create/subscribe integration.
    """
    visible, proposal = inspect_next_move(text)
    if recursive or not is_complex_next_move(proposal):
        return {"text": visible, "created": False, "proposal": proposal}
    assert proposal is not None
    task_id = create_fn(
        title=proposal["title"].strip(),
        body=json.dumps(
            {
                "plan": proposal,
                "origin_session_id": session_id,
                "user_confirmed": proposal.get("user_confirmed") is True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        assignee=str(proposal.get("assignee") or assignee),
        session_id=session_id,
        idempotency_key=_idempotency_key(proposal, session_id),
    )
    report = f"\n\nI created Kanban task `{task_id}` for that multi-step plan."
    return {"text": (visible + report).strip(), "created": True, "task_id": task_id, "proposal": proposal}
