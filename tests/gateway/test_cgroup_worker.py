"""Tests for gateway.cgroup_worker (child-cgroup isolation of dispatched workers)."""

from __future__ import annotations

import pytest

from gateway import cgroup_worker

SERVICE_CG = "/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway-jarvis.service"
SCOPE_CG = "/user.slice/user-1000.slice/user@1000.service/app.slice/session-9.scope"


class FakePath:
    """Tiny Path shim that records fs writes against a virtual cgroup tree."""

    def __init__(self, p: str, fs: dict):
        self.p = str(p)
        self._fs = fs

    def __str__(self):
        return self.p

    def __fspath__(self):
        return self.p

    def __truediv__(self, key):
        return FakePath(f"{self.p}/{key}", self._fs)

    def is_dir(self):
        return True

    def mkdir(self, *a, **kw):
        self._fs.setdefault("mkdirs", set()).add(self.p)

    def write_text(self, data, *a, **kw):
        self._fs["writes"][self.p] = data
        self._fs.setdefault("writes_log", []).append((self.p, data))

    def read_text(self, *a, **kw):
        return self._fs["reads"].get(self.p, "")


def _install_fake_fs(monkeypatch):
    fs = {"mkdirs": set(), "writes": {}, "reads": {}}
    monkeypatch.setattr(cgroup_worker, "Path", lambda p: FakePath(str(p), fs))
    monkeypatch.setattr(cgroup_worker.os, "getpid", lambda: 100)
    return fs


class TestEligibility:
    @pytest.mark.parametrize(
        "path,expected",
        [
            (SERVICE_CG, True),
            ("/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-cron-heavy.slice", True),
            (SCOPE_CG, False),
            ("/user.slice/user-1000.slice/user@1000.service/app.slice", False),
        ],
    )
    def test_eligible_only_for_systemd_units(self, path, expected):
        assert cgroup_worker._eligible(path) is expected


class TestSetupIneligibleNoop:
    def test_setup_returns_none_in_session_scope(self, monkeypatch):
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: SCOPE_CG)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None
        assert cgroup_worker._setup() is None

    def test_setup_returns_none_when_no_cgroup(self, monkeypatch):
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: None)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None
        assert cgroup_worker._setup() is None


class TestSetupBuildsLayout:
    def test_creates_children_drains_root_sets_weights(self, monkeypatch):
        fs = _install_fake_fs(monkeypatch)
        # seed the unit root with our own pid + a pre-existing helper subprocess
        root_procs = f"/sys/fs/cgroup{SERVICE_CG}/cgroup.procs"
        fs["reads"][root_procs] = "100\n105\n"  # self (100) + an MCP helper (105)
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: SERVICE_CG)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None

        workers_path = cgroup_worker._setup()
        assert workers_path == f"/sys/fs/cgroup{SERVICE_CG}/workers"
        assert f"/sys/fs/cgroup{SERVICE_CG}/main" in fs["mkdirs"]
        assert f"/sys/fs/cgroup{SERVICE_CG}/workers" in fs["mkdirs"]

        writes = fs["writes"]
        # ALL current members drained into main/ (not just self), so the unit
        # root becomes a pure container before controllers are enabled. Each
        # pid gets its own cgroup.procs write (a batch write including the
        # writer's own pid is rejected by the kernel).
        procs_writes = [d for p, d in fs["writes_log"] if p.endswith("/main/cgroup.procs")]
        assert [x.strip() for x in procs_writes] == ["100", "105"], procs_writes
        # controllers enabled for the child subtree
        assert "+cpu +memory +pids\n" in writes.get(f"/sys/fs/cgroup{SERVICE_CG}/cgroup.subtree_control", "")
        # weights
        assert writes[f"/sys/fs/cgroup{SERVICE_CG}/main/cpu.weight"].strip() == str(cgroup_worker.MAIN_CPU_WEIGHT)
        assert writes[f"/sys/fs/cgroup{SERVICE_CG}/workers/cpu.weight"].strip() == str(cgroup_worker.WORKER_CPU_WEIGHT)

    def test_setup_is_cached_and_idempotent(self, monkeypatch):
        fs = _install_fake_fs(monkeypatch)
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: SERVICE_CG)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None

        first = cgroup_worker._setup()
        second = cgroup_worker._setup()
        assert first == second
        # cache short-circuits a second full layout pass
        mkdir_count = len(fs["mkdirs"])
        assert second == first
        assert len(fs["mkdirs"]) == mkdir_count


class TestReadPids:
    def test_parses_integer_lines(self, monkeypatch):
        fs = _install_fake_fs(monkeypatch)
        fs["reads"][f"/sys/fs/cgroup{SERVICE_CG}/cgroup.procs"] = "100\n105\n\n"
        got = cgroup_worker._read_pids(cgroup_worker.Path(f"/sys/fs/cgroup{SERVICE_CG}"))
        assert got == [100, 105]

    def test_empty_and_unreadable(self, monkeypatch):
        fs = _install_fake_fs(monkeypatch)
        fs["reads"][f"/sys/fs/cgroup{SERVICE_CG}/cgroup.procs"] = ""
        got = cgroup_worker._read_pids(cgroup_worker.Path(f"/sys/fs/cgroup{SERVICE_CG}"))
        assert got == []


class TestPlaceWorker:
    def test_place_writes_pid_when_active(self, monkeypatch):
        fs = _install_fake_fs(monkeypatch)
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: SERVICE_CG)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None

        assert cgroup_worker.place_worker_in_child_cgroup(4242) is True
        wpath = f"/sys/fs/cgroup{SERVICE_CG}/workers/cgroup.procs"
        assert fs["writes"].get(wpath, "").strip() == "4242"

    def test_place_noop_when_ineligible(self, monkeypatch):
        monkeypatch.setattr(cgroup_worker, "_own_cgroup_path", lambda: SCOPE_CG)
        cgroup_worker._workers_checked = False
        cgroup_worker._workers_path = None
        assert cgroup_worker.place_worker_in_child_cgroup(4242) is False
