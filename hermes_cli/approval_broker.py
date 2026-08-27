"""Attestation broker for human approval gates.

STATUS: NO LOCAL APPROVAL AUTHORITY
-----------------------------------
There is deliberately **no local surface that can approve a plan.** Every call to
:func:`for_plan_decision` fails closed until a separately authenticated approval
adapter is configured, and none ships in this slice.

WHY THE LOCAL CLI WAS REMOVED AS AN AUTHORITY
---------------------------------------------
An earlier revision confirmed human presence by reading a fixed phrase from
``/dev/tty``, backed by tool-origin provenance (a ContextVar, an environment
marker, and a PID registry checked through process ancestry). Two independent
reviews and a reproduction retired that design:

1. A model tool can allocate a real PTY (``process_registry.spawn_local(...,
   use_pty=True)``) and drive the prompt, so the TTY alone proves nothing.
2. The provenance marks lived in same-user-writable locations. The reproduction
   that settled it:

       rm -rf <registry roots>; unset HERMES_TOOL_ORIGIN;
       env -u HERMES_TOOL_ORIGIN hermes project approve-plan <task>

   run inside a real tool call on a real PTY: prompt displayed, phrase accepted,
   task released, approval row written.

Hardening that design further does not work, and the reason is structural rather
than a missing check:

* Any marker a local process can read, a same-user process can erase.
* Ancestry cannot substitute. A process can ``fork`` twice to orphan itself,
  shedding the ancestry that would betray it, and then allocate its **own** PTY.
  The result has no Hermes ancestor, no Hermes terminal, and no marker — it is
  kernel-indistinguishable from a human's shell. There is no kernel-verifiable
  property of "is a human at a terminal" to test for.
* Blocking the CLI is not a boundary either: the same process can call this
  module directly, or write ``kanban.db`` with sqlite3, since both are
  same-user writable.

**Software running under the same macOS user cannot establish trustworthy human
provenance by process inspection alone.** That is the finding, and it is why the
authority moved out rather than being reinforced.

WHAT REMAINS, AND WHY
---------------------
Everything the database layer needs to *consume* an approval is intact and
tested: the subject, the binding hash over the exact plan bytes, the decision
binding, the single-use nonce, the TTL, and the atomic transaction in
``kanban_db.release_plan_gate``. A future adapter mints an
:class:`Attestation` through :func:`issue_attestation_for_adapter` and that
machinery accepts it unchanged. None of it had to be rebuilt, because none of it
was what failed — the *surface* was.

NOTE ON THE LOOPBACK DESKTOP SURFACE
------------------------------------
The loopback token is **not** independent authentication and must never be
treated as such. On a loopback connection ``WSTransport.auth_identity`` is
``None``, the ``?token=`` path stamps no identity, and the token is served
unauthenticated in ``GET /`` HTML, so any local process can read it. It attests
that *a process on this machine* made a call, which is exactly what a worker is.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

# Attestations expire quickly: the window between a human reading a plan and
# confirming it is seconds, and a long-lived object is a replay target.
DEFAULT_TTL_SECONDS = 300

# Phrase vocabulary a future confirmation surface may use. Deliberately not
# "y"/"yes": unambiguous in a scrollback and impossible to hit by accident.
CONFIRM_PHRASES = {"approved": "approve", "rejected": "reject"}

# Back-compat alias for callers/tests that referenced the single-phrase name.
CONFIRM_PHRASE = CONFIRM_PHRASES["approved"]

VALID_DECISIONS = frozenset(CONFIRM_PHRASES)

# Only this module may construct an Attestation. A speed bump against accidental
# construction, NOT a security boundary — Python has no private constructors,
# and anything running in-process can read this value. It is here so that a
# *mistake* fails loudly, not so that an adversary is stopped.
_CONSTRUCTOR_TOKEN = object()


class ApprovalProvenanceError(PermissionError):
    """Raised when an approval is attempted from a non-human context."""


class ApprovalSurfaceError(PermissionError):
    """Raised when the surface cannot establish human presence."""


class NoApprovalSurfaceError(ApprovalSurfaceError):
    """Raised because no separately authenticated approval surface is configured.

    This is the normal state in this release. It is a subclass of
    ``ApprovalSurfaceError`` so existing callers that already handle a refusal
    keep working, and a distinct type so a caller can tell "you are not allowed"
    from "there is nowhere to ask".
    """


NO_SURFACE_MESSAGE = (
    "Approval refused: no separately authenticated approval surface is "
    "configured, so this plan gate cannot be released from this machine.\n"
    "\n"
    "This is deliberate, not a misconfiguration. Local approval was removed as "
    "an authority: a process running as your own user can allocate a terminal, "
    "erase any local marker, and orphan itself, so nothing a local check can "
    "observe distinguishes a human from an agent. Releasing a gate requires an "
    "approval surface the agent holds no credential for; none ships yet."
)


@dataclass(frozen=True)
class Attestation:
    """Proof that a human confirmed one specific artifact, once.

    ``subject`` names what was approved (``plan:<project_id>:<revision>``) and
    ``binding_hash`` pins the exact bytes, so an attestation cannot be replayed
    against a different revision or a plan whose text has since changed.

    Consumed by ``kanban_db.release_plan_gate``, which re-derives both from the
    authoritative row inside its own transaction and refuses on any mismatch.
    """

    subject: str
    binding_hash: str
    decision: str
    surface: str
    operator_display: str
    os_user: str
    os_uid: Optional[int]
    host_id: str
    tty_path: Optional[str]
    issued_at: int
    nonce: str
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _CONSTRUCTOR_TOKEN:
            raise ApprovalProvenanceError(
                "Attestation must be produced by hermes_cli.approval_broker; "
                "constructing one directly does not establish human presence."
            )

    def expired(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                now: Optional[int] = None) -> bool:
        return (now if now is not None else int(time.time())) - self.issued_at > ttl_seconds

    def audit_fields(self) -> dict:
        """Columns for the ``pm_approvals`` row. Never includes the nonce value
        in user-facing output; the DB stores it to enforce single use."""
        return {
            "subject": self.subject,
            "binding_hash": self.binding_hash,
            "decision": self.decision,
            "surface": self.surface,
            "operator_display": self.operator_display,
            "os_user": self.os_user,
            "os_uid": self.os_uid,
            "host_id": self.host_id,
            "tty_path": self.tty_path,
            "nonce": self.nonce,
        }


# ---------------------------------------------------------------------------
# Binding — unchanged, and deliberately so
# ---------------------------------------------------------------------------


def plan_subject(project_id: str, revision: int) -> str:
    return f"plan:{project_id}:{int(revision)}"


def plan_binding_hash(project_id: str, revision: int, plan_body: str) -> str:
    """Bind an attestation to the exact plan text that was read.

    Approving revision 2 does not authorise revision 3, and editing revision 2's
    body after it was read invalidates the attestation rather than silently
    approving different words.
    """
    h = hashlib.sha256()
    h.update(plan_subject(project_id, revision).encode("utf-8"))
    h.update(b"\x00")
    h.update((plan_body or "").encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Advisory provenance — defence in depth for a future adapter, NOT a boundary
# ---------------------------------------------------------------------------


def _agent_provenance_reason() -> Optional[str]:
    """Name the agent context we are running inside, or None.

    **Advisory only.** Every signal here is erasable by the process it describes
    (``env -u``, ``env -i``, or simply not being in this interpreter). It is kept
    because it makes the *accidental* case — by far the most likely one — fail
    immediately and legibly, and because a future authenticated adapter should
    still decline to prompt when it can see it is inside an agent turn.

    It is not consulted before the surface check in :func:`for_plan_decision`,
    and nothing relies on it to refuse.
    """
    try:
        from agent.delegation_context import (
            DELEGATED_CHILD_ENV_MARKER,
            is_delegated_child_context,
        )

        if is_delegated_child_context():
            return "delegate_task child context"
        if os.environ.get(DELEGATED_CHILD_ENV_MARKER):
            return "delegate_task child process"
    except Exception:
        if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
            return "delegate_task child process"

    if os.environ.get("HERMES_KANBAN_TASK"):
        return "kanban worker (HERMES_KANBAN_TASK is set)"
    if os.environ.get("HERMES_KANBAN_RUN_ID"):
        return "kanban worker (HERMES_KANBAN_RUN_ID is set)"
    if os.environ.get("HERMES_CRON_SESSION"):
        return "cron session"

    try:
        from gateway.session_context import get_session_env

        if (get_session_env("HERMES_SESSION_SOURCE", "") or "").strip() == "kanban":
            return "kanban-sourced session"
        if (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip():
            return "gateway session turn"
    except Exception:
        if (os.environ.get("HERMES_SESSION_SOURCE") or "").strip() == "kanban":
            return "kanban-sourced session"

    if _in_tool_handler():
        return "model tool handler"
    return None


def _in_tool_handler() -> bool:
    """True while a model tool call is executing in this interpreter."""
    try:
        from agent.delegation_context import in_tool_handler

        return bool(in_tool_handler())
    except Exception:
        return False


def deny_agent_provenance() -> None:
    """Raise when called from a recognisable agent-driven context.

    Advisory. See :func:`_agent_provenance_reason`. Callers must not treat a
    silent return as evidence of human presence.
    """
    reason = _agent_provenance_reason()
    if reason is not None:
        raise ApprovalProvenanceError(
            f"Approval refused: running inside {reason}. This is an advisory "
            f"check, not a boundary — it recognises the ordinary case rather "
            f"than preventing a determined one."
        )


# ---------------------------------------------------------------------------
# The adapter seam — where a real boundary will attach
# ---------------------------------------------------------------------------


def resolve_plan_approval_adapter():
    """Return the configured approval adapter, or None.

    **Always None in this release.** No adapter ships, and there is deliberately
    no way to name one from configuration yet: a config key alone would invite a
    local shim that re-creates the surface this design removed. When an adapter
    lands it must satisfy one property — the agent must hold no credential that
    can drive it — and that is a property of the adapter's trust domain, not of
    anything this module can check.

    See ``planning/APPROVAL-SURFACE-DESIGN-NOTE.md`` for the intended first
    implementation (a macOS-native helper requiring fresh Touch ID).
    """
    return None


def issue_attestation_for_adapter(
    *,
    project_id: str,
    revision: int,
    plan_body: str,
    decision: str,
    surface: str,
    operator_display: str,
    tty_path: Optional[str] = None,
) -> Attestation:
    """Mint an Attestation on behalf of an authenticated approval adapter.

    The **only** constructor. The broker owns the subject, the binding hash, the
    nonce and the timestamp so that an adapter cannot show an operator one thing
    and record another; the adapter contributes only who authenticated and where.

    This does not authenticate anything. Calling it asserts that the caller has
    *already* established human presence in a trust domain the agent cannot
    reach. It is reachable in-process, which is not a defect to fix here: a
    process that can call it can also write ``kanban.db`` directly. That is the
    same limit that moved the authority out of this machine.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")
    return Attestation(
        subject=plan_subject(project_id, revision),
        binding_hash=plan_binding_hash(project_id, revision, plan_body),
        decision=decision,
        surface=surface,
        operator_display=operator_display,
        os_user=_os_user(),
        os_uid=getattr(os, "getuid", lambda: None)(),
        host_id=_host_id(),
        tty_path=tty_path,
        issued_at=int(time.time()),
        nonce=secrets.token_hex(16),
        _token=_CONSTRUCTOR_TOKEN,
    )


