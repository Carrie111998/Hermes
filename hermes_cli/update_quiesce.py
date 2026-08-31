"""Fail-closed pre-mutation quiescing for live Hermes self-updates.

``hermes update`` mutates a checkout that every running Hermes runtime
imports from.  Historically the mutation happened first and the restart
phase ran minutes later, which left a window where a still-running
gateway/dashboard/serve interpreter lazily imported a module from the
NEW tree into its OLD module graph — a torn module graph, and the class
of failure the in-process ``sys.modules`` purge in ``update_cmd`` cannot
touch (that purge only protects the updater's own interpreter).

The contract implemented here runs BEFORE any git or dependency
mutation:

1. **Updater ownership** — the updater must not live inside any affected
   runtime's supervisor cgroup or process tree.  Otherwise stopping that
   runtime (``systemctl restart``, a killed process group) takes the
   updater down mid-mutation.
2. **Complete inventory** — every runtime the plan saw must carry the
   identity needed to stop it now and relaunch it later.
3. **Confirmed quiesce** — every inventoried runtime is stopped and its
   old PID observed gone.

Any failure in 1–3 raises :class:`QuiesceAbort` and the update aborts
*before* mutating anything.  Success authorizes mutation process-wide;
:func:`assert_mutation_authorized` is the gate the mutation sites call,
so an unauthorized code path cannot silently mutate the checkout.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# How long a stopped runtime gets to actually leave the process table
# before we declare the quiesce failed. Old-PID exit is the only proof
# that the interpreter can no longer import from the mutated checkout.
DEFAULT_EXIT_TIMEOUT = 30.0
# Budget after a forced escalation (SIGKILL / taskkill /F). Short: at this
# point the kernel, not the process, decides.
DEFAULT_ESCALATED_EXIT_TIMEOUT = 15.0
DEFAULT_POLL_INTERVAL = 0.1


class QuiesceAbort(RuntimeError):
    """Raised when the fleet could not be proven quiesced before mutation."""


@dataclass
class IsolationResult:
    """Whether the updater owns itself, independent of the fleet."""

    isolated: bool
    reason: str = ""
    updater_cgroup: Optional[str] = None
    conflicts: list = field(default_factory=list)


@dataclass
class QuiesceReport:
    """What the pre-mutation quiesce actually did."""

    isolation: Optional[IsolationResult] = None
    quiesced_pids: list = field(default_factory=list)
    runtimes: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "isolated": bool(self.isolation and self.isolation.isolated),
            "isolation_reason": self.isolation.reason if self.isolation else "",
            "quiesced_pids": list(self.quiesced_pids),
            "runtimes": [
                r.to_dict() if hasattr(r, "to_dict") else dict(r)
                for r in self.runtimes
            ],
        }


# Process-wide authorization token. ``None`` means "no confirmed quiesce
# in this process", which is the state every mutation site must refuse to
# act in.
_authorized: Optional[QuiesceReport] = None


def reset_mutation_authorization() -> None:
    """Drop the authorization token (test hygiene / explicit re-arm)."""
    global _authorized
    _authorized = None


def authorized_report() -> Optional[QuiesceReport]:
    """The report that authorized mutation, or ``None`` when unauthorized."""
    return _authorized


def assert_mutation_authorized(what: str) -> QuiesceReport:
    """Gate a mutation site. Raises :class:`QuiesceAbort` when unquiesced.

    This is deliberately a hard failure rather than a warning: the whole
    point of the phase is that a mutation which happens without a proven
    quiesce is the bug.
    """
    if _authorized is None:
        raise QuiesceAbort(
            f"refusing to mutate ({what}): the running Hermes fleet was never "
            "quiesced — updater isolation, runtime inventory, or a runtime "
            "stop did not complete"
        )
    return _authorized


def _runtime_label(runtime: Any) -> str:
    kind = getattr(runtime, "kind", "runtime")
    profile = getattr(runtime, "profile", "?")
    pid = getattr(runtime, "pid", None)
    return f"{kind}[{profile}] pid={pid}"


def relaunch_authority(runtime: Any) -> str:
    """The exact authority that will bring *runtime* back, or ``""``.

    Preserving this BEFORE the stop is the whole reason inventory runs
    pre-mutation: a custom systemd unit or a manual argv is unreadable
    once the process (and its cgroup) is gone.
    """
    unit = str(getattr(runtime, "unit", "") or "")
    if unit:
        return unit
    detail = getattr(runtime, "detail", None) or {}
    argv = detail.get("argv") if isinstance(detail, dict) else None
    if argv:
        return "argv"
    supervisor = str(getattr(runtime, "supervisor", "") or "")
    if supervisor in ("desktop",):
        # The Desktop app respawns its own backend; no unit/argv needed.
        return supervisor
    return ""


def verify_inventory_complete(plan: Any) -> list:
    """Return the runtimes to quiesce, or raise when the plan is unusable.

    Fail-closed: a plan that could not be collected at all, or a runtime
    row with no PID (nothing to stop, nothing to confirm) or no relaunch
    authority (stoppable but unrecoverable) means the update cannot
    honour its restart obligation and must not mutate the checkout.
    """
    if plan is None:
        raise QuiesceAbort(
            "refusing to mutate: the pre-update runtime inventory is missing, "
            "so running Hermes runtimes cannot be proven stopped"
        )
    runtimes = list(getattr(plan, "runtimes", None) or [])
    incomplete: list[str] = []
    for runtime in runtimes:
        pid = getattr(runtime, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            incomplete.append(f"{_runtime_label(runtime)}: no live PID captured")
            continue
        if not relaunch_authority(runtime):
            incomplete.append(
                f"{_runtime_label(runtime)}: no supervisor unit/label or launch "
                "argv captured — it could not be relaunched after the update"
            )
    if incomplete:
        raise QuiesceAbort(
            "refusing to mutate: incomplete runtime inventory — "
            + "; ".join(incomplete)
        )
    return runtimes


def wait_for_pid_exit(
    pid: int,
    *,
    pid_alive: Callable[[int], bool],
    timeout: float = DEFAULT_EXIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll until *pid* is gone. ``False`` means it outlived the budget."""
    deadline = monotonic() + max(float(timeout), 0.0)
    while True:
        try:
            if not pid_alive(pid):
                return True
        except Exception as exc:  # a probe failure is not proof of exit
            logger.debug("PID liveness probe failed for %s: %s", pid, exc)
            return False
        if monotonic() >= deadline:
            return False
        sleep(max(float(poll_interval), 0.0))


