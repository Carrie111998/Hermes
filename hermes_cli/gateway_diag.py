"""Gateway lifecycle diagnostics (``logs/gateway-exit-diag.log``).

Deliberately stdlib-only and import-cheap: the whole point of this module is
to be importable at the very top of ``hermes_cli.main``'s module body, before
the ~50s of CLI startup (heavy imports + plugin discovery) that precedes
``run_gateway()``. Importing ``hermes_cli.gateway`` there instead would cost
seconds and defeat the purpose, so the writer lives here and both callers
share it.

Two records bracket that startup cost:

``gateway.spawn``
    Written from ``hermes_cli.main``'s module body, right after
    ``_apply_profile_override()`` resolves ``HERMES_HOME`` (the earliest point
    at which the log's location is even knowable). Minimal by necessity —
    nothing but the interpreter and raw argv is known yet.

``gateway.start``
    Written at ``run_gateway()`` entry with the fuller field set, and the
    record double-spawn detection keys on: a PAIR of ``gateway.start``
    records for a single launch is the signature. ``gateway.spawn`` uses a
    distinct tag precisely so it cannot perturb that pairing.

Everything here is best-effort by contract: a diagnostic must never be able
to break the gateway, so all failures are swallowed. Opt out entirely with
``HERMES_GATEWAY_EXIT_DIAG=0``.
"""

from __future__ import annotations

import atexit
import os
import sys
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # a runtime pathlib import would cost the module its cheapness
    from pathlib import Path

DIAG_ENV_VAR = "HERMES_GATEWAY_EXIT_DIAG"
DIAG_LOG_NAME = "gateway-exit-diag.log"

# Records which PID has already written its `gateway.spawn`. Must be an env var
# rather than a module global: under `python -m hermes_cli.main` the module body
# runs once as `__main__` and again the first time anything imports
# `hermes_cli.main` by its real name, and those are two distinct module objects
# with separate globals. Observed live — PID 40772 logged spawn at
# proc_age_s=12.5 and again at 144.9.
#
# That root cause is now fixed: `hermes_cli/main.py` publishes its `__main__`
# module object under `sys.modules["hermes_cli.main"]`, so the body executes
# once per process and a module global would in fact suffice today. This stays
# an env var anyway — it is the strictly stronger guard, and it is the one that
# survives the module ever being executed twice again for some other reason
# (a fresh `-m` alias regression, an explicit `importlib.reload`, a vendored
# copy on a second sys.path entry). The suppression is per-PID, so it costs
# nothing when the body only runs once.
#
# The value is the PID, not a bare "1", because env vars are inherited: a
# gateway spawned as a CHILD of a process that already logged (e.g. by
# `gateway restart`) must still record its own spawn. Comparing PIDs makes the
# suppression strictly per-process.
SPAWN_LOGGED_PID_VAR = "HERMES_GATEWAY_SPAWN_DIAG_PID"

# Stamped by whichever code path launches a gateway (see
# ``gateway_windows._spawn_detached`` and the generated .cmd/.vbs launchers),
# and echoed back into that gateway's own lifecycle records as ``spawn_site``.
# Call-site attribution has to be *carried* rather than inferred: a detached
# gateway outlives its spawner, so by the time anyone reads the log the parent
# is usually gone. An absent value is meaningful too — it means the launcher
# was not one of ours. Lives here, not in ``gateway.restart``, so that
# ``gateway_windows`` can reach it without paying a heavy import.
SPAWN_SITE_ENV = "HERMES_GATEWAY_SPAWN_SITE"

# What ``_spawn_detached`` stamps when its caller names no site. It records
# "one of ours launched this, but it did not say which" — strictly less
# information than the coarse inference in ``_detect_boot_reason``, so
# consumers that classify a boot must treat it as absent rather than report it.
SPAWN_SITE_UNSPECIFIED = "unspecified"


def carried_spawn_site() -> str | None:
    """The launcher's own label for this process, or None if it carries none.

    Reads the stamp rather than inferring from the process tree: a detached
    gateway outlives its spawner, so by the time anything asks, the parent is
    typically gone. Returns None for both "never stamped" and "stamped without
    a name" so a caller can use ``or`` to fall through to inference.
    """
    site = (os.environ.get(SPAWN_SITE_ENV) or "").strip()
    if not site or site == SPAWN_SITE_UNSPECIFIED:
        return None
    return site


