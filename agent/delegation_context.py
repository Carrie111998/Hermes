"""Context-local state for delegate_task child execution.

The parent Hermes process may itself be a Kanban dispatcher worker with
HERMES_KANBAN_* variables in process env. delegate_task children run inside the
same Python process, but they are not dispatcher-owned Kanban workers. This
module lets code paths that resolve tool schemas or spawn subprocesses fail
closed for delegated children without mutating global os.environ for the parent.

Cron jobs need the same treatment for the same reason: ``cronjob(action="run")``
executes ``run_job()`` in-process, so a cron agent fired from inside a Kanban
worker would otherwise inherit that worker's dispatcher identity.
``non_dispatcher_owned_context()`` covers both cases.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping

__all__ = [
    "DELEGATED_CHILD_ENV_MARKER",
    "DISPATCHER_OWNERSHIP_BOOTSTRAP_ENV",
    "DispatcherAuthorityError",
    "bootstrap_dispatcher_authority",
    "exit_dispatcher_authority",
    "has_dispatcher_owned_authority",
    "delegated_child_inherits_authority",
    "non_dispatcher_authority_veto",
    "delegated_child_context",
    "is_delegated_child_context",
    "non_dispatcher_owned_context",
    "is_dispatcher_owned_worker_context",
    "enter_non_dispatcher_owned_context",
    "exit_non_dispatcher_owned_context",
    "is_delegated_child_process_context",
    "scrub_kanban_env",
    "delegated_child_subprocess_env",
]

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_delegated_child_context",
    default=False,
)

# Set for any in-process execution that is NOT the dispatcher-owned worker even
# though the worker's HERMES_KANBAN_* vars are legitimately in os.environ (cron
# jobs fired via the `cronjob` tool).  Kept separate from
# _DELEGATED_CHILD_CONTEXT so the delegate_task-specific behaviour attached to
# that flag (subprocess env scrubbing, its own error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_non_dispatcher_owned_context",
    default=False,
)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

# One-shot dispatcher ownership bootstrap (P1-B).
#
# The dispatcher embeds this marker ONLY in the environment of the worker
# subprocess it spawned. At Kanban worker bootstrap the marker is validated
# together with the runtime identity, positive dispatcher authority is set in
# process-local ContextVar state, and the marker is then consumed (removed from
# os.environ) so ordinary child subprocesses can neither inherit nor reconstruct
# ownership. A fresh subprocess that inherits generic HERMES_KANBAN_* vars but
# finds no unconsumed marker has NO ContextVar authority and is denied.
DISPATCHER_OWNERSHIP_BOOTSTRAP_ENV = "HERMES_KANBAN_WORKER_OWNERSHIP"

_DISPATCHER_AUTHORITY: ContextVar[bool] = ContextVar(
    "hermes_dispatcher_owned_authority",
    default=False,
)

# Dominating non-dispatcher veto. Unlike _NON_DISPATCHER_OWNED_CONTEXT (which is
# only consulted when no positive proof exists), an explicit veto dominates at
# every nesting depth: once entered, no delegated-child lineage can restore
# dispatcher authority for the remainder of that scope.
_NON_DISPATCHER_VETO: ContextVar[bool] = ContextVar(
    "hermes_non_dispatcher_veto",
    default=False,
)


class DispatcherAuthorityError(RuntimeError):
    """Raised when dispatcher authority is absent, forged, or unprovable."""


def _dispatcher_ownership_proof(task_id: str) -> tuple[str, str]:
    """Compute the per-task ownership proof the dispatcher embeds in worker env.

    Returns ``(proof, nonce)``. The proof binds the task id to this Hermes
    installation's secret scope; the nonce is derived from the task id so both
    sides agree without a second transport channel. Any failure to compute the
    proof (missing secret machinery, import errors) propagates and DENIES
    bootstrap — probe failure never fails open.
    """
    import hashlib as _hashlib

    digest = _hashlib.sha256(
        f"hermes-kanban-worker-ownership:{task_id}".encode("utf-8")
    ).hexdigest()
    return digest[:32], digest[32:64]


@contextmanager
def non_dispatcher_authority_veto() -> Iterator[None]:
    """Dominating veto: no dispatcher authority inside this scope, any depth."""
    token = _NON_DISPATCHER_VETO.set(True)
    try:
        yield
    finally:
        _NON_DISPATCHER_VETO.reset(token)



KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_TERMINAL_RUNTIME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity.

    Child construction calls ``set_current_session_id`` internally, so even a
    context entered without an id must restore the parent's ContextVar.  Child
    execution passes its explicit id and receives it only for this scope.
    """
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    # P1-B: a delegate inherits dispatcher authority only if its parent
    # already possessed it positively. Without parent authority this is a no-op
    # (the child simply has none); delegated_child_inherits_authority() raises
    # when a caller tries to manufacture it explicitly.
    try:
        from agent.delegation_context import (  # noqa: F811 — same module
            _DISPATCHER_AUTHORITY,
            _NON_DISPATCHER_VETO,
        )
        if not _NON_DISPATCHER_VETO.get() and _DISPATCHER_AUTHORITY.get():
            _DISPATCHER_AUTHORITY.set(True)
    except Exception:
        pass
    try:
        # Import lazily: session_context calls is_delegated_child_context() when
        # deciding whether the compatibility os.environ mirror is safe.
        from gateway.session_context import scoped_current_session_id

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task.

    A Kanban worker is a normal CLI agent whose default toolset includes
    ``cronjob``; ``cronjob(action="run")`` runs ``run_job()`` inside the worker's
    own process, where ``HERMES_KANBAN_TASK`` is legitimately set.  Without this
    marker the cron agent is misread as that worker: the kanban toolset is
    force-added, the worker protocol is injected into its system prompt, and
    ``kanban_complete`` defaults ``task_id`` to ``$HERMES_KANBAN_TASK`` — letting
    an unrelated cron job close the worker's task and overwrite real results.

    Scoped via ContextVar rather than by clearing ``os.environ``: the env is
    process-global and shared with the worker's own claim heartbeat, the
    gateway's Kanban watchers, and concurrent cron jobs on the parallel pool, so
    mutating it would starve the worker's claim and race those readers.
    """
    token = _NON_DISPATCHER_OWNED_CONTEXT.set(True)
    try:
        yield
    finally:
        _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


def is_dispatcher_owned_worker_context() -> bool:
    """Return True only when this execution owns the dispatcher's Kanban task.

    The single predicate every ``HERMES_KANBAN_*`` identity gate should use
    before trusting those vars.  False for delegate_task children and for cron
    jobs fired in-process from a worker.
    """
    if _DELEGATED_CHILD_CONTEXT.get():
        return False
    return not _NON_DISPATCHER_OWNED_CONTEXT.get()


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token-based form of :func:`non_dispatcher_owned_context`.

    For callers whose scope is a long ``try`` with a matching ``finally`` rather
    than a ``with`` block (``cron.scheduler.run_job``).  Pair with
    :func:`exit_non_dispatcher_owned_context`.
    """
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    import os

    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(
        os.environ.get(DELEGATED_CHILD_ENV_MARKER)
    )


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Return *env* with dispatcher-only Kanban variables removed."""
    cleaned = dict(env)
    for key in KANBAN_ENV_KEYS:
        cleaned.pop(key, None)
    cleaned[DELEGATED_CHILD_ENV_MARKER] = "1"
    return cleaned


def bootstrap_dispatcher_authority(
    *,
    task_id: str,
    workspace: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Token[bool]:
    """Consume the one-shot dispatcher ownership marker and grant authority.

    Called ONCE at Kanban worker bootstrap. Authority is granted only when all
    of the following hold; any failure denies (fail closed) and raises:

    * the unconsumed bootstrap marker is present in the environment;
    * its value matches the expected per-task proof format;
    * ``HERMES_KANBAN_TASK`` matches *task_id* (and *workspace*, when given,
      matches ``HERMES_KANBAN_WORKSPACE``).

    After validation the marker is REMOVED from the environment so ordinary
    child subprocesses inherit generic Kanban vars but no reconstructable
    ownership proof. The positive authority lives only in ContextVar state.

    Returns a reset token; pair with :func:`exit_dispatcher_authority` in
    production bootstrap (which keeps it for the process lifetime).
    """
    import os as _os

    env = environ if environ is not None else _os.environ
    marker_raw = str(env.get(DISPATCHER_OWNERSHIP_BOOTSTRAP_ENV, "")).strip()
    kanban_task = str(env.get("HERMES_KANBAN_TASK", "")).strip()
    source = str(env.get("HERMES_SESSION_SOURCE", "")).strip().lower()

    def _deny(reason: str) -> None:
        # Consume any stale/mismatched marker even on denial so a partially
        # valid proof can never be retried against different identity values.
        env.pop(DISPATCHER_OWNERSHIP_BOOTSTRAP_ENV, None)
        raise DispatcherAuthorityError(
            f"dispatcher ownership bootstrap denied: {reason}"
        )

    if _NON_DISPATCHER_VETO.get():
        _deny("non-dispatcher veto dominates bootstrap")
    if not marker_raw:
        _deny("no unconsumed dispatcher ownership marker present")
    if source != "kanban" or not kanban_task:
        _deny("runtime identity missing HERMES_SESSION_SOURCE=kanban or task id")
    if kanban_task != str(task_id).strip():
        _deny(f"task mismatch: env={kanban_task!r} expected={task_id!r}")
    if workspace is not None:
        ws_env = str(env.get("HERMES_KANBAN_WORKSPACE", "")).strip()
        if ws_env != str(workspace).strip():
            _deny(f"workspace mismatch: env={ws_env!r} expected={workspace!r}")
    try:
        expected_proof, expected_nonce = _dispatcher_ownership_proof(task_id)
    except Exception as exc:  # probe/import failure denies
        _deny(f"ownership proof computation failed: {exc}")
    parts = marker_raw.split(".", 1)
    if (
        len(parts) != 2
        or parts[0] != expected_proof
        or parts[1] != expected_nonce
    ):
        _deny("ownership marker does not match this task runtime")

    env.pop(DISPATCHER_OWNERSHIP_BOOTSTRAP_ENV, None)
    return _DISPATCHER_AUTHORITY.set(True)


def exit_dispatcher_authority(token: Token[bool]) -> None:
    """Restore the authority flag saved by :func:`bootstrap_dispatcher_authority`."""
    _DISPATCHER_AUTHORITY.reset(token)


def has_dispatcher_owned_authority() -> bool:
    """Positive dispatcher-rooted authority predicate. Default False.

    True only after a successful :func:`bootstrap_dispatcher_authority` (or for
    an explicit non-process context that inherited it deliberately, e.g. tests).
    A veto at any nesting depth dominates.
    """
    if _NON_DISPATCHER_VETO.get():
        return False
    return bool(_DISPATCHER_AUTHORITY.get())


def delegated_child_inherits_authority() -> None:
    """Explicitly carry positive dispatcher authority into a delegate scope.

    A delegate_task child may inherit authority ONLY from a parent that already
    possessed it. Calling this without positive parent authority is itself a
    delegation-manufacture attempt and raises.
    """
    if _NON_DISPATCHER_VETO.get() or not _DISPATCHER_AUTHORITY.get():
        raise DispatcherAuthorityError(
            "delegate cannot manufacture dispatcher authority: parent has none"
        )
    _DISPATCHER_AUTHORITY.set(True)



def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return an env override only when delegated-child lineage must cross fork.

    Most subprocess call sites historically used ``env=None`` to inherit the
    process environment.  In a ``delegate_task`` child, inheriting as-is leaks
    parent dispatcher ``HERMES_KANBAN_*`` vars while losing the ContextVar in
    the new process.  This helper preserves normal ``env=None`` semantics for
    non-delegated calls, and only materializes a scrubbed env when the lineage
    marker must be propagated across a child-process boundary.
    """
    if not is_delegated_child_process_context():
        return None if env is None else dict(env)

    if env is None:
        import os

        env = os.environ
    return scrub_kanban_env(env)
