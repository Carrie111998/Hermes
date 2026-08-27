"""Process discovery for hermes-agent long-lived processes (deploy discipline).

Enumerates every long-lived process running hermes-agent code so deploy
tooling can answer "is the running system actually on this commit?":

* ``hermes_cli.main gateway``  — the messaging gateway (gateway/run.py)
* ``hermes_cli.main serve``    — the desktop-app backend (one per profile,
  spawned and reaped by the Electron app)
* ``hermes_cli.main dashboard``/``ai.hermes.dashboard`` — the web dashboard

For each process we report pid, process kind, start time, resolved module
paths, and — when the process was spawned by a fixed build — the HEAD it
started on (``HERMES_AGENT_HEAD`` env, stamped at spawn time by the three
spawn sites).  Callers (``hermes gateway restart --all``, ``hermes doctor
deploy``) compare that against the current install HEAD to flag stale
processes.

Design notes:

* psutil is imported lazily and defensively: every public function degrades
  gracefully (empty list / ``None`` fields) when psutil is missing or a scan
  fails, mirroring the fail-safe posture of ``hermes_cli.process_identity``.
* Process-kind classification is cmdline-based (same explicit patterns as
  ``hermes_cli.dashboard_procs``) plus the positive ``HERMES_SPAWN`` tag when
  present; a process is only ever classified once, gateway first.
* ``HERMES_AGENT_HEAD`` is read from the *target process's* environment, not
  ours — that is the whole point: it records the HEAD the process actually
  started with, which may differ from the current install HEAD.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

#: Process kinds this helper understands (stable identifiers used by callers).
KIND_GATEWAY = "gateway"
KIND_SERVE = "serve"
KIND_DASHBOARD = "dashboard"

#: All kinds, in classification priority order (gateway first).
ALL_KINDS = (KIND_GATEWAY, KIND_SERVE, KIND_DASHBOARD)

#: Cmdline patterns that positively identify each kind.  Kept in sync with
#: ``hermes_cli.dashboard_procs._scan_dashboard_processes`` and the gateway
#: process scan in ``hermes_cli.gateway``.
_CMDLINE_PATTERNS: dict[str, tuple[str, ...]] = {
    KIND_GATEWAY: (
        "hermes_cli.main gateway",
        "hermes_cli/main.py gateway",
        "gateway/run.py",
    ),
    KIND_SERVE: (
        "hermes_cli.main serve",
        "hermes_cli/main.py serve",
        "hermes serve",
    ),
    KIND_DASHBOARD: (
        "ai.hermes.dashboard",
        "hermes_cli.main dashboard",
        "hermes_cli/main.py dashboard",
        "hermes dashboard",
    ),
}

#: Env var stamped at spawn time with the install HEAD (see spawn sites in
#: gateway/run.py, hermes_cli/web_server.py, apps/desktop/electron/main.ts).
HEAD_ENV_VAR = "HERMES_AGENT_HEAD"

#: Env var set by the Electron app on its own backend children; used to
#: distinguish desktop-supervised backends (which must be SIGTERM'd, never
#: kill -9'd, so the supervisor respawns them).
DESKTOP_CHILD_ENV_VAR = "HERMES_DESKTOP_CHILD_PID"


@dataclass
class AgentProcess:
    """One discovered long-lived hermes-agent process."""

    pid: int
    kind: str
    start_time: Optional[float] = None
    cmdline: str = ""
    module_paths: list[str] = field(default_factory=list)
    head_at_start: Optional[str] = None
    desktop_supervised: bool = False

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "kind": self.kind,
            "start_time": self.start_time,
            "cmdline": self.cmdline,
            "module_paths": list(self.module_paths),
            "head_at_start": self.head_at_start,
            "desktop_supervised": self.desktop_supervised,
        }


def _classify_kind(cmdline: str, spawn_tag_purpose: Optional[str]) -> Optional[str]:
    """Classify a process cmdline into a kind, or ``None`` if not ours.

    The positive ``HERMES_SPAWN`` tag (when readable) wins; otherwise fall
    back to explicit cmdline patterns.  Gateway is checked first so a
    ``gateway restart`` management argv (which contains "gateway") never
    misclassifies as serve/dashboard.

    The ``-m hermes_cli.main`` launch form is matched *token-wise*, so a
    profile flag injected between the module and the subcommand (e.g.
    ``-m hermes_cli.main -p default serve``) still classifies correctly —
    a naive contiguous-substring match would miss it.
    """
    if spawn_tag_purpose in ALL_KINDS:
        return spawn_tag_purpose

    lowered = cmdline.lower()
    tokens = lowered.split()

    # 1) "python -m hermes_cli.main gateway|serve|dashboard" — first kind token
    #    after the module, skipping option flags AND their values (e.g.
    #    "-p default", "--port 0", "--host 127.0.0.1") that may precede the
    #    subcommand.
    for mod in ("hermes_cli.main", "hermes_cli/main.py"):
        try:
            idx = tokens.index(mod)
        except ValueError:
            continue
        skip_value = False
        for j in range(idx + 1, len(tokens)):
            tok = tokens[j]
            if skip_value:
                skip_value = False
                continue
            if tok in ALL_KINDS:
                return tok
            if tok.startswith("-"):
                # Option flag. If the next token is a bare value (not a flag),
                # it is this flag's value — skip it too.
                nxt = tokens[j + 1] if j + 1 < len(tokens) else None
                skip_value = bool(nxt and not nxt.startswith("-"))
                continue
            # A non-flag, non-kind token after the module means it's not one
            # of our long-lived kinds (e.g. "chat"); stop scanning.
            break

    # 2) "hermes serve" / "hermes dashboard" convenience invocations.
    if "hermes serve" in lowered or lowered.startswith("hermes serve"):
        return KIND_SERVE
    if "hermes dashboard" in lowered:
        return KIND_DASHBOARD

    # 3) Explicit standalone kinds: gateway runner and dashboard service.
    if "gateway/run.py" in lowered:
        return KIND_GATEWAY
    if "ai.hermes.dashboard" in lowered:
        return KIND_DASHBOARD

    return None


def _read_process_env(proc) -> Optional[dict]:
    """Best-effort read of a process environment (psutil may deny it)."""
    try:
        return proc.environ()
    except Exception:
        return None


def _read_module_paths(proc) -> list[str]:
    """Best-effort resolved module paths for a process (may be empty)."""
    try:
        return [p for p in proc.cmdline() if p]
    except Exception:
        return []


def _iter_psutil_processes():
    """Yield psutil Process objects; empty on any failure."""
    try:
        import psutil
    except Exception:
        return
    try:
        yield from psutil.process_iter(["pid", "name", "cmdline"])
    except Exception:
        return


def discover_processes(
    *,
    include_self: bool = False,
    kinds: Optional[tuple[str, ...]] = None,
) -> list[AgentProcess]:
    """Enumerate running hermes-agent long-lived processes.

    Args:
        include_self: include the calling process even if it matches (the
            restart command itself runs ``hermes_cli.main gateway restart``,
            which would otherwise be classified as a gateway).
        kinds: restrict to these kinds (default: all three).

    Returns:
        List of :class:`AgentProcess`, empty on any scan failure.  Never
        raises.
    """
    wanted = tuple(kinds) if kinds is not None else ALL_KINDS
    self_pid = os.getpid()
    found: list[AgentProcess] = []
    seen: set[int] = set()

    for proc in _iter_psutil_processes():
        try:
            pid = int(proc.info.get("pid") or proc.pid)
        except Exception:
            continue
        if pid in seen:
            continue
        if pid == self_pid and not include_self:
            continue

        cmdline = ""
        try:
            raw_cmdline = proc.info.get("cmdline") or proc.cmdline()
            cmdline = " ".join(str(part) for part in raw_cmdline)
        except Exception:
            pass

        env = _read_process_env(proc)
        spawn_purpose = None
        if env:
            try:
                from hermes_cli.process_identity import parse_spawn_tag

                spawn_purpose = parse_spawn_tag(env.get("HERMES_SPAWN")).purpose
            except Exception:
                spawn_purpose = None

        kind = _classify_kind(cmdline, spawn_purpose)
        if kind is None or kind not in wanted:
            continue

        start_time = None
        try:
            start_time = float(proc.create_time())
        except Exception:
            pass

        head_at_start = None
        desktop_supervised = False
        if env:
            head_at_start = env.get(HEAD_ENV_VAR) or None
            desktop_supervised = bool(env.get(DESKTOP_CHILD_ENV_VAR))

        found.append(
            AgentProcess(
                pid=pid,
                kind=kind,
                start_time=start_time,
                cmdline=cmdline[:500],
                module_paths=_read_module_paths(proc),
                head_at_start=head_at_start,
                desktop_supervised=desktop_supervised,
            )
        )
        seen.add(pid)

    found.sort(key=lambda p: (p.kind, p.pid))
    return found


def current_install_head(project_root: Optional[str] = None) -> Optional[str]:
    """Resolve the current install HEAD via ``git rev-parse HEAD``.

    Returns ``None`` when the repo is missing or git fails (callers treat
    that as "cannot verify", never as "clean").
    """
    import subprocess

    root = project_root or os.environ.get("HERMES_AGENT_INSTALL")
    if not root:
        try:
            from pathlib import Path as _Path

            root = str(_Path(__file__).resolve().parents[1])
        except Exception:
            root = None
    if not root:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def stale_processes(
    processes: list[AgentProcess],
    current_head: Optional[str],
) -> list[AgentProcess]:
    """Return processes whose HEAD-at-start differs from *current_head*.

    A process with no readable ``head_at_start`` is NOT flagged stale here —
    it simply cannot be verified (the caller decides how to treat that).
    """
    if not current_head:
        return []
    return [p for p in processes if p.head_at_start is not None and p.head_at_start != current_head]


def format_process_table(processes: list[AgentProcess]) -> str:
    """Render a human-readable table of discovered processes."""
    if not processes:
        return "No hermes-agent long-lived processes found."
    header = f"{'PID':>7}  {'KIND':<10} {'STARTED':<20} HEAD"
    lines = [header, "-" * len(header)]
    for p in processes:
        started = ""
        if p.start_time is not None:
            try:
                import datetime

                started = datetime.datetime.fromtimestamp(
                    p.start_time, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                started = f"{p.start_time:.0f}"
        head = p.head_at_start or "(unknown)"
        lines.append(f"{p.pid:>7}  {p.kind:<10} {started:<20} {head}")
    return "\n".join(lines)