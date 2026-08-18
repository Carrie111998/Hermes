"""Launch attribution for the ``gateway.start`` exit-diag record.

The ``argv`` field alone cannot identify who started a gateway: it is
``sys.argv`` captured *after* argparse consumed the global ``--profile``
option, so a process whose real command line is::

    pythonw.exe -m hermes_cli.main --profile main gateway run

logs only ``["...\\hermes_cli\\main.py", "gateway", "run"]``. An August 2026
double-spawn investigation wrongly eliminated two candidate launchers on
those grounds and never identified the real second spawner.

These tests pin the fields that make attribution possible: the process's
own command line, its parent chain, and an explicit spawn-site marker
stamped by whichever code path launched it. Every one of them is
diagnostic scaffolding, so every one of them must degrade to a null rather
than raise -- a broken diagnostic must never be why a gateway fails to
start.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_diag as gateway_diag
import hermes_cli.gateway_windows as gateway_windows


# ── raw_cmdline ──────────────────────────────────────────────────────────


def test_raw_cmdline_reports_the_os_command_line_not_sys_argv():
    """``raw_cmdline`` must be what the OS says, not what argparse left behind."""
    import psutil

    raw = gateway_diag.raw_cmdline()

    assert raw == list(psutil.Process(os.getpid()).cmdline())
    assert raw
    assert all(isinstance(arg, str) for arg in raw)


def test_raw_cmdline_is_none_when_psutil_raises(monkeypatch):
    class _BoomProcess:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("psutil exploded")

    monkeypatch.setattr("psutil.Process", _BoomProcess)

    assert gateway_diag.raw_cmdline() is None


def test_raw_cmdline_is_none_when_psutil_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)

    assert gateway_diag.raw_cmdline() is None


# ── ppid ─────────────────────────────────────────────────────────────────


def test_ppid_matches_os_getppid():
    assert gateway_diag.parent_pid() == os.getppid()


def test_ppid_is_none_when_the_platform_refuses(monkeypatch):
    def _boom():
        raise OSError("no getppid here")

    monkeypatch.setattr(gateway_diag.os, "getppid", _boom)

    assert gateway_diag.parent_pid() is None


# ── parent_chain ─────────────────────────────────────────────────────────


def test_parent_chain_starts_at_the_immediate_parent():
    chain = gateway_diag.parent_chain()

    assert chain, "test runner must have a live parent process"
    assert chain[0]["pid"] == os.getppid()
    assert set(chain[0]) == {"pid", "name", "cmdline"}


def test_parent_chain_is_capped_at_the_requested_depth():
    assert len(gateway_diag.parent_chain(depth=2)) <= 2
    assert len(gateway_diag.parent_chain(depth=1)) <= 1


class _FakeProc:
    """Minimal psutil.Process stand-in with per-attribute failure control."""

    def __init__(self, pid, name="fake.exe", cmdline=None, parent=None, *, deny=()):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline or []
        self._parent = parent
        self._deny = set(deny)

    def _maybe_deny(self, field):
        if field in self._deny:
            raise RuntimeError(f"access denied reading {field}")

    def name(self):
        self._maybe_deny("name")
        return self._name

    def cmdline(self):
        self._maybe_deny("cmdline")
        return list(self._cmdline)

    def parent(self):
        self._maybe_deny("parent")
        return self._parent


def _install_fake_psutil(monkeypatch, leaf):
    """Point ``psutil.Process()`` at a synthetic process tree."""
    monkeypatch.setattr("psutil.Process", lambda *a, **k: leaf)


def test_parent_chain_still_records_a_parent_whose_cmdline_is_denied(monkeypatch):
    """An elevated/foreign parent denies cmdline() but still yields pid + name."""
    grandparent = _FakeProc(3, name="services.exe", cmdline=["services.exe"])
    parent = _FakeProc(2, name="svchost.exe", parent=grandparent, deny={"cmdline"})
    _install_fake_psutil(monkeypatch, _FakeProc(1, parent=parent))

    chain = gateway_diag.parent_chain(depth=3)

    assert chain[0] == {"pid": 2, "name": "svchost.exe", "cmdline": None}
    assert chain[1] == {"pid": 3, "name": "services.exe", "cmdline": ["services.exe"]}


def test_parent_chain_stops_cleanly_when_walking_up_fails(monkeypatch):
    parent = _FakeProc(2, name="wscript.exe", cmdline=["wscript.exe", "gateway.vbs"], deny={"parent"})
    _install_fake_psutil(monkeypatch, _FakeProc(1, parent=parent))

    chain = gateway_diag.parent_chain(depth=3)

    assert chain == [{"pid": 2, "name": "wscript.exe", "cmdline": ["wscript.exe", "gateway.vbs"]}]


def test_parent_chain_is_empty_when_the_parent_is_already_gone(monkeypatch):
    _install_fake_psutil(monkeypatch, _FakeProc(1, parent=None))

    assert gateway_diag.parent_chain() == []


def test_parent_chain_is_empty_when_psutil_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)

    assert gateway_diag.parent_chain() == []


# ── the assembled gateway.start identity payload ─────────────────────────


def test_start_diag_identity_carries_every_attribution_field(monkeypatch):
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "cli:restart")

    identity = gateway_diag.launch_identity()

    assert set(identity) == {"raw_cmdline", "ppid", "parent_chain", "spawn_site"}
    assert identity["ppid"] == os.getppid()
    assert identity["spawn_site"] == "cli:restart"
    assert identity["raw_cmdline"], "raw_cmdline must survive a healthy psutil"


def test_both_lifecycle_records_carry_the_attribution_fields(monkeypatch, tmp_path):
    """gateway.spawn and gateway.start must each stand alone.

    A launcher that never imports hermes_cli.main writes no gateway.spawn, and
    a racer that dies in the startup guards writes no gateway.start. Whichever
    one survives has to name the launcher by itself.
    """
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_diag, "write_diag", lambda tag, **extra: records.append((tag, extra))
    )
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "windows-task-script")
    # Sidestep the once-per-PID guard, and let monkeypatch clean the var up so
    # this test can't suppress a later one's spawn record.
    monkeypatch.setenv(gateway_diag.SPAWN_LOGGED_PID_VAR, "not-this-pid")

    assert gateway_diag.emit_gateway_spawn_diag(["hermes", "--profile", "main", "gateway", "run"])

    tag, fields = records[0]
    assert tag == "gateway.spawn"
    for key in ("raw_cmdline", "ppid", "parent_chain", "spawn_site"):
        assert key in fields, key
    assert fields["spawn_site"] == "windows-task-script"
    # The pre-strip argv is what makes this record honest about --profile.
    assert fields["argv"] == ["hermes", "--profile", "main", "gateway", "run"]


def test_spawn_record_is_still_skipped_for_non_gateway_commands(monkeypatch):
    """Attribution must not turn every `hermes` invocation into a log write."""
    records: list = []
    monkeypatch.setattr(gateway_diag, "write_diag", lambda tag, **extra: records.append(tag))

    assert gateway_diag.emit_gateway_spawn_diag(["hermes", "status"]) is False
    assert records == []


def test_start_diag_identity_reports_no_spawn_site_when_unstamped(monkeypatch):
    """An unstamped launch is itself evidence: it came from outside our spawners."""
    monkeypatch.delenv(gateway_diag.SPAWN_SITE_ENV, raising=False)

    assert gateway_diag.launch_identity()["spawn_site"] is None


def test_start_diag_identity_is_json_serializable():
    """The diag writer json.dumps() the record; unserializable values would drop it."""
    json.dumps(gateway_diag.launch_identity())


def test_start_diag_identity_degrades_instead_of_raising(monkeypatch):
    """A totally broken psutil must not break gateway startup."""

    class _BoomProcess:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("psutil exploded")

    monkeypatch.setattr("psutil.Process", _BoomProcess)

    identity = gateway_diag.launch_identity()

    assert identity["raw_cmdline"] is None
    assert identity["parent_chain"] == []
    assert identity["ppid"] == os.getppid()


# ── spawn-site stamping on the Windows launch paths ──────────────────────


@pytest.fixture
def spawn_capture(monkeypatch, tmp_path):
    """Neutralize the real Popen and capture the child environment."""
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda: (["pythonw.exe", "-m", "hermes_cli.main", "gateway", "run"], str(tmp_path), {}),
    )
    monkeypatch.setattr(gateway_windows, "windows_detach_flags", lambda: 0)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)

    captured: dict[str, dict] = {}

    class _FakePopen:
        pid = 4242

        def __init__(self, argv, **kwargs):
            captured["env"] = kwargs["env"]

    monkeypatch.setattr(gateway_windows.subprocess, "Popen", _FakePopen)
    return captured


def test_spawn_detached_stamps_the_call_site_into_the_child_env(spawn_capture):
    gateway_windows._spawn_detached(reason="cli:restart")

    assert spawn_capture["env"][gateway_diag.SPAWN_SITE_ENV] == "cli:restart"


def test_spawn_detached_overrides_an_inherited_spawn_site(monkeypatch, spawn_capture):
    """The stamp must describe *this* spawn, not the one that made our parent."""
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "install:scheduled-task")

    gateway_windows._spawn_detached(reason="update:windows-cold-start")

    assert spawn_capture["env"][gateway_diag.SPAWN_SITE_ENV] == "update:windows-cold-start"


def test_spawn_detached_always_stamps_something(spawn_capture):
    """An unreasoned caller still gets a marker, so a blank field means 'not us'."""
    gateway_windows._spawn_detached()

    assert spawn_capture["env"][gateway_diag.SPAWN_SITE_ENV]


def test_launch_detached_gateway_threads_the_reason_through(monkeypatch):
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)
    seen = []
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda *a, reason="", **k: seen.append(reason) or 4242,
    )

    gateway_windows._launch_detached_gateway(reason="cli:restart")

    assert seen == ["cli:restart"]


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("start", "cli:start"),
        ("restart", "cli:restart"),
    ],
)
def test_cli_start_and_restart_are_distinguishable_in_the_log(monkeypatch, entry, expected):
    """Restart storms and cold starts must not look identical in the diag log."""
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(gateway_windows, "is_startup_entry_installed", lambda: False)
    monkeypatch.setattr(gateway_windows, "stop", lambda: None)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **k: True)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **k: True)
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda *_: None)

    seen = []
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda *a, reason="", **k: seen.append(reason) or 4242,
    )

    getattr(gateway_windows, entry)()

    assert seen == [expected]


def test_launchd_detached_fallback_stamps_its_own_spawn(monkeypatch, tmp_path):
    """The macOS launchd fallback is a spawn path too — leave no null holes.

    A null spawn_site is supposed to mean "no code of ours launched this". That
    reading only holds if every one of our spawn paths stamps itself.
    """
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_gateway_run_command", lambda: ["python", "-m", "hermes_cli.main"])

    captured: dict[str, dict] = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["env"] = kwargs.get("env")

    monkeypatch.setattr(gateway.subprocess, "Popen", _FakePopen)

    assert gateway._spawn_detached_gateway() is True
    assert captured["env"][gateway_diag.SPAWN_SITE_ENV] == "launchd-fallback:detached"
    assert captured["env"]["PATH"] == os.environ["PATH"], "must still inherit the ambient env"


# ── spawn-site stamping on the service-managed launch paths ──────────────
#
# The double-spawn investigation could not tell a Scheduled Task launch from a
# Startup-folder launch, because both route through the same gateway.vbs. These
# stamp the environment so the child's own diag record says which one ran it.


def test_task_script_stamps_the_service_spawn_site():
    content = gateway_windows._build_gateway_cmd_script(
        r"C:\venv\Scripts\python.exe", r"C:\work", r"C:\Users\x\.hermes", "--profile main"
    )

    assert f'set "{gateway_diag.SPAWN_SITE_ENV}=' in content


def test_vbs_launcher_defers_to_a_spawn_site_already_set_by_its_caller():
    """gateway.vbs is chained by the Startup dropper; it must not overwrite its stamp."""
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\venv\Scripts\python.exe", r"C:\work", r"C:\Users\x\.hermes", "--profile main"
    )

    assert gateway_diag.SPAWN_SITE_ENV in content
    assert gateway_windows._SPAWN_SITE_TASK_SCRIPT in content
    assert "If Len(" in content


def test_startup_launcher_identifies_itself_before_chaining(tmp_path):
    script = tmp_path / "gateway.cmd"
    content = gateway_windows._build_startup_launcher(script)

    assert gateway_diag.SPAWN_SITE_ENV in content
    assert gateway_windows._SPAWN_SITE_STARTUP_FOLDER in content


@pytest.fixture
def install_spawn_reasons(monkeypatch, tmp_path):
    """Drive install() with everything stubbed, recording spawn reasons."""
    reasons: list[str] = []
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_test")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: tmp_path / "gateway.cmd")
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "_install_startup_entry", lambda path: tmp_path / "startup.vbs")
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: None)
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda *a, reason="", **k: reasons.append(reason) or 12345,
    )
    return reasons


def test_install_with_scheduled_task_names_its_own_spawn(monkeypatch, install_spawn_reasons):
    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *a, **k: (True, True))
    monkeypatch.setattr(
        gateway_windows, "_install_scheduled_task", lambda name, path: (True, "Created Scheduled Task")
    )

    gateway_windows.install(force=False)

    assert install_spawn_reasons == ["install:scheduled-task"]


def test_install_without_login_autostart_names_its_own_spawn(monkeypatch, install_spawn_reasons):
    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *a, **k: (True, False))

    gateway_windows.install(force=False)

    assert install_spawn_reasons == ["install:no-login-autostart"]


def test_install_falling_back_after_schtasks_names_its_own_spawn(monkeypatch, install_spawn_reasons):
    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *a, **k: (True, True))
    monkeypatch.setattr(
        gateway_windows, "_install_scheduled_task", lambda name, path: (False, "ERROR: Access is denied.")
    )
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr("hermes_cli.gateway._profile_arg", lambda *a, **k: "")

    gateway_windows.install(force=False)

    assert install_spawn_reasons == ["install:startup-fallback-after-schtasks"]


def test_startup_fallback_install_names_its_own_spawn(monkeypatch, install_spawn_reasons, tmp_path):
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr("hermes_cli.gateway._profile_arg", lambda *a, **k: "")

    gateway_windows._install_startup_fallback(tmp_path / "gateway.cmd", True, "denied")

    assert install_spawn_reasons == ["install:startup-fallback"]


# The updater's own cold-start site is covered in
# test_update_concurrent_quarantine.py, which already pays for the
# hermes_cli.main import this file would otherwise duplicate.


def test_generated_launchers_stay_syntactically_intact(tmp_path):
    """Guard the hand-built VBScript: Option Explicit punishes undeclared vars."""
    vbs = gateway_windows._build_gateway_vbs_script(
        r"C:\venv\Scripts\python.exe", r"C:\work", r"C:\Users\x\.hermes", ""
    )
    declared = [line for line in vbs.splitlines() if line.startswith("Dim ")]
    names = {n.strip() for line in declared for n in line[4:].split(",")}

    for token in ("sh", "env"):
        assert token in names
    assert vbs.count("If ") == vbs.count("End If")

    dropper = gateway_windows._build_startup_launcher(tmp_path / "gateway.cmd")
    dropper_names = {
        n.strip() for line in dropper.splitlines() if line.startswith("Dim ") for n in line[4:].split(",")
    }
    for token in ("fso", "sh", "target"):
        assert token in dropper_names


def test_force_terminate_records_who_killed_the_gateway(monkeypatch):
    """A force-kill must be attributed by the KILLER, because the victim cannot.

    `_force_terminate_known_gateway_pids` escalates to `terminate_pid(force=True)`
    (taskkill /F). Windows `TerminateProcess` does not run `atexit`, so the victim's
    own `atexit.hook` / `gateway.exit_*` records are never written — the gateway
    simply vanishes leaving no trace at all.

    Observed 2026-08-18: two gateways died (pids 11232 ~09:30, 44064 ~10:12:57) with
    ZERO exit records, while the same instrumentation wrote 30 exit records the day
    before. No amount of victim-side instrumentation can close that gap; only the
    killer can say a kill happened, and who asked for it.
    """
    from hermes_cli import gateway_windows

    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_windows,
        "write_diag",
        lambda tag, **kw: records.append((tag, kw)),
        raising=False,
    )

    import gateway.status as gateway_status

    monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(
        gateway_status,
        "terminate_pid",
        lambda pid, force=False: killed.append(pid),
    )

    gateway_windows._force_terminate_known_gateway_pids([31337])

    assert killed == [31337]
    kill_records = [r for r in records if r[0] == "gateway.force_kill"]
    assert kill_records, f"no gateway.force_kill record written; got {[r[0] for r in records]}"
    payload = kill_records[0][1]
    assert payload.get("victim_pid") == 31337
    # The whole point: name the killer, since the victim can't.
    assert payload.get("killer_pid") == os.getpid()
    assert "killer_cmdline" in payload
