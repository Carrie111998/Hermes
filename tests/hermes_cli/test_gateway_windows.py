"""Tests for hermes_cli.gateway_windows."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.setup as setup


@pytest.mark.parametrize(
    "detail",
    [
        "ERROR: Access is denied.",
        "ERROR: Acceso denegado.",
        "ERROR: Přístup byl odepřen.",
        "schtasks timed out after 15s",
        "schtasks produced no output",
    ],
)
def test_schtasks_fallback_patterns_cover_localized_access_denied(detail):
    """Localized schtasks access-denied errors should use Startup fallback."""

    assert gateway_windows._should_fall_back(1, detail) is True


def test_schtasks_fallback_does_not_hide_unknown_errors():
    assert gateway_windows._should_fall_back(1, "ERROR: The system cannot find the file specified.") is False


def test_schtasks_encoding_falls_back_to_utf8(monkeypatch):
    """A broken/empty locale must not leave us without a decoder (issue #38172)."""

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", lambda *a, **k: "")
    assert gateway_windows._schtasks_encoding() == "utf-8"

    def _boom(*args, **kwargs):
        raise RuntimeError("locale exploded")

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", _boom)
    assert gateway_windows._schtasks_encoding() == "utf-8"


def test_exec_schtasks_decodes_with_replace_errors(monkeypatch):
    """schtasks output must be decoded with errors='replace' so localized
    (non-UTF-8) bytes never surface a UnicodeDecodeError traceback (#38172)."""

    captured: dict[str, object] = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows.shutil, "which", lambda name: r"C:\\Windows\\System32\\schtasks.exe")
    monkeypatch.setattr(gateway_windows.subprocess, "run", fake_run)

    code, out, err = gateway_windows._exec_schtasks(["/Query", "/TN", "Hermes_Gateway"])

    assert (code, out, err) == (0, "ok", "")
    assert captured["errors"] == "replace", "schtasks output must decode with errors='replace'"
    assert isinstance(captured["encoding"], str) and captured["encoding"], (
        "an explicit non-empty encoding must be passed to subprocess.run"
    )
    assert captured["text"] is True


def test_build_gateway_argv_uses_base_pythonw_for_uv_venv_launcher(monkeypatch, tmp_path):
    """Avoid uv's venv pythonw launcher because it respawns console python.exe."""

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

    monkeypatch.setattr(gateway_windows.sys, "platform", "win32")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(venv_python))
    monkeypatch.setattr(gateway, "_profile_arg", lambda hermes_home: "")
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(hermes_home))

    argv, cwd, env_overlay = gateway_windows._build_gateway_argv()

    assert argv[:3] == [str(base_pythonw), "-m", "hermes_cli.main"]
    assert cwd == str(hermes_home.resolve())
    assert env_overlay["VIRTUAL_ENV"] == str(project / "venv")
    assert str(project) in env_overlay["PYTHONPATH"].split(gateway_windows.os.pathsep)
    assert str(site_packages) in env_overlay["PYTHONPATH"].split(gateway_windows.os.pathsep)


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


def test_write_task_script_anchors_cmd_cd_at_hermes_home(monkeypatch, tmp_path):
    project = tmp_path / "project"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    python_exe = project / "venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    script_path = tmp_path / "gateway.cmd"

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(python_exe))
    monkeypatch.setattr(gateway, "_profile_arg", lambda hermes_home: "")
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(gateway_windows, "get_task_script_path", lambda: script_path)

    written = gateway_windows._write_task_script()
    content = script_path.read_text(encoding="utf-8")

    assert written == script_path
    assert f"cd /d {gateway_windows._quote_cmd_script_arg(str(hermes_home.resolve()))}" in content
    assert f"cd /d {gateway_windows._quote_cmd_script_arg(str(project))}" not in content


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
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: running_pids)
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile alice")
    return script_path, calls


