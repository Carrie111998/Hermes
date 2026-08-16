"""Tests for hermes_cli.gateway_windows."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.setup as setup




def test_schtasks_encoding_falls_back_to_utf8(monkeypatch):
    """A broken/empty locale must not leave us without a decoder (issue #38172)."""

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", lambda *a, **k: "")
    assert gateway_windows._schtasks_encoding() == "utf-8"

    def _boom(*args, **kwargs):
        raise RuntimeError("locale exploded")

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", _boom)
    assert gateway_windows._schtasks_encoding() == "utf-8"




@pytest.mark.windows_only
def test_build_gateway_argv_keeps_venv_console_python_for_uv_venv(monkeypatch, tmp_path):
    """No pythonw / base-interpreter detour: the venv console python.exe is
    launched hidden (CREATE_NO_WINDOW) so descendants inherit its hidden
    console instead of flashing their own (#54220/#56747).

    Windows-only: ``_build_gateway_argv()`` asserts the host is Windows and the
    argv/env overlay it returns is built from real Windows path separators and
    ``Scripts/python.exe`` layout — a patched ``sys.platform`` covered the
    branch but not any of that.
    """

    project = tmp_path / "project"
    scripts = project / "venv" / "Scripts"
    site_packages = project / "venv" / "Lib" / "site-packages"
    hermes_home = tmp_path / "hermes-home"
    base = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none"
    scripts.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    hermes_home.mkdir()
    base.mkdir(parents=True)

    venv_python = scripts / "python.exe"
    venv_pythonw = scripts / "pythonw.exe"
    base_pythonw = base / "pythonw.exe"
    for exe in (venv_python, venv_pythonw, base_pythonw):
        exe.write_text("", encoding="utf-8")
    (project / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\nimplementation = CPython\nuv = 0.11.14\nversion_info = 3.11.15\n",
        encoding="utf-8",
    )

    import hermes_cli.gateway as gateway

    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(venv_python))
    monkeypatch.setattr(gateway, "_profile_arg", lambda hermes_home: "")
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(hermes_home))

    argv, cwd, env_overlay = gateway_windows._build_gateway_argv()

    assert argv[:3] == [str(venv_python), "-m", "hermes_cli.main"]
    assert cwd == str(hermes_home.resolve())
    assert env_overlay["VIRTUAL_ENV"] == str(project / "venv")
    assert str(project) in env_overlay["PYTHONPATH"].split(gateway_windows.os.pathsep)


class TestStableWindowsGatewayWorkingDir:
    def test_stable_gateway_working_dir_uses_hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
        assert gateway_windows._stable_gateway_working_dir(tmp_path / "checkout") == str(home.resolve())

    def test_stable_gateway_working_dir_falls_back_to_project_root(self, tmp_path, monkeypatch):
        missing = tmp_path / "missing" / ".hermes"
        project = tmp_path / "checkout"
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: missing)
        assert gateway_windows._stable_gateway_working_dir(project) == str(project)




def _arrange_startup_fallback(monkeypatch, tmp_path, running_pids):
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    startup_entry = tmp_path / "Startup" / "Hermes_Gateway_alice.cmd"
    calls = []

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script_path)
    monkeypatch.setattr(
        gateway_windows,
        "_install_scheduled_task",
        lambda task_name, script_path: (
            False,
            "schtasks /Create failed (code 1): ERROR: Access is denied.",
        ),
    )
    monkeypatch.setattr(gateway_windows, "_should_fall_back", lambda code, detail: True)
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )

    def fake_install_startup_entry(path: Path) -> Path:
        calls.append(("install_startup", path))
        return startup_entry

    monkeypatch.setattr(gateway_windows, "_install_startup_entry", fake_install_startup_entry)
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: running_pids)
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile alice")
    return script_path, calls




