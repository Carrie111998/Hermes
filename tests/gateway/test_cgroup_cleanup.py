"""Tests for the systemd ExecStopPost cgroup reaper (issue #37454)."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from gateway import cgroup_cleanup


class TestOwnCgroupPath:
    def test_parses_v2_cgroup_path(self, tmp_path, monkeypatch):
        proc_self = tmp_path / "cgroup"
        proc_self.write_text("0::/user.slice/user-1000.slice/hermes-gateway.service\n")
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: proc_self if p == "/proc/self/cgroup" else Path(p),
        )

        assert cgroup_cleanup._own_cgroup_path() == "/user.slice/user-1000.slice/hermes-gateway.service"


class TestReapCgroup:


    def test_noop_when_procs_file_missing(self, tmp_path, monkeypatch):
        cgroup_path = "/missing.slice/hermes-gateway.service"
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: tmp_path / "does-not-exist" if "cgroup.procs" in p else Path(p),
        )

        def _explode(*_a, **_kw):
            pytest.fail("os.kill must not be called when cgroup.procs is unreadable")

        monkeypatch.setattr(cgroup_cleanup.os, "kill", _explode)
        assert cgroup_cleanup.reap_cgroup(cgroup_path) == 0


class TestReapCgroupRecursion:
    """Reaping must walk the whole cgroup subtree (issue #37454 follow-up).

    cgroup_worker nests dispatched workers under <unit>.service/workers/, so
    their pids are NOT in the unit cgroup's own cgroup.procs. The reaper must
    descend into every child cgroup or those workers are orphaned on stop.
    """

    def _walk(self, cgroup_path):
        # top-level cgroup + nested children, mirroring main/ + workers/
        yield cgroup_path
        yield f"{cgroup_path}/main"
        yield f"{cgroup_path}/workers"
        yield f"{cgroup_path}/workers/child"  # worker-spawned subprocess tree

    def test_reaps_nested_worker_pids(self, monkeypatch):
        cgroup_path = "/user.slice/hermes-gateway-jarvis.service"
        procs = {
            cgroup_path: [1001],
            f"{cgroup_path}/main": [2001],
            f"{cgroup_path}/workers": [3001, 3002],
            f"{cgroup_path}/workers/child": [3003],
        }
        killed = []

        monkeypatch.setattr(cgroup_cleanup, "iter_cgroup_tree", self._walk)
        monkeypatch.setattr(
            cgroup_cleanup,
            "_read_cgroup_pids",
            lambda p: procs.get(p, []),
        )
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 999999)
        monkeypatch.setattr(
            cgroup_cleanup.os,
            "kill",
            lambda pid, sig: killed.append(pid),
        )

        count = cgroup_cleanup.reap_cgroup(cgroup_path)
        assert count == 5
        assert sorted(killed) == [1001, 2001, 3001, 3002, 3003]

    def test_skips_caller_own_pid(self, monkeypatch):
        cgroup_path = "/user.slice/hermes-gateway-jarvis.service"
        procs = {cgroup_path: [777, 888]}
        killed = []

        monkeypatch.setattr(cgroup_cleanup, "iter_cgroup_tree", lambda p: iter([p]))
        monkeypatch.setattr(
            cgroup_cleanup,
            "_read_cgroup_pids",
            lambda p: procs.get(p, []),
        )
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 777)
        monkeypatch.setattr(
            cgroup_cleanup.os,
            "kill",
            lambda pid, sig: killed.append(pid),
        )

        assert cgroup_cleanup.reap_cgroup(cgroup_path) == 1
        assert killed == [888]

    def test_skips_already_gone_and_denied_pids(self, monkeypatch):
        cgroup_path = "/user.slice/hermes-gateway-jarvis.service"
        procs = {cgroup_path: [111, 222, 333]}

        monkeypatch.setattr(cgroup_cleanup, "iter_cgroup_tree", lambda p: iter([p]))
        monkeypatch.setattr(
            cgroup_cleanup,
            "_read_cgroup_pids",
            lambda p: procs.get(p, []),
        )
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 999999)

        def fake_kill(pid, sig):
            if pid == 222:
                raise ProcessLookupError
            if pid == 333:
                raise PermissionError

        monkeypatch.setattr(cgroup_cleanup.os, "kill", fake_kill)
        # 111 killed, 222 gone, 333 denied -> count only the real kill
        assert cgroup_cleanup.reap_cgroup(cgroup_path) == 1