def test_gateway_cmd_script_uses_pythonw_without_replace_or_start_churn(monkeypatch):
    """Scheduled Task wrapper should launch pythonw once and avoid replace loops."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (exe.replace("python.exe", "pythonw.exe"), r"C:\\Hermes\\hermes-agent\\venv", []),
    )

    content = gateway_windows._build_gateway_cmd_script(
        r"C:\\Hermes\\hermes-agent\\venv\\Scripts\\python.exe",
        r"C:\\Hermes\\hermes-agent",
        r"C:\\HermesHome\\profiles\\alice",
        "--profile alice",
    )

    assert "pythonw.exe" in content
    assert "gateway run" in content
    assert "--replace" not in content
    assert "start \"\"" not in content
    assert "exit /b 0" in content


def test_gateway_cmd_script_uses_uv_safe_base_pythonw(monkeypatch, tmp_path):
    """Scheduled Task wrapper should share the detached uv-venv workaround."""
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

    content = gateway_windows._build_gateway_cmd_script(
        str(venv_python),
        str(hermes_home),
        str(hermes_home),
        "",
    )

    assert str(base_pythonw) in content
    assert f'set "VIRTUAL_ENV={project / "venv"}"' in content
    assert str(site_packages) in content
    assert str(venv_pythonw) not in content


def test_elevated_gateway_command_uses_pythonw_hidden_console(monkeypatch):
    """UAC handoff should not leave a second elevated cmd.exe window open."""
    calls = []

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, executable, params, cwd, show):
            calls.append((hwnd, verb, executable, params, cwd, show))
            return 33

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_current_profile_cli_args", lambda: ["--profile", "alice"])
    monkeypatch.setattr(gateway_windows, "_derive_venv_pythonw", lambda exe: exe.replace("python.exe", "pythonw.exe"))
    monkeypatch.setattr(gateway_windows.sys, "executable", r"C:\Hermes\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_windows.ctypes, "windll", FakeWindll(), raising=False)

    assert gateway_windows._launch_elevated_gateway_command("install", ["--start-now", "--elevated-handoff"])

    assert len(calls) == 1
    _hwnd, verb, executable, params, cwd, show = calls[0]
    assert verb == "runas"
    assert executable.endswith("pythonw.exe")
    assert "--profile alice gateway install --start-now --elevated-handoff" in params
    assert show == 0
    assert cwd


def test_install_scheduled_task_recreates_instead_of_change(monkeypatch, tmp_path):
    """Install must delete+create so stale minute-repeat task settings are not preserved."""
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    xml_seen = {}

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
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


def test_gateway_vbs_script_quotes_spaced_paths(monkeypatch):
    """Spaced exe/dir paths stay correctly quoted through the VBScript literal."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (r"C:\Program Files\Py\pythonw.exe", Path(r"C:\v env"), []),
    )
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\Program Files\Py\python.exe",
        r"C:\work dir",
        r"C:\h home",
        "",
    )
    # list2cmdline quotes the spaced exe; _quote_vbs_string doubles those quotes.
    assert '""C:\\Program Files\\Py\\pythonw.exe""' in content
    assert 'sh.CurrentDirectory = "C:\\work dir"' in content


