"""Pure classification and routing for nightly-gate reports.

No I/O and no emitter access: this module decides only *where* a gate failure
should go. Routing is the safety mechanism for emitting every RED cause — the
classes an agent cannot fix are deliberately sent to a target the executor can
never act on, so they stay visible as triaged work a human owns.
"""
from __future__ import annotations

from typing import Optional

# Failures that live in agent-src and are verifiable by re-running a command.
AGENT_CAPABLE_CLASSES = frozenset({"pytest", "ruff"})

AGENT_TARGET = "hermes-clone"
# `hermes` is permanently ineligible by design: live_gateway_imports=true in
# the allowlist. Routing here records the work without ever offering it to
# the executor.
INELIGIBLE_TARGET = "hermes"


def validate_report(payload: object) -> Optional[dict]:
    """Return the report if usable, else None.

    The adapter declines any report missing culprit or failed_command; checking
    here avoids spending an emitter round-trip on a report that cannot succeed.
    """
    if not isinstance(payload, dict):
        return None
    for field in ("culprit", "failed_command"):
        if not str(payload.get(field) or "").strip():
            return None
    return payload


def route_target(failure_class: str) -> str:
    """Map a failure class to a target repo. Unknown classes fail safe."""
    return AGENT_TARGET if str(failure_class or "") in AGENT_CAPABLE_CLASSES else INELIGIBLE_TARGET
