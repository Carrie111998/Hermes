"""A respawn must reproduce the command that was actually running (#99450 R2-6).

The spawn ledger recorded ``" ".join(sys.argv[:10])`` and the relaunch
``shlex.split`` it back apart. That round trip is lossy in two ways that
both end in a WRONG process rather than a failed one:

* any argument containing whitespace comes back as several arguments;
* anything past the tenth token is simply gone.

``--profile``, ``--host`` and ``--port`` sit at exactly the positions a
slightly-longer command pushes over that cliff, so the respawned backend
could come back on the wrong profile or the wrong port. And
``detail['hermes_home']``, which the respawn used to set ``HERMES_HOME``
from, had no producer at all — nothing ever wrote it.

The ledger now records the full argv losslessly, plus the structured
launch authority (host/port/profile) and the runtime's HERMES_HOME. These
tests respawn REAL processes and read back what they actually received.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import hermes_cli.process_identity as pi
import hermes_cli.update_cmd as update_cmd
from hermes_cli import update_quiesce

ECHO_SRC = """
import json, os, sys

out = sys.argv[1]
with open(out, "w", encoding="utf-8") as handle:
    json.dump(
        {"argv": sys.argv[2:], "hermes_home": os.environ.get("HERMES_HOME", "")},
        handle,
    )