def run_pre_mutation_quiesce(
    plan: Any,
    *,
    stop_runtime: Callable[[Any], bool],
    pid_alive: Callable[[int], bool],
    assess_isolation: Callable[..., IsolationResult],
    escalate: Optional[Callable[[Any], None]] = None,
    exit_timeout: float = DEFAULT_EXIT_TIMEOUT,
    escalated_exit_timeout: float = DEFAULT_ESCALATED_EXIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    on_event: Optional[Callable[[str], None]] = None,
    expected_sha: str = "",
    persist_state: bool = True,
) -> QuiesceReport:
    """Establish ownership, stop every runtime, and authorize mutation.

    Order is the contract: isolation first (a non-isolated updater must
    not even *start* stopping runtimes, since the first stop can kill
    it), inventory completeness second, stops last.
    """
    global _authorized
    _authorized = None

    isolation = assess_isolation(plan)
    if not getattr(isolation, "isolated", False):
        raise QuiesceAbort(
            "refusing to mutate: the updater is not isolated from the running "
            f"Hermes fleet — {getattr(isolation, 'reason', '') or 'unknown reason'}"
        )

    runtimes = verify_inventory_complete(plan)

    # Persist the relaunch obligation BEFORE the first stop. From the first
    # SIGTERM onward the runtimes describe themselves nowhere else, so an
    # updater killed mid-phase would otherwise leave a fleet that cannot be
    # reconstructed — the exact "interrupted after quiesce" case.
    if persist_state and runtimes:
        write_restart_pending_state(runtimes, expected_sha=expected_sha)

    quiesced: list[int] = []
    for runtime in runtimes:
        pid = int(runtime.pid)
        if on_event:
            on_event(f"stopping {_runtime_label(runtime)}")
        try:
            stopped = bool(stop_runtime(runtime))
        except Exception as exc:
            raise QuiesceAbort(
                f"refusing to mutate: could not stop {_runtime_label(runtime)} "
                f"({exc})"
            ) from exc
        if not stopped:
            raise QuiesceAbort(
                f"refusing to mutate: could not stop {_runtime_label(runtime)}"
            )
        exited = wait_for_pid_exit(
            pid,
            pid_alive=pid_alive,
            timeout=exit_timeout,
            poll_interval=poll_interval,
        )
        if not exited and escalate is not None:
            # A wedged runtime (dead event loop, blocked I/O) must not
            # abort the whole update — escalate, then re-verify. The
            # verification is what stays non-negotiable: we only proceed
            # on a PID that is provably gone.
            if on_event:
                on_event(
                    f"{_runtime_label(runtime)} ignored the graceful stop "
                    "— escalating"
                )
            try:
                escalate(runtime)
            except Exception as exc:
                logger.debug("Escalated stop failed for %s: %s", pid, exc)
            exited = wait_for_pid_exit(
                pid,
                pid_alive=pid_alive,
                timeout=escalated_exit_timeout,
                poll_interval=poll_interval,
            )
        if not exited:
            raise QuiesceAbort(
                f"refusing to mutate: {_runtime_label(runtime)} did not exit "
                "— it would keep importing from the mutated checkout"
            )
        quiesced.append(pid)

    report = QuiesceReport(
        isolation=isolation, quiesced_pids=quiesced, runtimes=list(runtimes)
    )
    _authorized = report
    return report