def for_plan_decision(
    *,
    project_id: str,
    revision: int,
    plan_body: str,
    decision: str,
    display_context: Optional[dict] = None,
) -> Attestation:
    """Obtain an attestation for ONE decision about ONE plan revision.

    Fails closed with :class:`NoApprovalSurfaceError` whenever no separately
    authenticated adapter is configured — which is every call in this release.

    The refusal is unconditional and happens **first**. It does not depend on
    detecting the caller: there is no terminal, PTY, stdin, flag, environment
    variable, cron job, gateway route, loopback session, delegated child, MCP
    tool, code-execution child, orphaned process or same-user subprocess that
    can reach an approval, because there is no local approval endpoint to reach.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")

    adapter = resolve_plan_approval_adapter()
    if adapter is None:
        raise NoApprovalSurfaceError(NO_SURFACE_MESSAGE)

    return adapter.confirm_plan_decision(          # pragma: no cover - none ships
        project_id=project_id,
        revision=revision,
        plan_body=plan_body,
        decision=decision,
        display_context=display_context,
    )


# ---------------------------------------------------------------------------
# Identity helpers (recorded in the audit row, not used to authenticate)
# ---------------------------------------------------------------------------


def _operator_display() -> str:
    try:
        from hermes_cli.config import load_config

        name = ((load_config().get("approvals") or {}).get("operator_name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return _os_user()


def _os_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or "unknown"


def _host_id() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"
