"""Attestation broker for human approval gates.

WHAT THIS IS
------------
An ``Attestation`` is the object ``kanban_db.release_plan_gate`` requires before
it will release a gate. It can only be produced by a constructor in this module,
and every constructor establishes human presence from a source the caller cannot
supply as an argument.

WHAT THIS IS NOT — read before describing it
--------------------------------------------
Hermes approval gates are an **integrity control, not a security boundary
against arbitrary code execution as your own user account.**

Hermes workers run as the same OS user as you, with terminal and filesystem
access. Any process running as that user can read the credentials on disk. These
gates provide strong, tested protection against:

  * a worker or agent approving its own plan (accidental self-approval),
  * approval reached through ordinary tool invocation, on any surface,
  * prompt injection that emits an approval command,
  * approval of a stale plan whose text has since changed,
  * replay of a previous approval,
  * confused-deputy approval of a different project or board.

They do **not** provide a cryptographic boundary against a deliberately
adversarial process running as your user. A genuine adversarial boundary
requires an independent security domain: separate OS identities, an external
approval service the agent cannot reach, or hardware-backed confirmation. None
of those is in this slice.

WHY THE TTY, SPECIFICALLY
-------------------------
Dispatcher-spawned workers are launched with ``stdin=subprocess.DEVNULL`` and
``start_new_session=True``. ``setsid()`` detaches the controlling terminal, so
``open("/dev/tty")`` raises ENXIO inside any worker, cron job, delegated child,
or background process. Reading the confirmation phrase from ``/dev/tty`` — never
stdin, never argv, never an environment variable — means the phrase is never
present in any model's context.

That defeats an LLM emitting the command, prompt injection whose payload is a
shell command, cron, and every detached execution path. It does **not** defeat
an adversary that allocates a pty and drives the prompt. That is the stated
limit above, not an oversight.

NOTE ON THE LOOPBACK DESKTOP SURFACE
------------------------------------
An earlier design had a "medium strength" tier for the Desktop WebSocket. It was
removed as fictional: on a loopback connection ``WSTransport.auth_identity`` is
``None``, the ``?token=`` path stamps no identity, and the loopback token is
served unauthenticated in ``GET /`` HTML — so any local process can read it. It
attested that *a process on this machine* made a call, which is exactly what a
worker is. Deployment approval is therefore reserved for a separately
authenticated surface, and is not implemented here.
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

# The exact phrase the operator must type. Deliberately not "y" or "yes": it
# must be unambiguous in a scrollback and impossible to hit by accident.
CONFIRM_PHRASES = {"approved": "approve", "rejected": "reject"}

# Back-compat alias for callers/tests that referenced the single-phrase name.
CONFIRM_PHRASE = CONFIRM_PHRASES["approved"]

# Only this module may construct an Attestation. This is a speed bump against
# accidental construction, NOT a security boundary — Python has no private
# constructors, and anything running in-process can read this value. It is here
# so that a *mistake* fails loudly, not so that an adversary is stopped.
_CONSTRUCTOR_TOKEN = object()


class ApprovalProvenanceError(PermissionError):
    """Raised when an approval is attempted from a non-human context."""


class ApprovalSurfaceError(PermissionError):
    """Raised when the surface cannot establish human presence (e.g. no TTY)."""


@dataclass(frozen=True)
class Attestation:
    """Proof that a human confirmed one specific artifact, once.

    ``subject`` names what was approved (``plan:<project_id>:<revision>``) and
    ``binding_hash`` pins the exact bytes, so an attestation cannot be replayed
    against a different revision or a plan whose text has since changed.
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
# Binding
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
# Layer 1 — negative provenance (defence in depth, NOT the boundary)
# ---------------------------------------------------------------------------