def _cgroup_conflict(updater_cgroup: str, runtime_cgroup: str) -> bool:
    """True when *updater_cgroup* dies with *runtime_cgroup*.

    Equality is the obvious case; nesting is the subtle one — a transient
    child scope created inside the unit's cgroup is still torn down with
    the unit.
    """
    if not updater_cgroup or not runtime_cgroup:
        return False
    updater = updater_cgroup.rstrip("/")
    runtime = runtime_cgroup.rstrip("/")
    return updater == runtime or updater.startswith(runtime + "/")


def _default_cgroup_of(pid: int) -> Optional[str]:
    """Unified-hierarchy cgroup path for *pid*, or ``None`` off Linux."""
    try:
        from hermes_cli.main import _get_pid_cgroup_path

        return _get_pid_cgroup_path(int(pid))
    except Exception as exc:
        logger.debug("cgroup probe failed for %s: %s", pid, exc)
        return None


def _default_ancestors_of(pid: int) -> list:
    """PIDs of every ancestor of *pid*. Raises when ancestry is unreadable.

    Raising matters: an unreadable ancestry is not proof of independence,
    and :func:`assess_updater_isolation` fails closed on it.
    """
    import psutil

    return [parent.pid for parent in psutil.Process(int(pid)).parents()]


def runtime_cgroup(runtime: Any, cgroup_of: Callable[[int], Optional[str]]) -> str:
    """The runtime's recorded cgroup, falling back to a live probe."""
    detail = getattr(runtime, "detail", None) or {}
    if isinstance(detail, dict):
        recorded = detail.get("cgroup")
        if recorded:
            return str(recorded)
    pid = getattr(runtime, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            return str(cgroup_of(pid) or "")
        except Exception as exc:
            logger.debug("cgroup probe failed for runtime %s: %s", pid, exc)
    return ""


def assess_updater_isolation(
    plan: Any,
    *,
    updater_pid: Optional[int] = None,
    cgroup_of: Callable[[int], Optional[str]] = _default_cgroup_of,
    ancestors_of: Callable[[int], Sequence[int]] = _default_ancestors_of,
) -> IsolationResult:
    """Decide whether the updater survives stopping the whole fleet.

    Two independent ways the updater can be collateral damage:

    * it shares (or nests inside) a runtime's supervisor cgroup — the
      systemd case that plain ``setsid`` does not escape;
    * a runtime is one of its ancestors — the process-tree case that
      matters on macOS/Windows where there is no cgroup to inspect.

    Fail-closed: any probe that cannot answer means NOT isolated.
    """
    import os as _os

    if updater_pid is None:
        updater_pid = _os.getpid()
    runtimes = list(getattr(plan, "runtimes", None) or [])
    if not runtimes:
        return IsolationResult(isolated=True, reason="no running Hermes runtimes")

    try:
        updater_cgroup = cgroup_of(updater_pid)
    except Exception as exc:
        return IsolationResult(
            isolated=False,
            reason=f"could not read the updater's cgroup ({exc})",
        )

    conflicts: list[str] = []
    if updater_cgroup:
        for runtime in runtimes:
            cgroup = runtime_cgroup(runtime, cgroup_of)
            # Only a runtime we will stop THROUGH ITS UNIT can take the
            # updater with it: `systemctl stop <unit>` tears down the
            # whole cgroup. A runtime stopped by PID does not — and it
            # commonly shares a login-session scope (or an outer service
            # cgroup) with the updater, so treating that as a conflict
            # would abort ordinary updates for no reason.
            if not str(getattr(runtime, "unit", "") or ""):
                continue
            if _cgroup_conflict(str(updater_cgroup), cgroup):
                conflicts.append(
                    f"updater shares the supervisor cgroup of "
                    f"{_runtime_label(runtime)} ({cgroup})"
                )

    try:
        ancestors = {int(pid) for pid in ancestors_of(updater_pid)}
    except Exception as exc:
        return IsolationResult(
            isolated=False,
            updater_cgroup=updater_cgroup,
            reason=f"could not read the updater's process ancestry ({exc})",
            conflicts=conflicts,
        )
    # The updater's own PID counts as a conflict too: a plan row claiming
    # it IS a runtime means stopping the fleet stops the updater.
    ancestors.add(int(updater_pid))
    for runtime in runtimes:
        pid = getattr(runtime, "pid", None)
        if isinstance(pid, int) and pid in ancestors:
            conflicts.append(
                f"updater runs inside the process tree of {_runtime_label(runtime)}"
            )

    if conflicts:
        return IsolationResult(
            isolated=False,
            updater_cgroup=updater_cgroup,
            reason="; ".join(conflicts),
            conflicts=conflicts,
        )
    return IsolationResult(
        isolated=True,
        updater_cgroup=updater_cgroup,
        reason="updater owns its own cgroup and process tree",
    )


# ---------------------------------------------------------------------------
# Durable restart-pending state
# ---------------------------------------------------------------------------
#
# Sibling of the existing ``fleet_restart_pending`` breadcrumb in
# ``update_cmd``: that marker records THAT a restart is owed, this record
# says WHICH runtimes are owed one and by what authority. It has to be
# written before the first stop — after it, the processes it describes no
# longer exist, so a retry has nothing left to rediscover.
RESTART_PENDING_STATE_NAME = "fleet_restart_pending.json"


def restart_pending_state_path():
    """HERMES_HOME path of the durable restart-pending record."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / RESTART_PENDING_STATE_NAME


def write_restart_pending_state(runtimes, *, expected_sha: str = "") -> bool:
    """Persist the relaunch obligation atomically. Never raises."""
    import json
    import os as _os

    path = restart_pending_state_path()
    payload = {
        "version": 1,
        "written_at": time.time(),
        "pid": _os.getpid(),
        "expected_sha": expected_sha or "",
        "runtimes": [
            r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in runtimes
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            _os.fsync(handle.fileno())
        _os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.debug("Could not write restart-pending state: %s", exc)
        return False


def read_restart_pending_state() -> Optional[dict]:
    """Load the relaunch obligation, or ``None`` when absent/corrupt."""
    import json

    try:
        path = restart_pending_state_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("Could not read restart-pending state: %s", exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("runtimes"), list):
        return None
    return data


def clear_restart_pending_state() -> None:
    """Drop the relaunch obligation. Never raises."""
    try:
        restart_pending_state_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not clear restart-pending state: %s", exc)


@dataclass
class RelaunchOutcome:
    """What happened to one recorded runtime during the relaunch phase."""

    kind: str = ""
    profile: str = ""
    old_pid: Optional[int] = None
    unit: str = ""
    mechanism: str = ""
    old_pid_gone: bool = False
    relaunched: bool = False
    new_pid: Optional[int] = None
    code_sha: Optional[str] = None
    sha_matches: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _pid_still_alive(pid: int, pid_alive: Callable[[int], bool]) -> bool:
    """Liveness probe that fails CLOSED: unreadable means "assume alive"."""
    try:
        return bool(pid_alive(pid))
    except Exception as exc:
        logger.debug("PID probe failed for %s: %s", pid, exc)
        return True


def relaunch_recorded_runtimes(
    state: dict,
    *,
    restart_unit: Callable[[str, str], bool],
    respawn_argv: Callable[[str, dict], Optional[int]],
    pid_alive: Callable[[int], bool],
    probe_sha: Callable[[dict], Optional[str]],
    on_event: Optional[Callable[[str], None]] = None,
) -> list:
    """Bring every recorded runtime back, by its EXACT recorded authority.

    Each record is acted on exactly once: a recorded unit/label is handed
    verbatim to its supervisor, otherwise the recorded argv is respawned.
    Every old PID is verified gone (a survivor is still importing from the
    mutated checkout) and each replacement is asked for the source SHA it
    is now running.
    """
    expected_sha = str((state or {}).get("expected_sha") or "")
    outcomes: list[RelaunchOutcome] = []
    for record in (state or {}).get("runtimes") or []:
        if not isinstance(record, dict):
            continue
        unit = str(record.get("unit") or "")
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        argv = str((detail or {}).get("argv") or "")
        supervisor = str(record.get("supervisor") or "")
        if unit:
            mechanism = "unit"
        elif argv:
            mechanism = "argv"
        elif supervisor == "desktop":
            # The Desktop app respawns its own backend; there is nothing
            # for the updater to launch, and pretending otherwise would
            # wedge the durable record as permanently undischarged.
            mechanism = "desktop"
        else:
            mechanism = "none"
        outcome = RelaunchOutcome(
            kind=str(record.get("kind") or ""),
            profile=str(record.get("profile") or ""),
            old_pid=record.get("pid") if isinstance(record.get("pid"), int) else None,
            unit=unit,
            mechanism=mechanism,
        )
        if on_event:
            on_event(
                f"relaunching {outcome.kind}[{outcome.profile}] via "
                f"{outcome.mechanism} {unit or argv}"
            )
        try:
            if unit:
                outcome.relaunched = bool(
                    restart_unit(unit, str(record.get("unit_scope") or ""))
                )
            elif argv:
                # A record whose PID is STILL ALIVE was never actually
                # stopped — the abort-then-restore path, where a stop
                # failed partway through the fleet. Respawning it would
                # leave two runtimes on the same profile/port, so restore
                # nothing and report the survivor instead.
                if outcome.old_pid is not None and _pid_still_alive(
                    outcome.old_pid, pid_alive
                ):
                    outcome.error = (
                        "pre-update PID is still running — not respawning a "
                        "duplicate"
                    )
                else:
                    new_pid = respawn_argv(argv, record)
                    outcome.new_pid = new_pid if isinstance(new_pid, int) else None
                    outcome.relaunched = outcome.new_pid is not None
            elif mechanism == "desktop":
                outcome.relaunched = True
            else:
                outcome.error = "no recorded relaunch authority"
        except Exception as exc:
            outcome.error = str(exc)
            outcome.relaunched = False

        if outcome.old_pid is None:
            outcome.old_pid_gone = True
        else:
            outcome.old_pid_gone = not _pid_still_alive(outcome.old_pid, pid_alive)

        try:
            sha = probe_sha(record)
        except Exception as exc:
            logger.debug("SHA probe failed: %s", exc)
            sha = None
        outcome.code_sha = str(sha) if sha else None
        outcome.sha_matches = bool(
            expected_sha and outcome.code_sha == expected_sha
        )
        outcomes.append(outcome)
    return outcomes


def undischarged_records(state: Optional[dict], outcomes: Sequence) -> list:
    """The records of *state* still owed a relaunch after *outcomes*.

    Only the ``argv`` mechanism is discharged on success, and it is
    discharged even when the SHA does not match: a respawn that came up
    IS running, and running it again puts a second process on the same
    profile/port. Every other mechanism is idempotent — ``systemctl
    restart`` on an already-restarted unit is a restart, not a duplicate
    — so those records stay owed and a later pass can re-attempt them.
    """
    done = {
        (
            str(getattr(o, "kind", "") or ""),
            str(getattr(o, "profile", "") or ""),
            getattr(o, "old_pid", None),
            str(getattr(o, "unit", "") or ""),
        )
        for o in outcomes or ()
        if getattr(o, "relaunched", False)
        and getattr(o, "mechanism", "") == "argv"
    }
    remaining = []
    for record in (state or {}).get("runtimes") or []:
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        key = (
            str(record.get("kind") or ""),
            str(record.get("profile") or ""),
            pid if isinstance(pid, int) else None,
            str(record.get("unit") or ""),
        )
        if key not in done:
            remaining.append(record)
    return remaining


def discharge_relaunched_records(state: Optional[dict], outcomes: Sequence) -> None:
    """Shrink the durable record to what is still owed a relaunch.

    The relaunch runs twice by design — once in the update's restart
    phase, once from ``cmd_update``'s command-boundary backstop — so an
    incomplete first pass must not leave a fully-relaunched record for
    the second pass to act on again.
    """
    remaining = undischarged_records(state, outcomes)
    if not remaining:
        clear_restart_pending_state()
        return
    write_restart_pending_state(
        remaining, expected_sha=str((state or {}).get("expected_sha") or "")
    )


def relaunch_is_complete(outcomes: Sequence) -> bool:
    """True only when every recorded runtime came back on the new code."""
    outcomes = list(outcomes or [])
    if not outcomes:
        return True
    return all(
        o.relaunched and o.old_pid_gone and o.sha_matches for o in outcomes
    )


# ---------------------------------------------------------------------------
# Detached updater ownership
# ---------------------------------------------------------------------------
#
# ``setsid`` gives a new session, not a new cgroup. An updater spawned from
# a systemd-supervised gateway therefore stays inside that unit's cgroup,
# and is killed with it — including by the very restart the update
# performs. A transient ``systemd-run --user --scope`` moves the updater
# into a cgroup nothing in the fleet owns.


def updater_scope_unit_name(*, pid: Optional[int] = None, stamp: Optional[float] = None) -> str:
    """A unique, systemd-legal transient scope name for this updater."""
    import os as _os

    if pid is None:
        pid = _os.getpid()
    if stamp is None:
        stamp = time.time()
    return f"hermes-updater-{int(pid)}-{int(float(stamp) * 1000) % 1_000_000_000}"


def systemd_run_user_scope_binary() -> Optional[str]:
    """Path to ``systemd-run`` when a user scope can plausibly be created."""
    import shutil
    import sys as _sys

    if _sys.platform in ("win32", "darwin"):
        return None
    return shutil.which("systemd-run")


def isolated_updater_command(
    base_command: Sequence[str],
    *,
    systemd_run: Optional[str],
    unit_name: str,
) -> list:
    """Wrap *base_command* in its own transient scope when possible.

    Returns the command unchanged when ``systemd-run`` is unavailable —
    the updater's own isolation check is the fail-closed backstop, so a
    host without systemd simply relies on process-tree detachment.
    """
    base = list(base_command)
    if not systemd_run:
        return base
    return [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--unit",
        unit_name,
        "--collect",
        "--",
        *base,
    ]


def isolated_updater_shell_prefix(
    *, systemd_run: Optional[str], unit_name: str
) -> str:
    """Shell-quoted ``systemd-run`` prefix, or ``""`` when unavailable."""
    import shlex

    if not systemd_run:
        return ""
    wrapper = isolated_updater_command(
        [], systemd_run=systemd_run, unit_name=unit_name
    )
    return " ".join(shlex.quote(part) for part in wrapper) + " "
