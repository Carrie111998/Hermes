"""Test the ancestor-gating fix in _venv_launcher_ancestors (#87666).

The gateway is a two-process chain (venv launcher -> uv worker). When /update
is spawned BY the gateway, the launcher sits in the updater's own ancestor
chain and the old code skipped the whole chain, hiding the launcher. This
fix keeps gateway-runtime ancestors out of `skip` so the launcher is found.
"""
import sys
import types
from unittest.mock import patch

import pytest

import hermes_cli.update_cmd as cli_main
from hermes_cli import main as cli_main_module

WINDOWS_PATCH_TARGET = "hermes_cli.main._is_windows"


def _fake_psutil_with_ancestry(proc_tree, cmdlines):
    """Build a psutil stand-in.

    ``proc_tree`` maps pid -> parent pid (None for root).
    ``cmdlines`` maps pid -> argv list, for the skip-set gating.
    ``parents()`` on the CURRENT process returns the full ancestor chain
    (excluding self), which is what _venv_launcher_ancestors uses to build
    its skip set.
    """

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            if self.pid not in cmdlines:
                raise psutil_error("no such process")
            return cmdlines[self.pid]

        def parent(self):
            ppid = proc_tree.get(self.pid)
            if ppid is None:
                return None
            return FakeProc(ppid)

        def parents(self):
            # current process ancestry: walk up from self
            chain = []
            cur = self.pid
            seen = set()
            while cur in proc_tree and proc_tree[cur] is not None:
                cur = proc_tree[cur]
                if cur in seen:
                    break
                seen.add(cur)
                chain.append(FakeProc(cur))
            return chain

        def exe(self):
            # venv launcher = even pid, worker = odd pid (mirrors existing tests)
            if self.pid % 2 == 0:
                return str((cli_main._m().PROJECT_ROOT / "venv" / "Scripts" / "python.exe")).lower()
            return r"C:\Users\x\uv\python.exe"

    class psutil_error(Exception):
        pass

    mod = types.SimpleNamespace(Process=FakeProc, NoSuchProcess=psutil_error)
    return mod


# The updater's ancestry: updater(300) -> worker(200, uv) -> launcher(100, venv) -> wscript(1)
# The launcher (100) is the venv holder we must find. It's in the updater's
# ancestor chain, so the OLD code skipped it (bug); the fix keeps it visible.
ANCESTRY = {300: 200, 200: 100, 100: 1, 1: None}

GATEWAY_WORKER_CMD = [
    r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe",
    "-m", "hermes_cli.main", "gateway", "run", "--replace",
]
GATEWAY_LAUNCHER_CMD = [
    r"C:\hermes\venv\Scripts\python.exe",
    "-m", "hermes_cli.main", "gateway", "run",
]
WSCRIPT_CMD = [r"C:\Windows\System32\wscript.exe", "Hermes_Gateway.vbs"]


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_gateway_launcher_in_updater_ancestry_is_found(_winp, monkeypatch):
    """The gateway launcher in the updater's own ancestry must be returned."""
    cmdlines = {300: WSCRIPT_CMD, 200: GATEWAY_WORKER_CMD, 100: GATEWAY_LAUNCHER_CMD, 1: []}
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    # updater = pid 300 (the process whose parents() we simulate)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])  # worker
    assert 100 in found, f"launcher 100 should be found, got {found}"


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_non_gateway_ancestors_still_skipped(_winp, monkeypatch):
    """A non-gateway ancestor (e.g. plain shell) must still be skipped."""
    # updater(300) -> shell(250, cmd.exe) -> wscript(1)
    tree = {300: 250, 250: 1, 1: None}
    cmdlines = {
        300: [r"C:\Windows\System32\cmd.exe"],
        250: [r"C:\Windows\System32\cmd.exe", "/c", "hermes update"],
        1: [],
    }
    fake = _fake_psutil_with_ancestry(tree, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    # worker 200 doesn't exist in this tree; parents of 200 can't be resolved
    # -> nothing found, and no crash
    found = cli_main._venv_launcher_ancestors([200])
    assert found == []


@patch.object(cli_main_module, "_is_windows", return_value=True)
def test_gateway_launcher_with_spaces_in_path_still_found(_winp, monkeypatch):
    """list2cmdline quoting: paths with spaces must not break matching."""
    cmdlines = {
        300: WSCRIPT_CMD,
        200: [
            r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe",
            "-m", "hermes_cli.main", "gateway", "run", "--replace",
        ],
        100: [
            # NOTE: PROJECT_ROOT itself contains a space ("D:\Program file\...")
            # so this exercises the quoted-path path naturally.
            str((cli_main._m().PROJECT_ROOT / "venv" / "Scripts" / "python.exe")),
            "-m", "hermes_cli.main", "gateway", "run",
        ],
        1: [],
    }
    fake = _fake_psutil_with_ancestry(ANCESTRY, cmdlines)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(cli_main.os, "getpid", lambda: 300)

    found = cli_main._venv_launcher_ancestors([200])
    assert 100 in found, f"launcher 100 (space in path) should be found, got {found}"