def _agent_provenance_reason() -> Optional[str]:
    """Name the agent context we are running inside, or None.

    These signals are advisory: a worker can ``env -u HERMES_KANBAN_TASK``. They
    are here because they make the accidental case — by far the most likely one —
    fail immediately and legibly. The boundary is the positive requirement in
    the constructors below.
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
    """True while a model tool call is executing.

    The ContextVar is set by ``model_tools.handle_function_call`` in the NEXT
    commit of this series. Until then this returns False and the other signals
    carry the load; the import is written defensively so landing that commit
    needs no change here.
    """
    try:
        from agent.delegation_context import in_tool_handler

        return bool(in_tool_handler())
    except Exception:
        return False


def deny_agent_provenance() -> None:
    """Raise when called from any agent-driven context."""
    reason = _agent_provenance_reason()
    if reason is not None:
        raise ApprovalProvenanceError(
            f"Approval refused: running inside {reason}. Human approval gates "
            f"cannot be crossed by an agent. Approve from an interactive "
            f"terminal instead."
        )


# ---------------------------------------------------------------------------
# Layer 2 — positive capability (the actual boundary)
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


def for_plan_decision(
    *,
    project_id: str,
    revision: int,
    plan_body: str,
    decision: str,
    _tty_opener=None,
) -> Attestation:
    """Confirm ONE decision about ONE plan revision at the controlling terminal.

    The caller supplies only *what is being decided*. The broker owns the
    subject, the binding hash, the prompt text, and the confirmation phrase, so
    a caller cannot show the operator one thing and record another. The earlier
    signature took ``subject``, ``binding_hash`` and ``prompt`` from the caller
    and took ``decision`` separately at release time — which allowed a prompt
    describing a rejection to be redeemed as an approval.

    ``plan_body`` is used only to compute the hash the operator is confirming.
    It is not trusted: ``release_plan_gate`` recomputes the same hash from the
    authoritative ``pm_plans`` row inside its transaction and refuses on any
    mismatch. Passing a wrong body therefore fails closed rather than approving
    the wrong text.

    Opens ``/dev/tty`` directly — not stdin. A process with no controlling
    terminal (every dispatcher-spawned worker, cron job, delegated child and
    background process) fails at the open with ENXIO and never sees the prompt.
    """
    if decision not in CONFIRM_PHRASES:
        raise ValueError(f"decision must be one of {sorted(CONFIRM_PHRASES)}")
    deny_agent_provenance()

    subject = plan_subject(project_id, revision)
    binding_hash = plan_binding_hash(project_id, revision, plan_body)
    phrase = CONFIRM_PHRASES[decision]
    verb = "APPROVE" if decision == "approved" else "REJECT"
    prompt = (
        f"\n{verb} plan revision {int(revision)} of project {project_id}?\n"
        f"  plan fingerprint: {binding_hash[:16]}\n"
        f"Type {phrase!r} to confirm, anything else to abort: "
    )

    opener = _tty_opener or _open_controlling_tty
    try:
        tty = opener()
    except OSError as exc:
        raise ApprovalSurfaceError(
            "Approval refused: no controlling terminal. This command must be "
            "run from an interactive shell; it cannot be confirmed from a "
            "worker, a cron job, or a background process."
        ) from exc

    try:
        tty.write(prompt)
        tty.flush()
        typed = tty.readline()
    finally:
        try:
            tty.close()
        except Exception:
            pass

    if (typed or "").strip() != phrase:
        raise ApprovalSurfaceError(
            f"Refused: confirmation phrase not matched (expected {phrase!r})."
        )

    return Attestation(
        subject=subject,
        binding_hash=binding_hash,
        decision=decision,
        surface="cli-tty",
        operator_display=_operator_display(),
        os_user=_os_user(),
        os_uid=getattr(os, "getuid", lambda: None)(),
        host_id=_host_id(),
        tty_path=getattr(tty, "name", "/dev/tty"),
        issued_at=int(time.time()),
        nonce=secrets.token_hex(16),
        _token=_CONSTRUCTOR_TOKEN,
    )


def _open_controlling_tty():
    """Open ``/dev/tty`` read-write. Raises OSError when there is none."""
    return open("/dev/tty", "r+", buffering=1)