def test_gateway_vbs_script_pythonpath_chains_runtime_value(monkeypatch):
    """PYTHONPATH chains onto the task env's existing value, like ;%PYTHONPATH%."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (r"C:\v\pythonw.exe", Path(r"C:\v"), [r"C:\v\Lib\site-packages"]),
    )
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\v\python.exe", r"C:\w", r"C:\h", "",
    )
    assert 'existing_pp = env.Item("PYTHONPATH")' in content
    assert "If Len(existing_pp) > 0 Then" in content
    assert r"C:\v\Lib\site-packages" in content


def test_quote_vbs_string_doubles_quotes_and_rejects_newlines():
    assert gateway_windows._quote_vbs_string("plain") == '"plain"'
    assert gateway_windows._quote_vbs_string('a"b') == '"a""b"'
    with pytest.raises(ValueError):
        gateway_windows._quote_vbs_string("line1\nline2")


def test_install_scheduled_task_success_start_now_uses_direct_spawn_not_task_run(monkeypatch, tmp_path, capsys):
    """Install start-now should not /Run the task; that preserved old restart loops."""
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    calls = []

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (True, True))
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script_path)
    monkeypatch.setattr(
        gateway_windows,
        "_install_scheduled_task",
        lambda task_name, script_path: (True, "Created Scheduled Task 'Hermes_Gateway_alice'"),
    )
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: calls.append(("schtasks", tuple(args))) or (0, "", ""))
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))

    gateway_windows.install(force=False)

    assert not any(call[0] == "schtasks" and "/Run" in call[1] for call in calls)
    assert ("spawn", None) in calls
    assert any(call[0] == "report_start" for call in calls)
    out = capsys.readouterr().out
    assert "auto-start installed for Windows login" in out


def test_install_scheduled_task_success_does_not_auto_start(monkeypatch, tmp_path, capsys):
    """Install should register/update the task only; start is explicit."""
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    calls = []

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script_path)
    monkeypatch.setattr(
        gateway_windows,
        "_install_scheduled_task",
        lambda task_name, script_path: (True, "Created Scheduled Task 'Hermes_Gateway_alice'"),
    )
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: calls.append(("schtasks", tuple(args))) or (0, "", ""))
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))

    gateway_windows.install(force=False)

    assert not any(call[0] == "schtasks" and "/Run" in call[1] for call in calls)
    assert not any(call[0] == "spawn" for call in calls)
    assert not any(call[0] == "report_start" for call in calls)
    assert ("next_steps", None) in calls
    out = capsys.readouterr().out
    assert "auto-start installed for Windows login" in out


def test_install_access_denied_launches_elevated_install_before_startup_fallback(monkeypatch, tmp_path, capsys):
    """Non-admin Scheduled Task access denied should hand off to UAC elevation."""
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
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
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: False)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )
    monkeypatch.setattr(setup, "prompt_yes_no", lambda prompt, default=True: calls.append(("prompt", prompt, default)) or True)
    monkeypatch.setattr(gateway_windows, "_install_startup_entry", lambda path: calls.append(("install_startup", path)) or path)
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)

    gateway_windows.install(force=True)

    assert calls == [("prompt", "  Open the UAC prompt now?", False), ("elevate", True, False, True)]
    out = capsys.readouterr().out
    assert "administrator approval" in out
    assert "UAC is Windows' admin approval prompt" in out
    assert "Launched elevated Hermes gateway install prompt" in out


def test_install_prompts_start_choices_before_uac(monkeypatch, tmp_path, capsys):
    """Windows install asks start-now and auto-start before any UAC handoff."""
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    calls = []
    answers = iter([True, True, True])

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
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: False)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda prompt, default=True: calls.append(("prompt", prompt, default)) or next(answers))
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )

    gateway_windows.install(force=False)

    assert calls == [
        ("prompt", "Start the gateway now after install?", True),
        ("prompt", "Start the gateway automatically on Windows login with a Scheduled Task?", True),
        ("prompt", "  Open the UAC prompt now?", False),
        ("elevate", False, True, True),
    ]
    out = capsys.readouterr().out
    assert "elevated install will start the gateway afterwards" in out


def test_install_start_now_without_login_autostart_never_escalates(monkeypatch, capsys):
    """If auto-start is declined, install can start directly without touching schtasks/UAC."""
    calls = []
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (True, False))
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_install_scheduled_task", lambda *args, **kwargs: calls.append(("install_task", args)) or (True, "should not happen"))
    monkeypatch.setattr(gateway_windows, "_launch_elevated_install", lambda *args, **kwargs: calls.append(("elevate", args, kwargs)) or True)

    gateway_windows.install(force=False)

    assert not any(call[0] in {"install_task", "elevate"} for call in calls)
    assert ("spawn", None) in calls
    assert any(call[0] == "report_start" for call in calls)
    out = capsys.readouterr().out
    assert "Skipped Windows login auto-start install" in out


def test_start_noops_when_gateway_already_running(monkeypatch, capsys):
    """Repeated start should not invoke schtasks /Run or spawn another process."""
    calls = []
    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [27128])
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: calls.append("task_check") or True)
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: calls.append(("schtasks", tuple(args))) or (0, "", ""))
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path=None, **_kw: calls.append(("spawn", path)) or 12345)

    gateway_windows.start()

    assert calls == []
    out = capsys.readouterr().out
    assert "already running" in out
    assert "27128" in out


def test_restart_relaunches_manual_gateway_without_persistence(monkeypatch):
    """Restarting a manual gateway must not create Windows login persistence."""
    calls = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(
        gateway_windows,
        "_wait_for_gateway_absent",
        lambda **kwargs: calls.append(("wait_absent", kwargs)) or True,
    )
    monkeypatch.setattr(
        gateway_windows.time,
        "sleep",
        lambda seconds: calls.append(("sleep", seconds)),
    )
    monkeypatch.setattr(
        gateway_windows,
        "_gateway_pids",
        lambda: calls.append("concurrent_check") or [],
    )
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda *_a, **_kw: calls.append("spawn") or 12345,
    )
    monkeypatch.setattr(
        gateway_windows,
        "_report_gateway_start",
        lambda via: calls.append(("report_start", via)),
    )
    monkeypatch.setattr(
        gateway_windows,
        "_wait_for_gateway_ready",
        lambda **kwargs: calls.append(("wait_ready", kwargs)) or [12345],
    )

    monkeypatch.setattr(
        gateway_windows,
        "start",
        lambda: pytest.fail("restart must launch directly, not call start()"),
    )
    monkeypatch.setattr(
        gateway_windows,
        "install",
        lambda *args, **kwargs: pytest.fail("restart must not install persistence"),
    )
    monkeypatch.setattr(
        gateway_windows,
        "is_task_registered",
        lambda: pytest.fail("restart must not query the Scheduled Task"),
    )
    monkeypatch.setattr(
        gateway_windows,
        "is_startup_entry_installed",
        lambda: pytest.fail("restart must not query the Startup folder"),
    )
    monkeypatch.setattr(
        setup,
        "prompt_yes_no",
        lambda *args, **kwargs: pytest.fail("restart must not prompt for install"),
    )

    gateway_windows.restart()

    assert calls == [
        "stop",
        ("wait_absent", {"timeout_s": 30.0}),
        ("sleep", 1.0),
        "concurrent_check",
        "spawn",
        ("report_start", "direct spawn (PID 12345)"),
        ("wait_ready", {"timeout_s": 15.0}),
    ]


def test_restart_does_not_spawn_on_top_of_a_concurrent_starter(monkeypatch, capsys):
    """A gateway raced in during the drain window must not get a twin.

    ``_wait_for_gateway_absent`` already proved the OLD gateway is gone, so a
    live PID at this point is a replacement started by the watchdog or
    laptop-start.ps1. Before this guard, restart() spawned unconditionally and
    the loser of the runtime-lock race exited nonzero -- which reads as a
    failed restart even though the survivor is healthy.
    """
    calls = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_absent", lambda **kwargs: True
    )
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [33333])
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda *a, **k: pytest.fail("must not spawn on top of a live gateway"),
    )
    monkeypatch.setattr(
        gateway_windows,
        "_wait_for_gateway_ready",
        lambda **kwargs: pytest.fail("must return before the readiness wait"),
    )

    gateway_windows.restart()

    assert calls == ["stop"]
    out = capsys.readouterr().out
    assert "33333" in out
    assert "not spawning a second one" in out


def test_force_terminate_survives_a_taskkill_timeout(monkeypatch):
    """A ``TimeoutExpired`` from the kill primitive must not abort the sweep.

    ``subprocess.TimeoutExpired`` is a ``SubprocessError``, NOT an ``OSError``
    — so the ``except OSError`` arm below it never caught this, and a slow kill
    took the whole stop/restart chain down with it (2026-08-11 outage).
    """
    import subprocess

    from gateway import status as status_mod

    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: True)

    def fake_terminate_pid(target_pid, force=False):
        raise subprocess.TimeoutExpired(
            ["taskkill", "/PID", str(target_pid), "/T", "/F"], 10
        )

    monkeypatch.setattr(status_mod, "terminate_pid", fake_terminate_pid)

    # Must not propagate; the sweep reports it killed nothing it could confirm.
    killed = gateway_windows._force_terminate_known_gateway_pids([47264])

    assert killed == 0


def test_restart_still_spawns_when_stop_raises(monkeypatch, capsys):
    """THE regression: a failing ``stop()`` must never skip the relaunch.

    2026-08-11 ~12:00Z — ``taskkill`` exceeded its 10s cap on a loaded box
    (87.8% commit, six concurrent pytest runs). The kill SUCCEEDED, PID 47264
    died, and the ``TimeoutExpired`` then propagated out of ``stop()`` and out
    of ``restart()`` before ``_launch_detached_gateway()`` ran. :8642 was left
    with no listener for ~5 minutes until a manual relaunch.

    A slow-but-successful kill must leave a running gateway behind.
    """
    import subprocess

    calls = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)

    def boom():
        calls.append("stop")
        raise subprocess.TimeoutExpired(["taskkill", "/PID", "47264", "/T", "/F"], 10)

    monkeypatch.setattr(gateway_windows, "stop", boom)
    # The kill landed: nothing is alive afterwards.
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **_kw: True)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_detached_gateway",
        lambda **_kw: calls.append("launch"),
    )
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda **_kw: [4242]
    )

    gateway_windows.restart()

    assert calls == ["stop", "launch"], (
        f"restart() must relaunch even when stop() raises (calls={calls})"
    )


def test_restart_reports_the_stop_error_when_the_relaunch_fails(monkeypatch):
    """Swallowing the stop error is only acceptable if a gateway comes back.

    If the relaunch also fails we are genuinely down, so the original stop
    failure must not be lost — it is the most useful diagnostic.
    """
    import subprocess

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)

    original = subprocess.TimeoutExpired(["taskkill", "/PID", "47264", "/T", "/F"], 10)

    def boom():
        raise original

    monkeypatch.setattr(gateway_windows, "stop", boom)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **_kw: True)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gateway_windows, "_launch_detached_gateway", lambda **_kw: None)
    # Nothing came up.
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **_kw: [])

    with pytest.raises(RuntimeError) as excinfo:
        gateway_windows.restart()

    assert excinfo.value.__cause__ is original, (
        "the stop-phase failure must be chained onto the restart failure"
    )


def test_restart_does_not_spawn_a_duplicate_when_stop_raises_and_pid_survives(
    monkeypatch,
):
    """Resilience must not become recklessness.

    When ``stop()`` fails AND the old gateway is genuinely still alive, the
    pre-existing refusal to start a second gateway on :8642 still applies.
    """
    import subprocess

    calls = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)

    def boom():
        raise subprocess.TimeoutExpired(["taskkill", "/PID", "47264", "/T", "/F"], 10)

    monkeypatch.setattr(gateway_windows, "stop", boom)
    # The old gateway refuses to die.
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **_kw: False)
    monkeypatch.setattr(
        gateway_windows,
        "_force_terminate_known_gateway_pids",
        lambda pids: calls.append("force") or 0,
    )
    monkeypatch.setattr(gateway_windows, "_collect_gateway_stop_pids", lambda *_a: [47264])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_detached_gateway",
        lambda **_kw: calls.append("launch"),
    )

    with pytest.raises(RuntimeError, match="refusing to start a duplicate"):
        gateway_windows.restart()

    assert "launch" not in calls, (
        f"must not spawn on top of a surviving gateway (calls={calls})"
    )


def _arrange_windows_restart_fallthrough(monkeypatch, running_pids, exc=None):
    """Drive ``_gateway_command_inner('restart')`` down the Windows branch.

    ``gateway_windows.restart()`` raises, so the generic recovery path below it
    is reachable. Everything that path could use to tear down / relaunch a
    gateway is stubbed to record instead of act.

    ``exc`` overrides the exception ``restart()`` raises so the caller can
    verify which failure types the recovery arm actually catches.
    """
    calls = []

    monkeypatch.setattr(gateway, "is_windows", lambda: True)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "_dispatch_via_service_manager_if_s6", lambda _a: False)
    monkeypatch.setattr(gateway, "_dispatch_all_via_service_manager_if_s6", lambda _a: False)
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: False)

    def _boom():
        calls.append("windows_restart")
        raise exc or RuntimeError(
            "Gateway restart did not produce a running gateway process."
        )

    monkeypatch.setattr(gateway_windows, "restart", _boom)
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda *a, **k: list(running_pids))
    monkeypatch.setattr(
        gateway,
        "stop_profile_gateway",
        lambda: calls.append("stop_profile_gateway") or True,
    )
    monkeypatch.setattr(
        gateway,
        "_wait_for_gateway_exit",
        lambda **kwargs: calls.append("wait_exit"),
    )
    monkeypatch.setattr(
        gateway,
        "cleanup_gateway_state_files",
        lambda: calls.append("cleanup_state_files") or [],
    )
    monkeypatch.setattr(
        gateway,
        "launch_gateway_detached",
        lambda *a, **k: calls.append("launch_detached") or 4242,
    )
    return calls


def test_windows_restart_failure_does_not_tear_down_a_live_gateway(monkeypatch):
    """A slow-but-healthy restart must not be stopped and relaunched again.

    ``gateway_windows.restart()`` spawns the replacement BEFORE it waits for
    readiness, so a slow boot makes it raise while a healthy gateway is coming
    up. Falling through to the generic recovery path then stops that gateway,
    deletes its lock/pid, and launches a third one -- observed on this machine
    as pairs of detached gateways fighting over :8642.
    """
    calls = _arrange_windows_restart_fallthrough(monkeypatch, running_pids=[10588])

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="restart", all=False, system=False, detached=None)
    )

    assert calls == ["windows_restart"], (
        "a live gateway must be left alone after gateway_windows.restart() raises"
    )


def test_windows_restart_subprocess_timeout_reaches_the_recovery_arm(monkeypatch):
    """A ``TimeoutExpired`` escaping restart() must not bypass recovery.

    The recovery arm caught ``(CalledProcessError, RuntimeError, OSError)``.
    ``TimeoutExpired`` is a *sibling* of ``CalledProcessError`` under
    ``SubprocessError`` — not a subclass — so it sailed past this handler too,
    which is why the 2026-08-11 outage was not caught by the existing net.
    """
    import subprocess

    calls = _arrange_windows_restart_fallthrough(
        monkeypatch,
        running_pids=[],
        exc=subprocess.TimeoutExpired(["taskkill", "/PID", "47264", "/T", "/F"], 10),
    )

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="restart", all=False, system=False, detached=None)
    )

    assert calls == [
        "windows_restart",
        "stop_profile_gateway",
        "wait_exit",
        "cleanup_state_files",
        "launch_detached",
    ]


def test_windows_restart_timeout_leaves_a_surviving_gateway_alone(monkeypatch):
    """The survivor check must also apply to a subprocess timeout."""
    import subprocess

    calls = _arrange_windows_restart_fallthrough(
        monkeypatch,
        running_pids=[10588],
        exc=subprocess.TimeoutExpired(["taskkill", "/PID", "47264", "/T", "/F"], 10),
    )

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="restart", all=False, system=False, detached=None)
    )

    assert calls == ["windows_restart"]


def test_windows_restart_failure_still_recovers_when_nothing_is_running(monkeypatch):
    """The generic fallback must remain reachable when the restart truly failed."""
    calls = _arrange_windows_restart_fallthrough(monkeypatch, running_pids=[])

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="restart", all=False, system=False, detached=None)
    )

    assert calls == [
        "windows_restart",
        "stop_profile_gateway",
        "wait_exit",
        "cleanup_state_files",
        "launch_detached",
    ]


def test_install_startup_fallback_does_not_spawn_when_gateway_already_running(monkeypatch, tmp_path, capsys):
    """Repeated Windows fallback installs should not spawn duplicate gateways."""
    script_path, calls = _arrange_startup_fallback(monkeypatch, tmp_path, [24476])

    gateway_windows.install(force=False)

    assert ("install_startup", script_path) in calls
    assert not any(call[0] == "spawn" for call in calls)
    assert not any(call[0] == "report_start" for call in calls)
    assert ("next_steps", None) in calls
    out = capsys.readouterr().out
    assert "already running" in out
    assert "24476" in out


def test_install_startup_fallback_does_not_auto_spawn_when_gateway_stopped(monkeypatch, tmp_path, capsys):
    """Startup fallback install should only install login item, not launch pythonw."""
    script_path, calls = _arrange_startup_fallback(monkeypatch, tmp_path, [])

    gateway_windows.install(force=False)

    assert ("install_startup", script_path) in calls
    assert not any(call[0] == "spawn" for call in calls)
    assert not any(call[0] == "report_start" for call in calls)
    assert ("next_steps", None) in calls
    out = capsys.readouterr().out
    assert "gateway not started now" in out
    assert "hermes --profile alice gateway start" in out


def test_install_access_denied_declined_elevation_uses_startup_fallback(monkeypatch, tmp_path, capsys):
    """Install should ask before UAC; declining keeps the non-jarring fallback path."""
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
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
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: False)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda prompt, default=True: calls.append(("prompt", prompt, default)) or False)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )
    monkeypatch.setattr(gateway_windows, "_install_startup_entry", lambda path: calls.append(("install_startup", path)) or path)
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile alice")
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))

    gateway_windows.install(force=False)

    assert ("prompt", "  Open the UAC prompt now?", False) in calls
    assert not any(call[0] == "elevate" for call in calls)
    assert ("install_startup", script_path) in calls
    out = capsys.readouterr().out
    assert "Skipped elevation" in out
    assert "UAC is Windows' admin approval prompt" in out


def test_uninstall_access_denied_prompts_before_elevating(monkeypatch, tmp_path, capsys):
    """Uninstall should hand off to an elevated uninstall only after user consent."""
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    startup_entry = tmp_path / "Startup" / "Hermes_Gateway_alice.cmd"

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "get_task_script_path", lambda: script_path)
    monkeypatch.setattr(gateway_windows, "get_startup_entry_path", lambda: startup_entry)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_exec_schtasks",
        lambda args: calls.append(("schtasks", tuple(args))) or (1, "", "ERROR: Access is denied."),
    )
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: False)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda prompt, default=True: calls.append(("prompt", prompt, default)) or True)
    monkeypatch.setattr(gateway_windows, "_launch_elevated_uninstall", lambda: calls.append(("elevate_uninstall", None)) or True)

    gateway_windows.uninstall()

    assert ("prompt", "  Open the UAC prompt now?", False) in calls
    assert ("elevate_uninstall", None) in calls
    out = capsys.readouterr().out
    assert "uninstall needs administrator approval" in out
    assert "UAC is Windows' admin approval prompt" in out
    assert "Launched elevated Hermes gateway uninstall prompt" in out


def test_uninstall_access_denied_declined_keeps_task_and_cleans_files(monkeypatch, tmp_path, capsys):
    """Declining UAC should not surprise the user, but should still remove user-writable artifacts."""
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    startup_entry = tmp_path / "Startup" / "Hermes_Gateway_alice.cmd"
    startup_entry.parent.mkdir(parents=True)
    script_path.write_text("task", encoding="utf-8")
    startup_entry.write_text("startup", encoding="utf-8")

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "get_task_script_path", lambda: script_path)
    monkeypatch.setattr(gateway_windows, "get_startup_entry_path", lambda: startup_entry)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_exec_schtasks",
        lambda args: calls.append(("schtasks", tuple(args))) or (1, "", "ERROR: Access is denied."),
    )
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: False)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda prompt, default=True: calls.append(("prompt", prompt, default)) or False)
    monkeypatch.setattr(gateway_windows, "_launch_elevated_uninstall", lambda: calls.append(("elevate_uninstall", None)) or True)

    gateway_windows.uninstall()

    assert not any(call[0] == "elevate_uninstall" for call in calls)
    assert not script_path.exists()
    assert not startup_entry.exists()
    out = capsys.readouterr().out
    assert "Skipped elevation" in out
    assert "UAC is Windows' admin approval prompt" in out
    assert "Scheduled Task still registered" in out


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


def test_stop_writes_planned_stop_marker_before_killing(monkeypatch):
    """stop() must write the planned-stop marker BEFORE any kill signal.

    Without this, the gateway's drain loop never runs on Windows and
    sessions silently lose context across restarts.
    """
    pid = 99999
    events = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)

    # Stub the marker write so we can record the order of operations.
    from gateway import status as status_mod

    def fake_write_marker(target_pid):
        events.append(("write_marker", target_pid))
        return True

    def fake_pid_exists(check_pid):
        # Drain succeeds: pid "exits" right after the marker write.
        return ("write_marker", pid) not in events

    monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write_marker)
    monkeypatch.setattr(status_mod, "_pid_exists", fake_pid_exists)
    monkeypatch.setattr(status_mod, "get_running_pid", lambda: pid)

    def fake_kill(**kwargs):
        events.append(("kill", kwargs.get("force", False)))
        return 0

    monkeypatch.setattr("hermes_cli.gateway.kill_gateway_processes", fake_kill)
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 5.0)

    gateway_windows.stop()

    # Marker MUST be written before any kill.
    kinds = [e[0] for e in events]
    assert "write_marker" in kinds, "stop() never wrote the planned-stop marker"
    marker_idx = kinds.index("write_marker")
    kill_idx = kinds.index("kill") if "kill" in kinds else len(kinds)
    assert marker_idx < kill_idx, (
        f"stop() killed before writing the marker (events={events})"
    )


def test_stop_waits_for_graceful_drain_before_force_kill(monkeypatch):
    """When drain succeeds, stop() should NOT force-terminate the gateway.

    drained=True means the gateway exited cleanly after seeing the
    marker — escalating to taskkill /F afterwards would be wasted
    work and may emit confusing "killed N processes" output.
    """
    pid = 88888
    events = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])

    from gateway import status as status_mod

    def fake_write_marker(target_pid):
        events.append(("write_marker", target_pid))
        return True

    monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write_marker)

    # Simulate the gateway exiting cleanly after one poll tick.
    poll_count = [0]

    def fake_pid_exists(check_pid):
        poll_count[0] += 1
        return poll_count[0] < 2  # alive on first poll, gone on second

    monkeypatch.setattr(status_mod, "_pid_exists", fake_pid_exists)
    monkeypatch.setattr(status_mod, "get_running_pid", lambda: pid)

    def fake_terminate_pid(target_pid, force=False):
        events.append(("terminate", target_pid, force))

    monkeypatch.setattr(status_mod, "terminate_pid", fake_terminate_pid)
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 5.0)

    gateway_windows.stop()

    assert events == [("write_marker", pid)], (
        f"After clean drain, force termination should be skipped (events={events})"
    )


def test_stop_escalates_to_force_kill_when_drain_times_out(monkeypatch):
    """When drain times out, stop() MUST escalate to force=True.

    Drain timeout = gateway is stuck or unresponsive. Without the
    taskkill /T /F escalation, the gateway stays alive and the next
    `hermes gateway start` fails with "another instance is running".
    """
    pid = 77777
    events = []

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])

    from gateway import status as status_mod
    monkeypatch.setattr(status_mod, "write_planned_stop_marker", lambda p: True)
    monkeypatch.setattr(status_mod, "_pid_exists", lambda check_pid: True)
    monkeypatch.setattr(status_mod, "get_running_pid", lambda: pid)
    monkeypatch.setattr(gateway_windows, "_drain_gateway_pid", lambda *_args: False)

    def fake_terminate_pid(target_pid, force=False):
        events.append(("terminate", target_pid, force))

    monkeypatch.setattr(status_mod, "terminate_pid", fake_terminate_pid)

    gateway_windows.stop()

    assert events == [("terminate", pid, True)], (
        f"After drain timeout, known PID must be force terminated (events={events})"
    )


def test_stop_no_running_gateway_skips_drain(monkeypatch):
    """When no gateway PID file is running, skip drain but clear known strays."""
    events = []
    stray_pid = 42424

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [stray_pid])

    from gateway import status as status_mod
    monkeypatch.setattr(status_mod, "get_running_pid", lambda: None)

    def fake_write_marker(target_pid):
        events.append(("write_marker", target_pid))
        return True
    monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write_marker)
    monkeypatch.setattr(status_mod, "_pid_exists", lambda check_pid: check_pid == stray_pid)

    def fake_terminate_pid(target_pid, force=False):
        events.append(("terminate", target_pid, force))

    monkeypatch.setattr(status_mod, "terminate_pid", fake_terminate_pid)
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 5.0)

    gateway_windows.stop()

    # With no PID to drain, no marker is written. The bounded profile scan can
    # still find and terminate a known stray without falling back to a broad
    # process sweep.
    assert ("write_marker", None) not in events
    assert all(e[0] != "write_marker" for e in events), (
        f"Should not write marker when no PID is running (events={events})"
    )
    assert events == [("terminate", stray_pid, True)]


def test_drain_helper_handles_invalid_pid(monkeypatch):
    """_drain_gateway_pid returns False for invalid PIDs without crashing."""
    assert gateway_windows._drain_gateway_pid(0, 5.0) is False
    assert gateway_windows._drain_gateway_pid(-1, 5.0) is False


def test_drain_helper_returns_true_when_pid_exits_quickly(monkeypatch):
    """_drain_gateway_pid polls _pid_exists until it returns False."""
    pid = 66666
    poll_count = [0]

    def fake_pid_exists(check_pid):
        poll_count[0] += 1
        return poll_count[0] < 3  # alive twice, then gone

    from gateway import status as status_mod
    monkeypatch.setattr(status_mod, "write_planned_stop_marker", lambda p: True)
    monkeypatch.setattr(status_mod, "_pid_exists", fake_pid_exists)

    assert gateway_windows._drain_gateway_pid(pid, drain_timeout=5.0) is True


def test_drain_helper_returns_false_on_timeout(monkeypatch):
    """_drain_gateway_pid returns False when the PID never exits."""
    from gateway import status as status_mod
    monkeypatch.setattr(status_mod, "write_planned_stop_marker", lambda p: True)
    monkeypatch.setattr(status_mod, "_pid_exists", lambda check_pid: True)

    assert gateway_windows._drain_gateway_pid(55555, drain_timeout=1.0) is False


def test_drain_helper_still_waits_if_marker_write_fails(monkeypatch):
    """Marker-write failures are swallowed; drain still polls for PID exit.

    If the marker can't be written (disk full, permission error), the
    gateway can't drain — but the wait still happens so a slow-shutdown
    gateway from a different code path (e.g. SIGTERM working on this
    platform after all) still gets observed cleanly.
    """
    pid = 44444
    def fake_write(target_pid):
        raise OSError("disk full")

    from gateway import status as status_mod
    monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write)
    monkeypatch.setattr(status_mod, "_pid_exists", lambda check_pid: False)

    # Returns True because _pid_exists immediately says "gone".
    assert gateway_windows._drain_gateway_pid(pid, drain_timeout=5.0) is True


# ── spawn label -> boot_reason round trip ────────────────────────────────────
#
# tests/hermes_cli/test_gateway_start_diag_attribution.py already pins that
# `start()` passes "cli:start" and `restart()` passes "cli:restart" by stubbing
# `_spawn_detached`. That leaves the seam untested: the label is only worth
# anything if it survives the real spawn into the child env AND is read back as
# the `boot_reason` an operator sees in ~/.hermes/events/audit.jsonl. Stubbing
# either end would let the two halves drift apart while both files stayed green.


@pytest.mark.parametrize(
    "entry, expected_boot_reason",
    [
        ("start", "cli:start"),
        ("restart", "cli:restart"),
    ],
)
def test_cli_entrypoint_label_survives_into_boot_reason(
    monkeypatch, tmp_path, entry, expected_boot_reason
):
    """A restart must be distinguishable from a plain start in the audit trail.

    Drives the real `_spawn_detached` (only `Popen` is faked) so the assertion
    covers the actual env hand-off, then evaluates `_detect_boot_reason` under
    exactly the environment the child would have inherited.
    """
    import sys as _sys

    from gateway.run import GatewayRunner
    from hermes_cli import gateway_diag

    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(gateway_windows, "is_startup_entry_installed", lambda: False)
    monkeypatch.setattr(gateway_windows, "stop", lambda: None)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **k: True)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **k: True)
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda *_: None)
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

    getattr(gateway_windows, entry)()

    # The producer half: the child really was handed the label.
    child_env = captured["env"]
    assert child_env[gateway_diag.SPAWN_SITE_ENV] == expected_boot_reason

    # The consumer half: that child classifies its own boot from the label.
    monkeypatch.setattr(_sys, "argv", ["hermes", "gateway", "run"])
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, child_env[gateway_diag.SPAWN_SITE_ENV])
    assert GatewayRunner._detect_boot_reason(None) == expected_boot_reason


class TestWindowsStopDrainTimeout:
    """The grace period the stopper gives a gateway before it force-kills it.

    2026-08-17: this was ``min(configured, 30.0)``, which silently halved this
    box's configured 60s drain. The gateway was therefore *structurally*
    guaranteed to be shot mid-drain whenever an in-flight cron needed more
    than 30s — which a live LLM turn routinely does. 3 of 3 teardowns with
    crons in flight died at shutdown phase 1; 0 of 2 without any did.
    """

    @staticmethod
    def _configured(monkeypatch, value):
        monkeypatch.setattr(
            gateway, "_get_restart_drain_timeout", lambda: value, raising=False
        )

    def test_grace_outlasts_the_drain_budget_the_gateway_is_told_it_has(
        self, monkeypatch
    ):
        """A gateway granted less than its own drain budget dies mid-drain.

        ``agent.restart_drain_timeout`` is handed to the gateway's phase-2
        drain (``gateway/run.py``), so a stopper that gives less than that can
        never let the drain finish.
        """
        self._configured(monkeypatch, 60.0)

        assert gateway_windows._windows_stop_drain_timeout() > 60.0

    def test_grace_outlasts_the_internal_shutdown_watchdog_leash(self, monkeypatch):
        """The LOUD killer must win the race against the silent one.

        ``gateway/shutdown_watchdog.py`` force-exits at ``drain + 60s`` after
        logging CRITICAL and writing an all-thread dump. The external
        ``taskkill`` leaves no trace at all. If the stopper fires first the
        watchdog is unreachable and a genuine wedge is undiagnosable — which
        is why "Shutdown watchdog fired" appears zero times on this box.
        """
        from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay

        self._configured(monkeypatch, 60.0)

        assert gateway_windows._windows_stop_drain_timeout() > (
            resolve_shutdown_watchdog_delay(60.0)
        )

    def test_grace_is_still_bounded_for_an_absurd_configured_drain(
        self, monkeypatch
    ):
        """``stop`` must not wedge forever; the ceiling only has to be sane."""
        self._configured(monkeypatch, 86400.0)

        assert gateway_windows._windows_stop_drain_timeout() <= 600.0

    def test_grace_survives_an_unreadable_config(self, monkeypatch):
        """A broken config must not produce a zero-or-negative grace period."""

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(
            gateway, "_get_restart_drain_timeout", _boom, raising=False
        )

        assert gateway_windows._windows_stop_drain_timeout() >= 1.0