"""


@pytest.fixture()
def echo(tmp_path):
    script = tmp_path / "echo_argv.py"
    script.write_text(ECHO_SRC, encoding="utf-8")
    return script


def _await_json(path: Path, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass  # still being written
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written — the respawn did not run")


def _record(detail, *, kind="serve", profile="default"):
    return {"kind": kind, "profile": profile, "pid": 4242, "detail": detail}


# ---------------------------------------------------------------------------
# The lossless path
# ---------------------------------------------------------------------------


class TestLosslessArgvRespawn:
    def test_arguments_containing_spaces_survive(self, echo, tmp_path):
        out = tmp_path / "spaces.json"
        argv = [
            sys.executable,
            str(echo),
            str(out),
            "a value with spaces",
            "--flag=x y z",
            "trailing ",
        ]
        pid = update_cmd._respawn_recorded_runtime("", _record({"argv_list": argv}))
        assert isinstance(pid, int)
        assert _await_json(out)["argv"] == argv[3:]

    def test_more_than_ten_arguments_survive(self, echo, tmp_path):
        out = tmp_path / "long.json"
        tail = [f"--opt{i}" for i in range(18)]
        argv = [sys.executable, str(echo), str(out), *tail]
        pid = update_cmd._respawn_recorded_runtime("", _record({"argv_list": argv}))
        assert isinstance(pid, int)
        got = _await_json(out)["argv"]
        assert got == tail
        assert len(got) == 18, "the legacy 10-token cap must be gone"

    def test_hermes_home_reaches_the_replacement(self, echo, tmp_path):
        out = tmp_path / "home.json"
        home = tmp_path / "some home"
        home.mkdir()
        argv = [sys.executable, str(echo), str(out)]
        pid = update_cmd._respawn_recorded_runtime(
            "", _record({"argv_list": argv, "hermes_home": str(home)})
        )
        assert isinstance(pid, int)
        assert _await_json(out)["hermes_home"] == str(home)

    def test_the_lossless_argv_beats_a_stale_legacy_string(self, echo, tmp_path):
        """Both present: the list is the truth, the string is a rendering."""
        out = tmp_path / "both.json"
        argv = [sys.executable, str(echo), str(out), "one two"]
        pid = update_cmd._respawn_recorded_runtime(
            "totally wrong legacy string",
            _record({"argv_list": argv, "argv": "totally wrong legacy string"}),
        )
        assert isinstance(pid, int)
        assert _await_json(out)["argv"] == ["one two"]


# ---------------------------------------------------------------------------
# Structured authority for serve/dashboard
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim script")
class TestStructuredServeAuthority:
    def _shim(self, tmp_path, out):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        shim = scripts / "hermes"
        shim.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > ' + str(out) + "\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return scripts

    def _await_lines(self, out):
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if out.is_file():
                text = out.read_text(encoding="utf-8")
                if text:
                    return text.splitlines()
            time.sleep(0.05)
        raise AssertionError("the structured respawn never ran")

    def test_a_legacy_serve_entry_relaunches_from_host_port_profile(
        self, tmp_path, monkeypatch
    ):
        """No argv_list on an upgraded-from-old ledger — but host/port/profile
        are structured, and that is a better authority than a joined string."""
        out = tmp_path / "shim.out"
        scripts = self._shim(tmp_path, out)
        monkeypatch.setattr("hermes_cli.main._venv_scripts_dir", lambda: scripts)

        pid = update_cmd._respawn_recorded_runtime(
            "hermes serve --host 127.0.0.1 --port 9119",
            _record(
                {"host": "127.0.0.1", "port": 9119, "argv": "hermes serve"},
                kind="serve",
                profile="work",
            ),
        )
        assert isinstance(pid, int)
        assert self._await_lines(out) == [
            "--profile",
            "work",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "9119",
        ]


# ---------------------------------------------------------------------------
# Ambiguous legacy records are refused, not guessed at
# ---------------------------------------------------------------------------


@pytest.fixture()
def never_spawns(monkeypatch):
    """A refusal must be decided BEFORE anything is launched.

    Asserting only on the ``None`` return is not enough: ``_respawn_recorded_runtime``
    also returns ``None`` when ``Popen`` raises ``OSError`` because the recorded
    executable does not resolve on ``PATH``. On a box without ``hermes`` on
    ``PATH`` that makes an accepted-but-wrong record look refused, which is how
    a genuinely broken round-trip check ran green. Fail on the spawn itself so
    these tests answer the same way in both environments.
    """

    def _boom(parts, *args, **kwargs):
        raise AssertionError(f"a record that must be refused was spawned: {parts!r}")

    monkeypatch.setattr(update_cmd.subprocess, "Popen", _boom)


class TestAmbiguousLegacyRecordsAreRefused:
    def test_a_truncated_legacy_argv_is_refused(self, never_spawns):
        """Ten tokens is exactly the old cap — the eleventh may be missing."""
        argv = " ".join(["hermes", "serve"] + [f"--o{i}" for i in range(8)])
        assert update_cmd._respawn_recorded_runtime(argv, _record({"argv": argv})) is None

    def test_a_quoted_legacy_argv_is_refused(self, never_spawns):
        """Quoting in the string proves the join was not a plain join."""
        argv = "hermes serve --config '/my dir/c.toml'"
        assert (
            update_cmd._respawn_recorded_runtime(argv, _record({"argv": argv}, kind="gateway"))
            is None
        )

    def test_repeated_whitespace_in_a_legacy_argv_is_refused(self, never_spawns):
        """A plain join emits exactly one space — more means it was not one."""
        argv = "hermes  serve --port 9119"
        assert (
            update_cmd._respawn_recorded_runtime(argv, _record({"argv": argv}, kind="gateway"))
            is None
        )

    def test_a_short_unambiguous_legacy_argv_still_works(self, echo, tmp_path):
        """Backward compatibility: a plain, short, unquoted argv is honoured."""
        out = tmp_path / "legacy.json"
        argv = f"{sys.executable} {echo} {out}"
        pid = update_cmd._respawn_recorded_runtime(
            argv, _record({"argv": argv}, kind="gateway")
        )
        assert isinstance(pid, int)
        assert _await_json(out)["argv"] == []

    def test_an_empty_record_is_refused(self, never_spawns):
        assert update_cmd._respawn_recorded_runtime("", _record({})) is None


# ---------------------------------------------------------------------------
# The producers
# ---------------------------------------------------------------------------


class TestTheLedgerProducesWhatTheRespawnConsumes:
    def test_register_self_records_the_full_argv_and_home(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pi, "_ledger_path", lambda: tmp_path / "ledger.json")
        monkeypatch.setattr(pi, "install_id", lambda *a, **k: "inst")
        home = tmp_path / "home dir"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        argv = ["hermes", "serve", "--note", "a b c"] + [f"--o{i}" for i in range(12)]
        monkeypatch.setattr(sys, "argv", argv)

        assert pi.register_self("serve", detail={"host": "h", "port": 1, "profile": "p"})

        entry = pi._read_ledger(tmp_path / "ledger.json")[-1]
        assert entry["argv_list"] == argv, "argv must be recorded losslessly"
        assert Path(entry["hermes_home"]) == home

    def test_the_inventory_carries_them_into_the_relaunch_record(
        self, monkeypatch, tmp_path
    ):
        import sys as _sys
        from types import SimpleNamespace

        import hermes_cli.update_inventory as ui

        entry = {
            "pid": 4321,
            "create_time": 111.0,
            "purpose": "serve",
            "install": "inst",
            "spawner_pid": None,
            "spawner_create": None,
            "registered_at": 222.0,
            "argv": "hermes serve",
            "argv_list": ["hermes", "serve", "--note", "a b"],
            "hermes_home": str(tmp_path / "home dir"),
            "host": "127.0.0.1",
            "port": 9119,
            "profile": "default",
        }
        monkeypatch.setitem(
            _sys.modules,
            "hermes_cli.process_identity",
            SimpleNamespace(
                ledger_entries=lambda **k: [entry],
                spawner_is_dead=lambda e: True,
            ),
        )
        monkeypatch.setattr(ui, "_default_pid_cgroup", lambda pid: None)
        monkeypatch.setattr(ui, "_live_launchd_labels", lambda: {})
        monkeypatch.setattr(ui, "_windows_service_names_by_pid", lambda: {})

        row = [r for r in ui.collect_runtime_inventory().runtimes if r.kind == "serve"][0]
        assert row.detail["argv_list"] == ["hermes", "serve", "--note", "a b"]
        assert row.detail["hermes_home"] == str(tmp_path / "home dir")
        assert update_quiesce.relaunch_authority(row) == "argv"


def test_a_structured_only_record_counts_as_relaunch_authority():
    """An upgraded ledger row with no argv at all is still relaunchable, so
    the inventory check must not refuse the whole update over it."""
    record = _record({"host": "127.0.0.1", "port": 9119})
    assert update_quiesce.relaunch_authority(record) == "argv"
