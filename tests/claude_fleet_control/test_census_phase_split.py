"""Phase-1 census split: one ppid snapshot, create_time only for the closure.

WHY THIS FILE EXISTS. On 2026-09-01 the fleet controller's pass went from a
steady 15s to past its PT4M30S ExecutionTimeLimit and was killed by Task
Scheduler (event 329) every cycle -- so fleet growth disabled the guard that
exists to contain fleet growth, and its silence watchdog was killed with it.

Cause: phase 1 asked psutil for ``ppid`` (and ``create_time``) across the
whole process table. ppid looks as cheap as pid or name and is not: psutil's
Windows backend takes a whole-system Toolhelp snapshot per call, so a
whole-table ppid census is quadratic. First-enumeration cost in a FRESH
interpreter -- the only honest comparison, since the scheduled task is always
a fresh interpreter:

    process_iter(pid)                   0.14s
    process_iter(pid, name)             0.20s
    process_iter(pid, ppid, name)      56.48s
    _psutil_windows.ppid_map()          0.06s   <- same map, one call

A first attempt at this fix blamed ``create_time`` instead, on an A/B run
INSIDE one interpreter where the create_time variant happened to run first
and absorbed the whole cold-start cost. These tests therefore pin the
mechanism explicitly (phase 1 must not ask process_iter for ppid) rather
than pinning a wall-clock number, which is exactly the measurement that
misled. Loops claim tray-329-kills-fleet-controller-20260901.
"""

from claude_fleet_control import controller, planner
from tests.claude_fleet_control.conftest import NOW, cli_rec, rec


# ------------------------------------------------- the closure is a superset

def test_closure_covers_seed_descendants_and_ancestors():
    # desktop -> cli root -> shell -> grandchild, plus an unrelated tree.
    parent = rec(-1, name="Claude.exe")
    root = cli_rec(-2, ppid=-1)
    child = rec(-3, ppid=-2, name="bash.exe")
    grandchild = rec(-4, ppid=-3, name="python.exe")
    stranger = rec(-90, name="chrome.exe")
    stranger_kid = rec(-91, ppid=-90, name="chrome.exe")
    records = [parent, root, child, grandchild, stranger, stranger_kid]

    closure = planner.census_ctime_pids(records)
    assert {-1, -2, -3, -4} <= closure
    assert -90 not in closure and -91 not in closure


def test_closure_is_a_superset_of_enrichment_pids():
    # THE load-bearing invariant. Every pid phase 2 enriches has its
    # create_time compared against a freshly read one, so every one of them
    # must have had a real create_time fetched in phase 1b.
    root = cli_rec(-2)
    child = rec(-3, ppid=-2, name="node.exe")
    other = rec(-70, name="svchost.exe")
    records = [root, child, other]
    assert planner.enrichment_pids(records) <= planner.census_ctime_pids(records)


def test_closure_survives_a_ppid_cycle():
    # A recycled ppid can make the ancestor walk circular; it must terminate.
    a = cli_rec(-2, ppid=-3)
    b = rec(-3, ppid=-2, name="bash.exe")
    assert planner.census_ctime_pids([a, b]) >= {-2, -3}


def test_closure_ignores_validation_that_collect_tree_would_apply():
    # The closure is deliberately UNVALIDATED. A child that predates its
    # parent is dropped by collect_tree, but must still be IN the closure --
    # otherwise it would have no create_time and the guard could not judge it.
    root = cli_rec(-2, create_time=NOW - 100.0)
    recycled = rec(-3, ppid=-2, name="bash.exe", create_time=NOW - 9999.0)
    records = [root, recycled]
    assert -3 in planner.census_ctime_pids(records)
    assert -3 not in {m.pid for m in planner.collect_tree(root, records)}


# ------------------------------------------------------- live_snapshot wiring

class _FakeProc:
    def __init__(self, pid, ctime, boom=False):
        self.pid = pid
        self._ctime = ctime
        self._boom = boom

    def create_time(self):
        if self._boom:
            raise OSError("access denied")
        return self._ctime

    def oneshot(self):
        class _Ctx:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *a):
                return False

        return _Ctx()

    def memory_info(self):
        class _M:
            rss = 1234

        return _M()

    def exe(self):
        return "C:\\claude.exe"

    def cmdline(self):
        return ["claude.exe", "--resume", "x"]

    def username(self):
        return "BOX\\diego"


def _install_fake_psutil(monkeypatch, table, boom_pids=(), ppid_boom_pids=()):
    """table: list of (pid, ppid, name, create_time)."""

    ppids = {pid: pp for pid, pp, _, _ in table}

    class _Iter:
        def __init__(self, pid, ppid, name):
            self.info = {"pid": pid, "ppid": ppid, "name": name}

        def ppid(self):              # per-process fallback path
            pid = self.info["pid"]
            if pid in ppid_boom_pids:
                raise OSError("access denied")
            return ppids[pid]

    ctimes = {pid: ct for pid, _, _, ct in table}
    asked = []

    class _FakePsutil:
        @staticmethod
        def process_iter(fields):
            asked.append(tuple(fields))
            return [_Iter(p, pp, n) for p, pp, n, _ in table]

        @staticmethod
        def Process(pid):
            return _FakeProc(pid, ctimes[pid], boom=(pid in boom_pids))

    import sys
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    # Default to the fast path; individual tests override _live_ppid_map.
    monkeypatch.setattr(controller, "_live_ppid_map", lambda: dict(ppids))
    return asked


