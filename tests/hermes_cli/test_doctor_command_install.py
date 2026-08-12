"""Tests for the Command Installation check in hermes doctor."""

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

import hermes_cli.doctor as doctor_mod

# Importing this module also warms ``run_doctor``'s two heaviest lazy imports
# at collection time, which is what keeps the tests below under the repo's 30s
# ``--timeout`` cap. See ``conftest_doctor_externals`` for the measurements.
from tests.hermes_cli.conftest_doctor_externals import (  # noqa: E402
    stub_doctor_externals,
)


@pytest.fixture(autouse=True)
def _stub_doctor_externals(monkeypatch):
    """Keep ``run_doctor`` off ``install_doctor.probe`` / ``gh auth status`` /
    ``agent-browser --version`` -- ~31s of host subprocesses per call that none
    of these tests assert on. See ``conftest_doctor_externals`` for the
    measurements and the rationale."""
    stub_doctor_externals(monkeypatch)


def _setup_doctor_env(monkeypatch, tmp_path, venv_name="venv"):
    """Create a minimal HERMES_HOME + PROJECT_ROOT for doctor tests."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    # Create a fake venv entry point
    venv_bin_dir = project / venv_name / "bin"
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    hermes_bin = venv_bin_dir / "hermes"
    hermes_bin.write_text("#!/usr/bin/env python\n# entry point\n")
    hermes_bin.chmod(0o755)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    # Stub model_tools so doctor doesn't fail on import
    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    # Stub auth checks
    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    except Exception:
        pass

    # Stub httpx.get to avoid network calls
    try:
        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
    except Exception:
        pass

    return home, project, hermes_bin


def _run_doctor(fix=False):
    """Run doctor and capture stdout."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=fix))
    return buf.getvalue()


