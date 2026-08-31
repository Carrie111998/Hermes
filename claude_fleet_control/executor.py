"""The irreversible boundary of the P6 fleet controller.

One class, one method, one action. The executor receives exactly one fully
revalidated target, revalidates identity ONE more time against a snapshot it
takes itself, and only then calls the injected terminate function (production
wiring: ``gateway.status.terminate_pid(..., force=True, reason=<plan_id>)``,
the box's attributed kill chokepoint — Windows ``taskkill /PID <root> /T /F``
takes the whole tree in one call, so there are never concurrent kill waves).

The controller NEVER constructs this class in shadow or disabled mode; a test
pins that by making construction observable. Any identity mismatch, missing
root, or new-descendant surprise cancels the whole tree — there is no partial
kill and no retry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence, Tuple

from claude_fleet_control.models import (
    ProcessRecord,
    TargetSummary,
    identity_of,
)

SnapshotFn = Callable[[], Sequence[ProcessRecord]]
TerminateFn = Callable[..., None]  # terminate_pid(pid, *, force, reason)


@dataclass(frozen=True)
class ExecutionReport:
    ok: bool
    cancelled: bool
    detail: str
    exited_identities: Tuple[str, ...] = ()
    surviving_identities: Tuple[str, ...] = ()


class WindowsTreeExecutor:
    """Hard-terminate one revalidated session tree, then prove the outcome.

    ``terminate_fn`` and ``snapshot_fn`` are injected so every test path uses
    fakes with negative PIDs — a failed injection cannot address a real
    process. Production wiring happens in the controller's enforce branch and
    nowhere else.
    """

    def __init__(
        self,
        *,
        terminate_fn: TerminateFn,
        snapshot_fn: SnapshotFn,
        sleep_fn: Callable[[float], None] = time.sleep,
        settle_seconds: float = 5.0,
    ) -> None:
        self._terminate = terminate_fn
        self._snapshot = snapshot_fn
        self._sleep = sleep_fn
        self._settle_seconds = settle_seconds

    def _live_identities(self) -> dict:
        return {r.pid: identity_of(r.pid, r.create_time) for r in self._snapshot()}

    def hard_terminate_tree(self, target: TargetSummary, *, plan_id: str) -> ExecutionReport:
        expected = set(target.member_identities)

        # Final revalidation, on our own fresh snapshot: the root must still
        # be the exact (pid, create_time) the plan named, and no member slot
        # may have been recycled into a different process. A NEW descendant
        # (an identity in the tree's pids we did not plan for) also cancels —
        # the tree changed under us, so the plan no longer describes it.
        live = self._live_identities()
        root_live = live.get(target.root_pid)
        if root_live != target.root_identity:
            return ExecutionReport(
                ok=False, cancelled=True,
                detail=f"root identity mismatch or gone: {root_live!r}",
            )
        planned_pids = {int(identity.split(":", 1)[0]) for identity in expected}
        for pid in planned_pids:
            observed = live.get(pid)
            if observed is not None and observed not in expected:
                return ExecutionReport(
                    ok=False, cancelled=True,
                    detail=f"member pid {pid} recycled to {observed}",
                )

        try:
            self._terminate(target.root_pid, force=True, reason=f"claude_fleet:{plan_id}")
        except Exception as exc:
            survivors = tuple(sorted(
                identity for identity in self._live_identities().values()
                if identity in expected
            ))
            return ExecutionReport(
                ok=False, cancelled=False,
                detail=f"terminate failed: {exc}",
                surviving_identities=survivors,
            )

        self._sleep(self._settle_seconds)
        after = set(self._live_identities().values())
        survivors = tuple(sorted(expected & after))
        exited = tuple(sorted(expected - after))
        return ExecutionReport(
            ok=not survivors, cancelled=False,
            detail="tree exited" if not survivors else f"{len(survivors)} member(s) survived",
            exited_identities=exited,
            surviving_identities=survivors,
        )


def build_production_executor(snapshot_fn: SnapshotFn) -> WindowsTreeExecutor:
    """The ONLY place the live kill chokepoint is wired in. Imported lazily so
    that merely importing this module (or running shadow mode) never touches
    gateway code."""
    from gateway.status import terminate_pid

    return WindowsTreeExecutor(terminate_fn=terminate_pid, snapshot_fn=snapshot_fn)
