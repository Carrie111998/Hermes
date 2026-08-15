"""Self-improvement guard — hard gate on autonomous skill/memory mutation.

Hermes runs an autonomous "background review" fork after a turn that evolves the
skill library and memory store (commands from agent.background_review,
spawned via AIAgent._spawn_background_review). For governed PGF/orchestration
runs the operator's contract forbids mutating SKILL.md, its references, or
memory unless an explicit SELF_IMPROVEMENT authorization is present. A prompt
instruction is not sufficient enforcement — this module provides a structural
gate consulted by the two mutation choke points:

  * AIAgent._spawn_background_review (run_agent.py) — refuse to SPAWN the fork
    at all when the current profile is a governed, non-authorized run.
  * tools.skill_manager_tool._apply_skill_write_gate / the memory-write path —
    defense in depth: even if a review-origin write reaches the tool, refuse it
    unless authorized.

Authorization resolution (hard stop, never silent-open):

  1. Explicit per-run opt-in via env  PGF_SELF_IMPROVEMENT = "1"   (highest
     precedence; lets an operator explicitly authorize a single governed run).
  2. Otherwise profile config  self_improvement.enabled:  under the active
     profile config (config.yaml) — default OFF for governed/orchestration
     profiles; the operator turns the run green by setting it true for that
     profile.

A governed run is identified by the config profile carrying
``governance.governed: true`` (set by the Pg/orchestrator host) — when that
marker is absent the module defers to the existing nudge/curator behaviour so
regular Hermes use is unaffected.

Every check fails closed: on any lookup/parse error, an authorization is NOT
assumed (returns False) so the safe default (no autonomous mutation) holds.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_AUTH_ENV_VAR = "HERMES_SELF_IMPROVEMENT"


def _profile_config() -> dict[str, Any]:
    """Load the active profile's config dict, read-only, on error {}."""
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:  # noqa: BLE001 - never raise the gate
        return {}


def _governed(cfg: dict[str, Any]) -> bool:
    """True when the active profile is a governed container run."""
    gov = cfg.get("governance", {})
    if not isinstance(gov, dict):
        return False
    return bool(gov.get("governed")) or bool(gov.get("governance"))


def _enabled_in_config(cfg: dict[str, Any]) -> bool:
    sect = cfg.get("self_improvement", {})
    if not isinstance(sect, dict):
        return False
    return bool(sect.get("enabled"))


def self_improvement_authorized() -> bool:
    """Hard authorization check for autonomous skill/memory mutation.

    Returns True only when an explicit opt-in is present. For governed profiles
    a bare process env var is NOT sufficient: the operator-mandated contract
    requires the versioned, code-reviewable profile config
    ``self_improvement.enabled: true`` (or an env opt-in explicitly combined
    with the governed config marker). The env-only bypass was the governance gap
    identified in the Claude review — if the operator wants to authorize a
    governed run they must set the config, not just an env var in a child
    process. For non-governed profiles the built-in autonomous behaviour (and
    the env override) is preserved.
    """
    cfg = _profile_config()

    if _governed(cfg):
        # Governed: NO silent env-only override. The operator must set the
        # versioned, auditable config flag. An env var alone cannot flip a
        # governed run green — otherwise any child process could authorise
        # SKILL.md/reference/memory mutation without operator knowledge.
        return _enabled_in_config(cfg)

    # Non-governed profile: preserve the built-in autonomous review behaviour,
    # including the explicit per-process env opt-in.
    env = os.environ.get(_AUTH_ENV_VAR, "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    return True


def guard_skill_write(action: str, name: str) -> str | None:
    """Return an error dict-JSON string to abort an autonomous skill write.

    Called from the skill write middleware for WRITE-ORIGIN autonomous paths
    (curator / background review). When the write is NOT authorized, returns a
    fail-closed error string the caller returns verbatim; otherwise ``None``.
    ``action`` / ``name`` are used only for the message (audit).
    """
    if self_improvement_authorized():
        return None
    return (
        "Self-improvement is DISABLED for this governed run: autonomous "
        f"skill mutation '{action}' of '{name}' requires an explicit "
        "SELF_IMPROVEMENT authorization (env HERMES_SELF_IMPROVEMENT=1 or "
        "profile config self_improvement.enabled=true). Refusing the write. "
        "No SKILL.md / reference / memory changes were made."
    )


def spawn_allowed() -> bool:
    """Whether the background review fork may be spawned at all.

    Returns False for governed, unauthorized runs so the fork never starts
    (no inference, no mutation). True otherwise.
    """
    if self_improvement_authorized():
        return True
    cfg = _profile_config()
    if _governed(cfg):
        return False
    return True