def test_phase1_never_asks_process_iter_for_ppid_or_create_time(monkeypatch):
    # THE mechanism assert. Both are per-handle fields on Windows and neither
    # may appear in the whole-table call; ppid is the ~800x one.
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0),
             (-70, None, "svchost.exe", NOW - 900.0)]
    asked = _install_fake_psutil(monkeypatch, table)
    controller.live_snapshot()
    assert asked, "process_iter was never called"
    for fields in asked:
        assert "ppid" not in fields, fields
        assert "create_time" not in fields, fields


def test_ppid_comes_from_the_snapshot_map(monkeypatch):
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0)]
    _install_fake_psutil(monkeypatch, table)
    calls = []

    def _map():
        calls.append(1)
        return {-2: None, -3: -2}

    monkeypatch.setattr(controller, "_live_ppid_map", _map)
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-3].ppid == -2
    # ONE call for the whole table is the entire point of the fix.
    assert len(calls) == 1


def test_pid_born_after_the_map_is_asked_directly_not_marked_incomplete(monkeypatch):
    # A process born between the map call and the enumeration must be asked
    # directly. Marking it incomplete instead would protect its whole tree
    # (planner default-deny), so on a churning box the enforce lane would
    # drift toward never acting -- a behaviour change wearing a fail-safe's
    # clothes. Measured: this race fires on a busy box most passes.
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0)]
    _install_fake_psutil(monkeypatch, table)
    monkeypatch.setattr(controller, "_live_ppid_map", lambda: {-2: None})  # -3 missing
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-3].ppid == -2
    assert by_pid[-3].complete is True
    assert snap.complete is True


def test_pid_whose_ppid_cannot_be_read_at_all_is_incomplete(monkeypatch):
    # The genuine failure still fails closed.
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0)]
    _install_fake_psutil(monkeypatch, table, ppid_boom_pids={-3})
    monkeypatch.setattr(controller, "_live_ppid_map", lambda: {-2: None})
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-3].ppid is None
    assert by_pid[-3].complete is False
    assert snap.complete is False


def test_unavailable_ppid_map_falls_back_to_per_process(monkeypatch):
    # A psutil upgrade that moves or drops the private helper must degrade to
    # the slow-but-correct path, never raise and never drop ancestry.
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0)]
    _install_fake_psutil(monkeypatch, table)
    monkeypatch.setattr(controller, "_live_ppid_map", lambda: None)
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-3].ppid == -2
    assert by_pid[-3].complete is True


def _patch_ppid_map_source(monkeypatch, fn):
    """Point whichever private helper _live_ppid_map resolves at ``fn``.

    ``from psutil import _psutil_windows`` reads the ATTRIBUTE on the psutil
    package, so injecting into sys.modules does not take -- a first version of
    this test did that and silently measured the real box instead.
    """
    import psutil
    for mod in ("_psutil_windows", "_psplatform"):
        target = getattr(psutil, mod, None)
        if target is not None and hasattr(target, "ppid_map"):
            monkeypatch.setattr(target, "ppid_map", fn)
            return True
    return False


def test_empty_ppid_map_is_treated_as_unavailable(monkeypatch):
    # A real box always has processes, so {} means the call did not work.
    # Believing it would hand the planner a census with no ancestry at all.
    if not _patch_ppid_map_source(monkeypatch, lambda: {}):
        import pytest
        pytest.skip("no private ppid_map on this psutil build")
    assert controller._live_ppid_map() is None


def test_raising_ppid_map_is_treated_as_unavailable(monkeypatch):
    def _boom():
        raise OSError("gone in a psutil upgrade")

    if not _patch_ppid_map_source(monkeypatch, _boom):
        import pytest
        pytest.skip("no private ppid_map on this psutil build")
    assert controller._live_ppid_map() is None


def test_live_ppid_map_reads_the_real_box():
    # Positive control for the two negatives above: without this, a rename of
    # the private helper would leave both of them passing on the fallback and
    # nothing would notice the fast path was gone.
    m = controller._live_ppid_map()
    assert m is not None and len(m) > 10
    assert all(isinstance(k, int) for k in list(m)[:20])


def test_closure_pids_get_a_real_create_time_and_strangers_do_not(monkeypatch):
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0),
             (-70, None, "svchost.exe", NOW - 900.0)]
    _install_fake_psutil(monkeypatch, table)
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-2].create_time == NOW - 50.0
    assert by_pid[-3].create_time == NOW - 40.0
    # Outside the closure: never a tree member or ancestor, so the field is
    # never read -- it stays 0.0 and the record stays complete.
    assert by_pid[-70].create_time == 0.0
    assert by_pid[-70].complete is True


def test_unreadable_create_time_marks_incomplete_not_zero(monkeypatch):
    # FAIL-SAFE. A silent 0.0 would read as "older than everything" to
    # collect_tree's recycled-ppid guard, which decides tree membership --
    # i.e. it would silently change what the executor may terminate.
    table = [(-2, None, "claude.exe", NOW - 50.0), (-3, -2, "bash.exe", NOW - 40.0)]
    _install_fake_psutil(monkeypatch, table, boom_pids={-3})
    snap = controller.live_snapshot()
    by_pid = {r.pid: r for r in snap.records}
    assert by_pid[-3].complete is False
    assert snap.complete is False
    # ...and the failure is ISOLATED: its readable sibling in the same closure
    # still carries a real create_time. Without this line the test passes
    # vacuously against the pre-split code, which set every create_time to 0.0
    # and every record incomplete under this fake -- proving nothing.
    assert by_pid[-2].create_time == NOW - 50.0
    assert by_pid[-2].complete is True