def diag_enabled() -> bool:
    """Whether lifecycle records should be written (default: on)."""
    return os.environ.get(DIAG_ENV_VAR, "1") == "1"


def resolve_log_dir() -> Path | None:
    """Resolve the log directory *now*, for a record written later.

    ``get_hermes_home()`` reads ``HERMES_HOME`` on every call, so a writer that
    resolves it lazily writes wherever the env var happens to point when it
    fires — which for an ``atexit`` hook is long after the code that registered
    it stopped running. Capturing the path at registration is what pins a
    record to the home its process actually ran under; see ``write_diag``'s
    ``log_dir`` parameter.

    Returns None if the home can't be resolved, keeping the module's
    never-raise contract.
    """
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "logs"
    except Exception:
        return None


def write_diag(tag: str, *, log_dir: Path | None = None, **extra: object) -> None:
    """Append one JSON lifecycle record to ``logs/gateway-exit-diag.log``.

    ``log_dir`` pins the destination to a directory captured earlier by
    :func:`resolve_log_dir`, instead of resolving ``HERMES_HOME`` at write
    time. Pass it from any writer that can outlive the environment it was set
    up in — an ``atexit`` hook above all, whose fire happens after pytest's
    ``monkeypatch`` has restored the real ``HERMES_HOME`` and would otherwise
    append test records to the live production log (observed 2026-08-11).
    """
    if not diag_enabled():
        return
    try:
        import json
        from datetime import datetime, timezone

        if log_dir is None:
            log_dir = resolve_log_dir()
            if log_dir is None:
                return
        elif not log_dir.parent.exists():
            # The captured home has been deleted (a pytest tmp dir, typically).
            # Recreating it to hold a record nobody will read would just leave
            # litter behind, so drop the record instead.
            return
        log_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            **extra,
        }
        with open(log_dir / DIAG_LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass  # never let the diagnostic itself crash the gateway


def register_exit_hook() -> Callable[[], None]:
    """Register the ``atexit.hook`` record, bound to the home current *now*.

    The hook fires from the interpreter's shutdown path, arbitrarily far from
    the code that registered it — so it is the one writer that must never
    resolve its own destination. Under pytest that gap spans ``monkeypatch``
    teardown, and a late-resolved path appended test records to the real
    ``~/.hermes/profiles/main`` log: exactly the artifact the gateway
    double-spawn investigation reads, stamped with a pytest PID.

    Returns the registered hook so callers can ``atexit.unregister`` it.
    """
    log_dir = resolve_log_dir()

    def _hook() -> None:
        write_diag("atexit.hook", sys_exc=repr(sys.exc_info()), log_dir=log_dir)

    atexit.register(_hook)
    return _hook


def process_start_age_s() -> float | None:
    """Seconds between this process's creation and now, or None if unknown.

    Stamped onto the lifecycle records so each one self-documents how much
    boot latency preceded it — the residual blind spot is then readable from
    the log itself instead of needing a live ``Get-CimInstance Win32_Process``
    correlation after the fact.
    """
    try:
        import time

        import psutil  # type: ignore

        return round(time.time() - psutil.Process(os.getpid()).create_time(), 3)
    except Exception:
        return None


# ── launch attribution ───────────────────────────────────────────────────────
# ``argv`` alone cannot identify a launcher. ``run_gateway()`` reads sys.argv
# after ``_apply_profile_override()`` has stripped the global ``--profile``, so
# a gateway whose real command line is
#     pythonw.exe -m hermes_cli.main --profile main gateway run
# logs only ["...\\hermes_cli\\main.py", "gateway", "run"]. An August 2026
# double-spawn investigation eliminated two candidate launchers on exactly that
# mismatch and never found the real second spawner.
#
# The helpers below add what argv cannot carry: the OS command line, the parent
# chain, and the carried spawn-site stamp. All are best-effort by construction —
# psutil raises AccessDenied for processes we don't own, NoSuchProcess for a
# parent that already exited, and can be absent from a stripped install. They
# degrade to None / [] rather than raise, because a diagnostic must never be the
# reason a gateway fails to start.


def raw_cmdline() -> list[str] | None:
    """The process's real OS command line, or None if it can't be read."""
    try:
        import psutil  # type: ignore

        return list(psutil.Process().cmdline())
    except Exception:
        return None


def parent_pid() -> int | None:
    """The parent PID, or None if the platform won't say."""
    try:
        return os.getppid()
    except Exception:
        return None


def _proc_entry(proc) -> dict[str, object]:
    """Describe one ancestor, per field, so a denied field can't blank the rest.

    Windows routinely allows ``name()`` on a process whose ``cmdline()`` is
    access-denied (services, elevated parents). A name alone still narrows a
    launcher down, so each field is captured independently.
    """
    entry: dict[str, object] = {"pid": None, "name": None, "cmdline": None}
    for field in ("pid", "name", "cmdline"):
        try:
            value = getattr(proc, field)
            entry[field] = value if field == "pid" else value()
        except Exception:
            continue
    if isinstance(entry["cmdline"], (list, tuple)):
        entry["cmdline"] = list(entry["cmdline"])
    return entry


def parent_chain(depth: int = 3) -> list[dict[str, object]]:
    """Walk up to ``depth`` ancestors, nearest first.

    This is the field that names a spawner we never instrumented: a
    ``wscript.exe``/``svchost.exe`` ancestor means Task Scheduler, a
    ``powershell.exe`` one means a startup script, and so on.
    """
    chain: list[dict[str, object]] = []
    try:
        import psutil  # type: ignore

        proc = psutil.Process().parent()
    except Exception:
        return chain

    for _ in range(max(0, depth)):
        if proc is None:
            break
        chain.append(_proc_entry(proc))
        try:
            proc = proc.parent()
        except Exception:
            break
    return chain


def launch_identity() -> dict[str, object]:
    """The launch-attribution fields shared by both lifecycle records.

    Adds no import cost over ``process_start_age_s()``, which already pays for
    psutil on the same code paths. A null ``spawn_site`` is evidence in its own
    right: it means the launch did not come through any of our spawn paths.
    """
    return {
        "raw_cmdline": raw_cmdline(),
        "ppid": parent_pid(),
        "parent_chain": parent_chain(),
        "spawn_site": os.environ.get(SPAWN_SITE_ENV),
    }


def argv_selects_gateway_run(argv: list[str]) -> bool:
    """Whether ``argv`` invokes ``hermes [flags] gateway [run] [flags]``.

    Used to decide whether a process importing ``hermes_cli.main`` is on its
    way to becoming a gateway. Raw-argv matching is unavoidable here: the
    record has to be written long before argparse runs.

    The asymmetry is deliberate. A false negative loses one diagnostic line.
    A false positive writes one spurious ``gateway.spawn`` line — and since
    the tag is distinct from ``gateway.start``, it cannot manufacture the
    double-spawn signature either way. Nothing here affects control flow.
    """
    try:
        args = list(argv[1:])
        gateway_at = args.index("gateway")
    except (ValueError, IndexError, TypeError):
        return False
    for token in args[gateway_at + 1 :]:
        if token.startswith("-"):
            continue  # `gateway -v run`
        return token == "run"
    return True  # bare `gateway` defaults to the run subcommand


def emit_gateway_spawn_diag(argv: list[str]) -> bool:
    """Write the earliest-possible ``gateway.spawn`` record for a gateway run.

    Returns whether a record was written, so the behavior is testable without
    reaching into ``hermes_cli.main``'s module body. ``argv`` is passed in (and
    logged) rather than read from ``sys.argv`` because
    ``_apply_profile_override()`` strips ``--profile`` from ``sys.argv`` before
    any of this runs: logging the pre-strip copy keeps the record honest about
    how the process was actually launched.
    """
    if not diag_enabled() or not argv_selects_gateway_run(argv):
        return False
    pid = str(os.getpid())
    if os.environ.get(SPAWN_LOGGED_PID_VAR) == pid:
        return False  # this process already logged its spawn; see the var's docs
    os.environ[SPAWN_LOGGED_PID_VAR] = pid
    # Attribution belongs here more than anywhere else: this is the earliest
    # record, so it is the one most likely to catch the parent process still
    # alive — a detached spawner returns the moment CreateProcess does, and the
    # ~50s of CLI startup that follows is long enough for it to be gone by the
    # time ``gateway.start`` is written.
    write_diag(
        "gateway.spawn",
        argv=list(argv),
        **launch_identity(),
        proc_age_s=process_start_age_s(),
    )
    return True