@pytest.mark.windows_only
def test_elevated_gateway_command_uses_hidden_console_python(monkeypatch):
    """UAC handoff launches console python with SW_HIDE — a single hidden
    console, not console-less pythonw (#54220/#56747), and no visible
    elevated cmd.exe window left open.

    Windows-only: the code path runs behind ``_assert_windows()`` and goes
    through ``ctypes.windll.shell32``, neither of which exists on a faked
    host. ShellExecuteW itself stays mocked — it would raise a real UAC
    prompt — but the host identity is genuine.
    """
    calls = []

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, executable, params, cwd, show):
            calls.append((hwnd, verb, executable, params, cwd, show))
            return 33

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(gateway_windows, "_current_profile_cli_args", lambda: ["--profile", "alice"])
    monkeypatch.setattr(gateway_windows.sys, "executable", r"C:\Hermes\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_windows.ctypes, "windll", FakeWindll(), raising=False)

    assert gateway_windows._launch_elevated_gateway_command("install", ["--start-now", "--elevated-handoff"])

    assert len(calls) == 1
    _hwnd, verb, executable, params, cwd, show = calls[0]
    assert verb == "runas"
    assert executable == r"C:\Hermes\venv\Scripts\python.exe"
    assert "--profile alice gateway install --start-now --elevated-handoff" in params
    assert show == 0
    assert cwd


def test_install_scheduled_task_recreates_instead_of_change(monkeypatch, tmp_path):
    """Install must delete+create so stale minute-repeat task settings are not preserved.

    Host-agnostic on purpose: ``_install_scheduled_task`` only renders the task
    XML and shells out through ``_exec_schtasks`` (mocked here as the genuine
    external dependency), so no platform fake is needed.
    """
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    xml_seen = {}

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Delete":
            return (0, "SUCCESS", "")
        if args[0] == "/Create":
            xml_path = Path(args[args.index("/XML") + 1])
            xml_seen["text"] = xml_path.read_text(encoding="utf-16")
            return (0, "SUCCESS", "")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, detail = gateway_windows._install_scheduled_task("Hermes_Gateway_alice", script_path)

    assert ok is True
    assert "/Change" not in [arg for call in calls for arg in call]
    assert calls[0][:4] == ("/Delete", "/F", "/TN", "Hermes_Gateway_alice")
    assert calls[1][0] == "/Create"
    assert "/XML" in calls[1]
    assert "/SC" not in calls[1]
    assert "<Delay>PT30S</Delay>" in xml_seen["text"]
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml_seen["text"]
    assert "<StopOnIdleEnd>false</StopOnIdleEnd>" in xml_seen["text"]
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml_seen["text"]
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml_seen["text"]
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml_seen["text"]
    assert "<RestartOnFailure>" in xml_seen["text"]
    assert "<Count>999</Count>" in xml_seen["text"]
    # Scheduled Task launches the console-less .vbs via wscript.exe, never cmd.exe
    # (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A).
    assert "<Command>wscript.exe</Command>" in xml_seen["text"]
    assert "//B //Nologo" in xml_seen["text"]
    assert "Hermes_Gateway_alice.vbs" in xml_seen["text"]
    assert "cmd.exe" not in xml_seen["text"]


def test_gateway_vbs_script_is_console_less(monkeypatch):
    """The .vbs launcher must avoid cmd.exe entirely and Run pythonw hidden
    (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A)."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (r"C:\venv\Scripts\pythonw.exe", Path(r"C:\venv"), []),
    )
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\venv\Scripts\python.exe",
        r"C:\Hermes",
        r"C:\Hermes",
        "--profile work",
    )
    assert "cmd.exe" not in content.lower()
    assert 'CreateObject("WScript.Shell")' in content
    assert "pythonw.exe" in content
    assert "hermes_cli.main" in content
    assert "gateway run" in content
    assert ", 0, False" in content  # hidden window, detached/async
    for var in ("HERMES_HOME", "PYTHONIOENCODING", "HERMES_GATEWAY_DETACHED", "VIRTUAL_ENV", "PYTHONPATH"):
        assert var in content
    assert "--profile" in content and "work" in content
    assert content.endswith("\r\n")


_ROOT_GATEWAY_CHILD_SCOPE_MARKERS = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_WORKTREE",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
)


@pytest.mark.windows_only
@pytest.mark.parametrize("launcher", ["cmd", "vbs"])
def test_gateway_root_launcher_drops_child_scope_before_spawn(
    launcher,
    monkeypatch,
    tmp_path,
):
    """Generated root launchers must not inherit delegated-worker ownership."""
    fake_root = tmp_path / "fake-root"
    fake_package = fake_root / "hermes_cli"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    output_path = tmp_path / f"{launcher}-child-env.json"
    probe_source = (
        "import json, os\n"
        "from pathlib import Path\n"
        f"markers = {_ROOT_GATEWAY_CHILD_SCOPE_MARKERS!r}\n"
        "payload = {\n"
        "    'hermes_home': os.environ.get('HERMES_HOME'),\n"
        "    'sentinel': os.environ.get('ROOT_GATEWAY_KEEP_ME'),\n"
        "    'markers': {name: {'present': name in os.environ, 'value': os.environ.get(name)} for name in markers},\n"
        "}\n"
        "Path(os.environ['ROOT_GATEWAY_PROBE_OUTPUT']).write_text(json.dumps(payload), encoding='utf-8')\n"
    )
    (fake_package / "main.py").write_text(probe_source, encoding="utf-8")

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda _path: (sys.executable, Path(sys.prefix), []),
    )
    monkeypatch.setattr(gateway_windows, "_preserve_hermes_home_path", str)
    monkeypatch.setattr(
        gateway_windows, "__file__", str(fake_package / "gateway_windows.py")
    )
    monkeypatch.setenv("ROOT_GATEWAY_PROBE_OUTPUT", str(output_path))
    monkeypatch.setenv("ROOT_GATEWAY_KEEP_ME", "preserved")
    for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS:
        monkeypatch.setenv(marker, f"inherited::{marker}")

    if launcher == "cmd":
        content = gateway_windows._build_gateway_cmd_script(
            sys.executable,
            str(tmp_path),
            str(hermes_home),
            "",
        )
        script_path = tmp_path / "gateway.cmd"
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(script_path)]
        launch_needle = " -m "
    else:
        content = gateway_windows._build_gateway_vbs_script(
            sys.executable,
            str(tmp_path),
            str(hermes_home),
            "",
        )
        script_path = tmp_path / "gateway.vbs"
        argv = ["cscript.exe", "//B", "//Nologo", str(script_path)]
        launch_needle = "sh.Run "
    script_path.write_bytes(content.encode("utf-8"))

    completed = subprocess.run(
        argv,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 10
    while not output_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert output_path.is_file(), "launcher child did not write its environment probe"

    observed = json.loads(output_path.read_text(encoding="utf-8"))
    assert observed["hermes_home"] == str(hermes_home)
    assert observed["sentinel"] == "preserved"
    assert all(
        not row["present"] and row["value"] is None
        for row in observed["markers"].values()
    )
    before_launch = content[: content.index(launch_needle)]
    assert all(marker in before_launch for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS)


@pytest.mark.windows_only
def test_spawn_detached_scrubs_child_scope_env(monkeypatch, tmp_path):
    """The direct Windows launcher must not propagate worker ownership."""
    captured = []

    class _FakeProcess:
        pid = 4242

    def _fake_popen(argv, **kwargs):
        captured.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda: (
            [sys.executable, "-c", "pass"],
            str(tmp_path),
            {"HERMES_HOME": str(tmp_path), "ROOT_GATEWAY_OVERLAY": "preserved"},
        ),
    )
    monkeypatch.setattr(gateway_windows, "windows_detach_flags", lambda: 0)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: str(tmp_path)
    )
    monkeypatch.setenv("ROOT_GATEWAY_KEEP_ME", "preserved")
    for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS:
        monkeypatch.setenv(marker, "seeded-parent-value")

    assert gateway_windows._spawn_detached() == 4242

    assert len(captured) == 1
    child_env = captured[0][1]["env"]
    assert child_env["HERMES_HOME"] == str(tmp_path)
    assert child_env["ROOT_GATEWAY_OVERLAY"] == "preserved"
    assert child_env["ROOT_GATEWAY_KEEP_ME"] == "preserved"
    for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS:
        assert marker not in child_env


@pytest.mark.windows_only
def test_windows_update_restart_watcher_scrubs_child_scope_env(monkeypatch):
    """The post-update watcher must receive a clean root-gateway environment."""
    import hermes_cli._subprocess_compat as subprocess_compat
    import hermes_cli.gateway as gateway

    captured = []

    def _fake_popen(argv, **kwargs):
        captured.append((argv, kwargs))
        return object()

    monkeypatch.setattr(
        gateway_windows,
        "windowless_gateway_restart_spec",
        lambda argv: (list(argv), "C:/hermes", {"ROOT_GATEWAY_OVERLAY": "preserved"}),
    )
    monkeypatch.setattr(
        subprocess_compat,
        "windows_detach_popen_kwargs",
        lambda: {"creationflags": 0},
    )
    monkeypatch.setattr(gateway.subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("ROOT_GATEWAY_KEEP_ME", "preserved")
    for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS:
        monkeypatch.setenv(marker, "seeded-parent-value")

    assert gateway._spawn_gateway_restart_watcher(
        4242,
        [sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
    )

    assert len(captured) == 1
    watcher_env = captured[0][1]["env"]
    assert watcher_env["ROOT_GATEWAY_KEEP_ME"] == "preserved"
    for marker in _ROOT_GATEWAY_CHILD_SCOPE_MARKERS:
        assert marker not in watcher_env














# ---------------------------------------------------------------------------
# stop() drain semantics — issue #33778
#
# Background: on Windows, asyncio.add_signal_handler raises NotImplementedError,
# so the gateway's SIGTERM handler (which drains in-flight agents and writes
# resume_pending=True) never fires when `hermes gateway stop` kills the
# process. The fix: stop() writes the planned_stop_marker first, waits for
# the gateway's marker-watcher thread to drain + exit cleanly, then escalates
# to taskkill if drain times out.
# ---------------------------------------------------------------------------










