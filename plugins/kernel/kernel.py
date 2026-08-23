"""Kernel: the harness's own audit invariant for outgoing model calls.

Grounded in two external precedents, surveyed in depth before this was
written (internal-docs/harness/execution-architecture.md, MershLab's
private research, not part of this repo): DeepSeek Harness's
deriveMessages()/invariant.ts (byte-exact request reconstruction from an
append-only log) and OpenClaw's enforcement.coverageState gradient (an
honest confidence label on whether a check actually enforced anything,
not just pass/fail). MershLab's own mershtrust.adapters.llm_adapter
mirrors the same provenance-hash shape (request_hash/response_hash/
provenance_hash) for a different product; this module follows that same
vocabulary in plain stdlib rather than taking a runtime dependency on
mershtrust's package, which pulls in numpy for zero-knowledge/TEE
machinery this check has no use for.

What this can and cannot do, stated plainly, not left to be discovered
later: Hermes's plugin hooks (pre_api_request/post_api_request) are
observer-only by design — every callback exception is caught and logged,
never propagated (hermes_cli/plugins.py, PluginManager.invoke_hook) — so
nothing registered here can block a call in flight. coverage_state is
therefore never "enforced" in the OpenClaw sense; the strongest honest
value this module can produce is "attribution-only": a divergence is
detected and recorded, loudly, but not prevented. Blocking would need a
core patch to conversation_loop.py's hook call sites — a real, named
follow-up, not built here.

The check itself is also a deliberately smaller claim than DeepSeek's
full byte-exact deriveMessages() replay. That needs an independent,
from-scratch event log walking every user/tool/assistant event and
reconstructing the request from it — which needs hook coverage this
plugin architecture doesn't expose (pre_llm_call fires for context
injection, not full message assembly; see the module's use of
pre_api_request/post_api_request instead, the only pair that exposes the
actual outgoing request_messages). What's built here is the concrete,
weaker-but-real subset: message history for a session must never
silently shrink between two consecutive outgoing calls. A truncation
bug, a corrupted cache reload, or a race condition clobbering context all
show up as an unexplained drop in message_count between consecutive
calls in the same session — this catches exactly that, honestly scoped
to what the hook surface actually allows.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CoverageState(str, Enum):
    """How confident a record is that its check was actually meaningful.

    Mirrors OpenClaw's enforcement.coverageState gradient
    (internal-docs/harness/openclaw/audit.md), not invented here.
    """

    ATTRIBUTION_ONLY = "attribution-only"  # detected + recorded, not blockable
    UNKNOWN = "unknown"  # no prior baseline in this session to compare against


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace differences."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass
class KernelEvent:
    kind: str  # "api_request" | "api_response" | "kernel_violation"
    session_id: str
    api_request_id: str
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    model: str = ""
    provider: str = ""
    message_count: int = 0
    request_hash: str = ""
    response_hash: str = ""
    coverage_state: str = CoverageState.UNKNOWN.value
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def append_event(log_path: str, event: KernelEvent) -> None:
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(canonical_json(event.to_dict()))
        f.write("\n")


def load_session_events(log_path: str, session_id: str) -> list[dict]:
    if not os.path.exists(log_path):
        return []
    events: list[dict] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("session_id") == session_id:
                events.append(rec)
    return events


def last_request_event(log_path: str, session_id: str) -> Optional[dict]:
    events = [
        e for e in load_session_events(log_path, session_id) if e.get("kind") == "api_request"
    ]
    return events[-1] if events else None


def check_continuity(
    session_id: str,
    message_count: int,
    log_path: str,
) -> tuple[str, Optional[dict]]:
    """Compare against the last logged request in this session.

    Returns (coverage_state, violation_detail_or_None). A violation means
    the message count dropped without this module having any record of
    why — the real, concrete failure mode this guards against, stated in
    the module docstring.
    """
    prior = last_request_event(log_path, session_id)
    if prior is None:
        return CoverageState.UNKNOWN.value, None
    prior_count = prior.get("message_count", 0)
    if message_count < prior_count:
        return CoverageState.ATTRIBUTION_ONLY.value, {
            "prior_api_request_id": prior.get("api_request_id"),
            "prior_message_count": prior_count,
            "current_message_count": message_count,
        }
    return CoverageState.ATTRIBUTION_ONLY.value, None