class TestEditableInstallCmd:
    """``_editable_install_cmd`` — the entry-point remedy's command builder.

    These run on every platform on purpose. The section that uses it is
    Unix-only, so every ``run_doctor`` test around it is skipped on Windows —
    which is exactly how a Windows-authored change could regress it unseen.
    """

    def test_uses_uv_with_an_explicit_python_when_the_venv_has_no_pip(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(install_doctor.sys, "executable", "/proj/.venv/bin/python")

        assert doctor_mod._editable_install_cmd("-e '.[all]'") == (
            "/usr/bin/uv pip install -e '.[all]' --python /proj/.venv/bin/python"
        )

    def test_uses_pip_when_the_interpreter_has_one(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: True)
        monkeypatch.setattr(install_doctor.sys, "executable", "/proj/venv/bin/python")

        assert doctor_mod._editable_install_cmd("-e '.[all]'") == (
            "/proj/venv/bin/python -m pip install -e '.[all]'"
        )

    def test_falls_back_to_the_platform_hint_if_install_doctor_is_unimportable(
        self, monkeypatch
    ):
        """A failure in the sibling module must not take down this section."""
        monkeypatch.setitem(sys.modules, "hermes_cli.install_doctor", None)

        cmd = doctor_mod._editable_install_cmd("-e '.[all]'")

        assert cmd.endswith("-e '.[all]'")
        assert cmd.startswith(doctor_mod._python_install_cmd())


class TestDoctorCommandInstallation:
    """Tests for the ◆ Command Installation section."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_correct_symlink_shows_ok(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        # Create the command link dir with correct symlink
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        cmd_link.symlink_to(hermes_bin)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "Venv entry point exists" in out
        assert "correct target" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_missing_symlink_shows_fail(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Don't create the symlink — it should be missing

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "Venv entry point exists" in out
        assert "not found" in out
        assert "hermes doctor --fix" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_fix_creates_missing_symlink(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=True)
        assert "Command Installation" in out
        assert "Created symlink" in out

        # Verify the symlink was actually created
        cmd_link = tmp_path / ".local" / "bin" / "hermes"
        assert cmd_link.is_symlink()
        assert cmd_link.resolve() == hermes_bin.resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_wrong_target_symlink_shows_warn(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        # Create a symlink pointing to the wrong target
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        wrong_target = tmp_path / "wrong_hermes"
        wrong_target.write_text("#!/usr/bin/env python\n")
        cmd_link.symlink_to(wrong_target)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "wrong target" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_fix_repairs_wrong_symlink(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        # Create a symlink pointing to wrong target
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        wrong_target = tmp_path / "wrong_hermes"
        wrong_target.write_text("#!/usr/bin/env python\n")
        cmd_link.symlink_to(wrong_target)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=True)
        assert "Fixed symlink" in out

        # Verify the symlink now points to the correct target
        assert cmd_link.is_symlink()
        assert cmd_link.resolve() == hermes_bin.resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_missing_venv_entry_point_shows_warn(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        # Do NOT create any venv entry point

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
        try:
            from hermes_cli import auth as _auth_mod
            monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        except Exception:
            pass
        try:
            import httpx
            monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
        except Exception:
            pass

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "Venv entry point not found" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_dot_venv_dir_is_found(self, monkeypatch, tmp_path):
        """The check finds entry points in .venv/ as well as venv/."""
        home, project, _ = _setup_doctor_env(monkeypatch, tmp_path, venv_name=".venv")

        # Create the command link with correct symlink
        hermes_bin = project / ".venv" / "bin" / "hermes"
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        cmd_link.symlink_to(hermes_bin)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Venv entry point exists" in out
        assert ".venv/bin/hermes" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_non_symlink_regular_file_shows_ok(self, monkeypatch, tmp_path):
        """If ~/.local/bin/hermes is a regular file (not symlink), accept it."""
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        cmd_link.write_text("#!/bin/sh\nexec python -m hermes_cli.main \"$@\"\n")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "non-symlink" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_termux_uses_prefix_bin(self, monkeypatch, tmp_path):
        """On Termux, the command link dir is $PREFIX/bin."""
        prefix_dir = tmp_path / "termux_prefix"
        prefix_bin = prefix_dir / "bin"
        prefix_bin.mkdir(parents=True)

        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
        monkeypatch.setenv("PREFIX", str(prefix_dir))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "$PREFIX/bin" in out

    def test_missing_entry_point_remedy_is_runnable_and_drops_the_dead_activate(
        self, monkeypatch, tmp_path
    ):
        """The printed remedy must be a command that can actually run.

        It used to be ``cd <root> && source venv/bin/activate && pip install
        -e '.[all]'``, which is wrong twice over: the activate script cannot
        exist (this branch is only reached when neither venv/ nor .venv/ holds
        an entry point), and a uv-created venv has no pip to run afterwards.

        Deliberately NOT skipped on Windows. The section is Unix-only, so
        every other test of it skips there — which would leave the branch this
        pins unexecuted on the machine where the defect was found. It forces
        ``sys.platform`` instead, mirroring ``test_windows_skips_check``, which
        already forces the opposite direction. Nothing in this branch touches a
        POSIX-only primitive: it stats two paths and formats two strings.
        """
        monkeypatch.setattr(sys, "platform", "linux")
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)  # no venv/ and no .venv/ inside

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            doctor_mod, "_editable_install_cmd", lambda spec: f"<detected> {spec}"
        )

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        out = _run_doctor(fix=False)

        assert "Venv entry point not found" in out
        assert "<detected> -e '.[all]'" in out
        assert "source venv/bin/activate" not in out

    def test_windows_skips_check(self, monkeypatch, tmp_path):
        """On Windows, the Command Installation section is skipped."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(sys, "platform", "win32")

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
        try:
            from hermes_cli import auth as _auth_mod
            monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        except Exception:
            pass
        try:
            import httpx
            monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
        except Exception:
            pass

        out = _run_doctor(fix=False)
        assert "Command Installation" not in out
