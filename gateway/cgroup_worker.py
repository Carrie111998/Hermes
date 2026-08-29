"""Place hermes gateway dispatched kanban workers in a lower-weighted child cgroup.

Background (t_4ae8a651 / DGX-HOST-CPU-PRIORITY-2026-08):
  the gateway's asyncio event loop and every dispatched kanban worker share ONE
  systemd unit cgroup. cgroup-v2 ``cpu.weight`` only arbitrates *between sibling
  cgroups*, so a weight applied to the unit hands ~340 worker tasks a bigger
  claim and gives the event loop (which wants <0.1 cores) nothing it lacked —
  measured: raising the unit weight moved ``cpu.pressure`` some avg300 from
  26.03% to 26.28% (no effect), and the loop stayed starved.

This module implements the actual fix: move the gateway's own process into a
``main`` child cgroup and each dispatched worker into a ``workers`` child
cgroup, with ``main`` carrying a higher ``cpu.weight`` than ``workers``. The
kernel then arbitrates *between the two child cgroups*, so under contention the
event loop keeps ~95% of the unit's CPU and the workers are throttled.

Layout (cgroup v2, ``Delegate=yes`` on the unit):
    <unit>.service/            # pure container — no member processes
      main/                    # cpu.weight=200 — the gateway's own process
      workers/                 # cpu.weight=10  — every dispatched worker + its tree

Eligibility & safety:
  * Only activates when the calling process runs inside a systemd ``.service``
    (or ``.slice``) unit — a standalone ``hermes kanban daemon`` in a session
    scope is a no-op. Every step is best-effort (try/except): if any cgroup
    operation fails (not a unit, no permission, kernel constraint), placement
    degrades to a no-op and dispatch is unaffected.
  * The unit must have ``Delegate=yes`` so systemd delegates the subtree (which
    enables ``cpu`` in ``cgroup.subtree_control``) and reaps the nested pids on
    stop. ``gateway.cgroup_cleanup`` is the ExecStopPost safety net and walks
    the whole subtree.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

#: cpu.weight for the gateway's own process (a "main" leaf). Above the unit
#: default of 100 so the event loop wins against its own workers.
MAIN_CPU_WEIGHT = 200
#: cpu.weight for the dispatched-workers leaf. Well below ``main`` so workers
#: absorb the stall instead of the event loop.
WORKER_CPU_WEIGHT = 10

_lock = threading.Lock()
#: Cache of the active workers cgroup path (str) or None once determined.
_workers_path: str | None = None
_workers_checked = False


def _own_cgroup_path() -> str | None:
    """Return the cgroup v2 path for the calling process, or None."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^0::(.+)$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _eligible(cgroup_path: str) -> bool:
    """Only operate inside a real systemd unit, never a session scope.

    A transient ``session-N.scope`` (e.g. ``hermes kanban daemon`` launched
    from a terminal) is excluded so a standalone dispatcher never creates
    child cgroups in a scope systemd does not expect them in. The bare
    container dirs (``app.slice``, ``system.slice``, ``user.slice``) are not
    units and are rejected too — only a concrete ``<name>.service`` /
    ``<name>.slice`` unit qualifies.
    """
    base = cgroup_path.rstrip("/").rsplit("/", 1)[-1]
    if not (base.endswith(".service") or base.endswith(".slice")):
        return False
    if base in ("app.slice", "system.slice", "user.slice"):
        return False
    return True


def _write(path: Path, value: str) -> bool:
    try:
        path.write_text(value, encoding="utf-8")
        return True
    except OSError:
        return False


def _read_pids(cgroup_dir: Path) -> list[int]:
    """Read the integer pids from a cgroup's ``cgroup.procs``, or []."""
    procs_file = cgroup_dir / "cgroup.procs"
    try:
        raw = procs_file.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(int(line))
            except ValueError:
                continue
    return out


def _setup() -> str | None:
    """Create the main/ + workers/ layout, move self into main/, set weights.

    Idempotent. Returns the workers cgroup path (str) on success, else None.
    """
    global _workers_path, _workers_checked
    with _lock:
        if _workers_checked:
            return _workers_path
        _workers_checked = True

        own = _own_cgroup_path()
        if not own or not _eligible(own):
            return None
        root = Path(f"/sys/fs/cgroup{own}")
        try:
            if not root.is_dir():
                return None
            main_dir = root / "main"
            workers_dir = root / "workers"
            main_dir.mkdir(exist_ok=True)
            workers_dir.mkdir(exist_ok=True)
        except OSError:
            return None

        # Drain the unit cgroup: move EVERY current member (our own process and
        # any subprocesses already forked — MCP servers, etc.) into main/ so the
        # unit root becomes a pure container. This must happen before enabling
        # controllers in subtree_control: a writer cannot enable a controller in
        # its own cgroup while that cgroup still has member processes (EBUSY -
        # "no internal process"), and the weights on the children only arbitrate
        # once the root is clean. It also keeps the gateway's live helpers on
        # the high-weight side where they belong.
        #
        # Each pid is written in its OWN write: a cgroup.procs batch write is
        # rejected by the kernel (EBUSY) when it includes the writing process's
        # own pid, so a single multi-pid write would move nothing and leave the
        # root polluted.
        for pid in _read_pids(root):
            _write(main_dir / "cgroup.procs", f"{pid}\n")

        # Enable cpu (and memory/pids) for child cgroups so their weights and
        # limits are honored. Requires Delegate=yes on the unit.
        _write(root / "cgroup.subtree_control", "+cpu +memory +pids\n")

        # Set the sibling weights that actually protect the event loop.
        _write(main_dir / "cpu.weight", f"{MAIN_CPU_WEIGHT}\n")
        _write(workers_dir / "cpu.weight", f"{WORKER_CPU_WEIGHT}\n")

        workers_path = str(workers_dir)
        _workers_path = workers_path
        return workers_path


def place_worker_in_child_cgroup(pid: int) -> bool:
    """Move a dispatched worker pid into the ``workers`` child cgroup.

    No-op (returns False) when the layout is unavailable or ineligible, so a
    failure here can never break dispatch. Returns True when the pid was
    moved.
    """
    workers = _setup()
    if not workers:
        return False
    return _write(Path(workers) / "cgroup.procs", f"{int(pid)}\n")
