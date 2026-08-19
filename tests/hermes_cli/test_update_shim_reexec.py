"""Regression tests for the Windows console-shim update self-lock.

`venv\\Scripts\\hermes.exe` is a PE launcher that runs the managed-runtime
interpreter with the shim *itself* as its script/zipapp. The interpreter
keeps that file open without FILE_SHARE_DELETE for the whole process
lifetime, so an update launched as `hermes update` locks the very file uv
must replace (os error 32, quarantine rename denied). The updater must
re-exec itself via `venv\\Scripts\\python.exe -m hermes_cli.main` so no
process maps the shim while it is being rewritten.

Detection must recognise every launch variant: sys.argv[0] as the shim
path, the shim's `__main__.py` (runpy/`-m` zipapp launches), and the
`__main__.__module__.__spec__.origin` path.
"""

import pytest

from hermes_cli import update_cmd


class _FakeMain:
    def __init__(self, scripts_dir):
        self._scripts_dir = scripts_dir

    def _venv_scripts_dir(self):
        return self._scripts_dir


def _patch_base(monkeypatch, tmp_path, argv0):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"fake")
    monkeypatch.setattr(update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(update_cmd.sys, "argv", [str(argv0), "update", "--yes"])
    monkeypatch.delenv(update_cmd._REEXEC_ENV, raising=False)
    monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain(scripts))
    return scripts


def _install_popen_capture(monkeypatch):
    captured = {}

    def fake_popen(cmd, env=None):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env or {})
        return object()

    monkeypatch.setattr(update_cmd.subprocess, "Popen", fake_popen)
    return captured


@pytest.mark.parametrize(
    "shim_name",
    ["hermes.exe", "hermes-acp.exe", "hermes-agent.exe", "hermes-gateway.exe"],
)
def test_reexec_fires_when_arg0_is_shim(monkeypatch, tmp_path, shim_name):
    scripts = _patch_base(monkeypatch, tmp_path, tmp_path / "Scripts" / shim_name)
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is True
    assert captured["cmd"][0] == str(scripts / "python.exe")
    assert captured["cmd"][1:3] == ["-m", "hermes_cli.main"]
    assert captured["cmd"][3:] == ["update", "--yes"]
    assert captured["env"][update_cmd._REEXEC_ENV] == "1"


def test_reexec_fires_when_arg0_is_shim_main_py(monkeypatch, tmp_path):
    """Zipapp/`-m` launches put the shim's __main__.py into sys.argv[0]."""
    shim = tmp_path / "Scripts" / "hermes.exe"
    _patch_base(monkeypatch, tmp_path, shim / "__main__.py")
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is True
    assert captured["cmd"][3:] == ["update", "--yes"]


def test_reexec_fires_via_main_module_file(monkeypatch, tmp_path):
    """When argv[0] is opaque, __main__.__file__ still reveals the shim."""
    import __main__ as main_mod

    shim = tmp_path / "Scripts" / "hermes.exe"
    scripts = _patch_base(monkeypatch, tmp_path, tmp_path / "opaque" / "launcher.exe")
    monkeypatch.setattr(main_mod, "__file__", str(shim / "__main__.py"))
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is True
    assert captured["cmd"][3:] == ["update", "--yes"]
    monkeypatch.undo()


def test_reexec_fires_via_spec_origin(monkeypatch, tmp_path):
    """When argv[0] is opaque, the module spec origin still reveals the shim."""
    import __main__ as main_mod

    shim = tmp_path / "Scripts" / "hermes.exe"
    _patch_base(monkeypatch, tmp_path, tmp_path / "opaque" / "launcher.exe")
    monkeypatch.setattr(main_mod, "__file__", None)

    class _Spec:
        origin = str(shim)

    monkeypatch.setattr(main_mod, "__spec__", _Spec())
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is True
    assert captured["cmd"][3:] == ["update", "--yes"]
    monkeypatch.undo()


def test_reexec_skipped_when_marker_already_set(monkeypatch, tmp_path):
    _patch_base(monkeypatch, tmp_path, tmp_path / "Scripts" / "hermes.exe")
    monkeypatch.setenv(update_cmd._REEXEC_ENV, "1")
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is False
    assert "cmd" not in captured


def test_reexec_skipped_when_not_launched_from_shim(monkeypatch, tmp_path):
    _patch_base(monkeypatch, tmp_path, tmp_path / "Scripts" / "python.exe")
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is False
    assert "cmd" not in captured


def test_reexec_skipped_off_windows(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")
    monkeypatch.setattr(
        update_cmd.sys, "argv", [str(scripts / "hermes.exe"), "update"]
    )
    monkeypatch.delenv(update_cmd._REEXEC_ENV, raising=False)
    monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain(scripts))
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is False
    assert "cmd" not in captured


def test_reexec_skipped_when_scripts_dir_unresolved(monkeypatch, tmp_path):
    monkeypatch.setattr(update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(
        update_cmd.sys,
        "argv",
        [str(tmp_path / "Scripts" / "hermes.exe"), "update"],
    )
    monkeypatch.delenv(update_cmd._REEXEC_ENV, raising=False)
    monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain(None))
    captured = _install_popen_capture(monkeypatch)

    assert update_cmd._maybe_reexec_windows_update_from_shim() is False
    assert "cmd" not in captured