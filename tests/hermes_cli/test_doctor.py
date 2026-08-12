"""Tests for hermes_cli.doctor."""

import os
import sys
import types
import io
import json
import contextlib
import subprocess
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.doctor as doctor
import hermes_cli.gateway as gateway_cli
from hermes_cli import doctor as doctor_mod
from hermes_cli.doctor import _has_provider_env_config

# ``run_doctor`` imports ``model_tools`` lazily, and importing it runs
# ``discover_builtin_tools()`` -- the whole builtin tool registry, which
# transitively pulls in asyncio/websockets/openai and constructs the
# module-scope ``tools.process_registry.ProcessRegistry`` singleton. Cold on
# Windows that single import measures ~66s (`python -X importtime -c "import
# model_tools"`), and warm it is still ~13s.
#
# It is a session-shared cost, but a lazy import inside ``run_doctor`` charges
# all of it to whichever test happens to call ``run_doctor`` first -- which
# pushed that one test past the repo's 30s `--timeout` cap whenever this file
# is run on its own. Importing it here moves the cost into module collection,
# which pytest-timeout does not cover, so no single test is billed for it.
# Ten other test modules already import model_tools at module scope for the
# same reason.
import model_tools  # noqa: F401,E402


def _fake_install_probe(names, entrypoints, python=None, env=None):
    """Stand in for ``install_doctor.probe`` without spawning an interpreter.

    The real probe launches ``sys.executable -c <script>`` which imports every
    declared package from a neutral cwd. On this host that single spawn costs
    ~16s per call, and ``run_doctor`` calls it once per invocation -- it was
    the largest slice of the ~60s each ``run_doctor`` test used to take. It
    also makes the result depend on whatever happens to be installed in the
    developer's venv, so the doctor section it feeds was never deterministic.

    Returning an all-clean result keeps the real ``_collect``/``analyze``/
    render path under test and only removes the subprocess.
    """
    return {
        "resolved": {n: {"ok": True, "origin": f"<stub>/{n}", "error": None} for n in names},
        "imports": {n: {"ok": True, "error": None} for n in entrypoints},
        "executable": sys.executable,
    }


def _fast_agent_browser_runnable(path):
    """``hermes_constants.agent_browser_runnable`` minus the ``--version`` spawn.

    The real helper execs the resolved binary to reject dangling symlinks;
    on Windows that npm ``.CMD`` shim costs ~4s per call. The cheap checks it
    performs first (npx form, exists, executable) are kept verbatim so the
    dangling-symlink semantics the callers rely on still hold.
    """
    if not path:
        return False
    if " " in path and path.split()[0].endswith("npx"):
        return True
    return os.path.exists(path) and os.access(path, os.X_OK)


@pytest.fixture(autouse=True)
def _stub_doctor_externals(request, monkeypatch):
    """Keep ``run_doctor`` off the three slowest host probes.

    Measured on Windows, each ``run_doctor(...)`` test spent ~31s of its ~39s
    inside three subprocesses that none of these tests assert on:

      * ``install_doctor.probe``            ~16.6s
      * ``gh auth status --json ...``       ~10.7s
      * ``agent-browser --version``          ~3.9s

    That put every one of them over the repo's 30s ``--timeout`` cap from
    ``pyproject.toml``, so they could only pass under an explicit
    ``--timeout=600`` -- i.e. they were silently red in a default run. The
    probes are also host state, not behaviour under test, so stubbing them
    makes the tests deterministic as well as fast.

    Classes that deliberately exercise the real gh probe opt out by setting
    ``exercises_real_gh_probe = True`` (see ``TestGitHubTokenCheck``).
    """
    from hermes_cli import install_doctor as _install_doctor

    _real_section_lines = _install_doctor.doctor_section_lines

    def _stubbed_section_lines(probe_fn=None, root=None):
        return _real_section_lines(probe_fn=_fake_install_probe, root=root)

    monkeypatch.setattr(_install_doctor, "doctor_section_lines", _stubbed_section_lines)
    monkeypatch.setattr(doctor_mod, "agent_browser_runnable", _fast_agent_browser_runnable)

    if not getattr(request.instance, "exercises_real_gh_probe", False):
        monkeypatch.setattr(doctor_mod, "_gh_authenticated", lambda: False)


class TestDoctorPlatformHints:
    def test_termux_package_hint(self, monkeypatch):
        monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
        monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
        assert doctor._is_termux() is True
        assert doctor._python_install_cmd() == "python -m pip install"
        assert doctor._system_package_install_cmd("ripgrep") == "pkg install ripgrep"

    def test_non_termux_package_hint_defaults_to_apt(self, monkeypatch):
        monkeypatch.delenv("TERMUX_VERSION", raising=False)
        monkeypatch.setenv("PREFIX", "/usr")
        monkeypatch.setattr(sys, "platform", "linux")
        assert doctor._is_termux() is False
        assert doctor._python_install_cmd() == "uv pip install"
        assert doctor._system_package_install_cmd("ripgrep") == "sudo apt install ripgrep"


class TestInstallIntegrityRemediationIsRunnable:
    """The Install Integrity remediation doctor prints must be executable here.

    Regression for 2026-08-12: on a uv-created ``.venv`` (uv installs no pip
    into the environments it builds) doctor printed
    ``run `pip install -e . --no-deps` `` — which died on "No module named
    pip" the moment anyone pasted it. The command form is now detected from
    the environment in ``install_doctor.reinstall_command``; this pins that
    the detected form is what reaches doctor's summary block, since doctor
    is the surface a user actually reads.
    """

    def _drifted_section(self, monkeypatch, *, has_pip):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: has_pip)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(install_doctor.sys, "executable", "/agent-src/.venv/bin/python")

        root = install_doctor.InstallRoot(
            path=Path("/agent-src"), provenance="test", mapping={"events": "x"}
        )
        probe_result = {
            "executable": "/agent-src/.venv/bin/python",
            "resolved": {
                "events": {"ok": True, "origin": "/agent-src/events", "error": None},
                "jobflow_dispatch": {"ok": False, "origin": None, "error": "not found"},
            },
            "imports": {},
        }
        findings = install_doctor.analyze(
            {"events", "jobflow_dispatch"}, probe_result, root
        )
        assert findings.ok is False  # the branch under test only runs on drift
        return install_doctor._remediation_one_liner(root, findings.finder_is_stale)

    def test_uv_environment_gets_the_uv_form_with_an_explicit_python(self, monkeypatch):
        remediation = self._drifted_section(monkeypatch, has_pip=False)

        assert (
            "uv pip install -e . --no-deps --python /agent-src/.venv/bin/python"
            in remediation
        )
        # The old, unrunnable text: a bare pip invocation with no interpreter.
        assert "run `pip install -e . --no-deps`" not in remediation

    def test_pip_environment_still_gets_the_pip_form(self, monkeypatch):
        """Hardcoding uv would be the same bug mirrored onto a pip venv."""
        remediation = self._drifted_section(monkeypatch, has_pip=True)

        assert "/agent-src/.venv/bin/python -m pip install -e . --no-deps" in remediation
        assert "uv pip install" not in remediation


class TestProviderEnvDetection:
    def test_detects_openai_api_key(self):
        content = "OPENAI_BASE_URL=http://localhost:1234/v1\nOPENAI_API_KEY=***"
        assert _has_provider_env_config(content)

    def test_detects_custom_endpoint_without_openrouter_key(self):
        content = "OPENAI_BASE_URL=http://localhost:8080/v1\n"
        assert _has_provider_env_config(content)

    def test_detects_kimi_cn_api_key(self):
        content = "KIMI_CN_API_KEY=sk-test\n"
        assert _has_provider_env_config(content)

    def test_returns_false_when_no_provider_settings(self):
        content = "TERMINAL_ENV=local\n"
        assert not _has_provider_env_config(content)


class TestDoctorToolAvailabilitySummary:
    def test_missing_api_key_summary_ignores_disabled_toolsets(self, monkeypatch):
        unavailable = [
            {"name": "rl", "missing_vars": ["TINKER_API_KEY"]},
            {"name": "web", "missing_vars": ["EXA_API_KEY"]},
        ]
        monkeypatch.setattr(doctor, "_enabled_cli_toolsets_for_doctor", lambda: {"web"})

        filtered = doctor._missing_api_key_toolsets_for_summary(unavailable)

        assert [item["name"] for item in filtered] == ["web"]

    def test_missing_api_key_summary_falls_back_when_config_unavailable(self, monkeypatch):
        unavailable = [
            {"name": "rl", "missing_vars": ["TINKER_API_KEY"]},
            {"name": "web", "missing_vars": ["EXA_API_KEY"]},
        ]
        monkeypatch.setattr(doctor, "_enabled_cli_toolsets_for_doctor", lambda: None)

        filtered = doctor._missing_api_key_toolsets_for_summary(unavailable)

        assert [item["name"] for item in filtered] == ["rl", "web"]


class TestDoctorEnvFileEncoding:
    """Regression for #18637 (bug 3): `hermes doctor` crashed on Windows
    Chinese locale (GBK) because `.env` was read with Path.read_text() which
    defaults to the system locale encoding, not UTF-8."""

    def test_doctor_reads_env_as_utf8_even_when_locale_is_not_utf8(
        self, monkeypatch, tmp_path
    ):
        import pathlib

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        # Write a UTF-8 .env containing an em dash (U+2014 = e2 80 94). The
        # 0x94 byte is exactly the one the issue reporter hit: it's invalid
        # as a GBK trailing byte in this position, so locale-default reads
        # raise UnicodeDecodeError on Chinese Windows.
        env_path = hermes_home / ".env"
        env_path.write_text(
            "OPENAI_API_KEY=sk-test  # em-dash here — should not crash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)

        orig_read_text = pathlib.Path.read_text

        def gbk_like_read_text(self, encoding=None, errors=None, **kwargs):
            # Simulate a GBK locale: refuse to decode this specific UTF-8
            # .env unless the caller pins encoding="utf-8".
            if self == env_path and encoding != "utf-8":
                raise UnicodeDecodeError(
                    "gbk", b"\x94", 0, 1, "illegal multibyte sequence"
                )
            return orig_read_text(self, encoding=encoding, errors=errors, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", gbk_like_read_text)

        # Short-circuit the expensive tool-availability probe — we only
        # need doctor to reach the .env read without crashing.
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        # Run doctor. If the .env read still uses locale encoding, this
        # raises UnicodeDecodeError and the test fails.
        with pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=False))


class TestDoctorToolAvailabilityOverrides:
    def test_marks_honcho_available_when_configured(self, monkeypatch):
        monkeypatch.setattr(doctor, "_honcho_is_configured_for_doctor", lambda: True)

        available, unavailable = doctor._apply_doctor_tool_availability_overrides(
            [],
            [{"name": "honcho", "env_vars": [], "tools": ["query_user_context"]}],
        )

        assert available == ["honcho"]
        assert unavailable == []

    def test_leaves_honcho_unavailable_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(doctor, "_honcho_is_configured_for_doctor", lambda: False)

        honcho_entry = {"name": "honcho", "env_vars": [], "tools": ["query_user_context"]}
        available, unavailable = doctor._apply_doctor_tool_availability_overrides(
            [],
            [honcho_entry],
        )

        assert available == []
        assert unavailable == [honcho_entry]

    def test_marks_kanban_available_only_when_missing_worker_env_gate(self, monkeypatch):
        monkeypatch.setattr(doctor, "_honcho_is_configured_for_doctor", lambda: False)
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        available, unavailable = doctor._apply_doctor_tool_availability_overrides(
            [],
            [{"name": "kanban", "env_vars": [], "tools": ["kanban_show"]}],
        )

        assert available == ["kanban"]
        assert unavailable == []

    def test_leaves_kanban_unavailable_when_worker_env_is_set(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "probe")
        kanban_entry = {"name": "kanban", "env_vars": [], "tools": ["kanban_show"]}

        available, unavailable = doctor._apply_doctor_tool_availability_overrides(
            [],
            [kanban_entry],
        )

        assert available == []
        assert unavailable == [kanban_entry]

    def test_leaves_non_worker_kanban_failure_unavailable(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        kanban_entry = {"name": "kanban", "env_vars": [], "tools": ["kanban_show", "not_a_kanban_tool"]}

        available, unavailable = doctor._apply_doctor_tool_availability_overrides(
            [],
            [kanban_entry],
        )

        assert available == []
        assert unavailable == [kanban_entry]

    def test_kanban_doctor_detail_explains_worker_gate(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        assert doctor._doctor_tool_availability_detail("kanban") == "(runtime-gated; loaded only for dispatcher-spawned workers)"


class TestHonchoDoctorConfigDetection:
    def test_reports_configured_when_enabled_with_api_key(self, monkeypatch):
        fake_config = SimpleNamespace(enabled=True, api_key="***")

        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda: fake_config,
        )

        assert doctor._honcho_is_configured_for_doctor()

    def test_reports_not_configured_without_api_key(self, monkeypatch):
        fake_config = SimpleNamespace(enabled=True, api_key="")

        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda: fake_config,
        )

        assert not doctor._honcho_is_configured_for_doctor()


def test_run_doctor_sets_interactive_env_for_tool_checks(monkeypatch, tmp_path):
    """Doctor should present CLI-gated tools as available in CLI context."""
    project_root = tmp_path / "project"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()

    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    seen = {}

    def fake_check_tool_availability(*args, **kwargs):
        seen["interactive"] = os.getenv("HERMES_INTERACTIVE")
        raise SystemExit(0)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=fake_check_tool_availability,
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    with pytest.raises(SystemExit):
        doctor_mod.run_doctor(Namespace(fix=False))

    assert seen["interactive"] == "1"


def test_check_gateway_service_linger_warns_when_disabled(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n")

    monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(gateway_cli, "get_systemd_linger_status", lambda: (False, ""))

    issues = []
    doctor._check_gateway_service_linger(issues)

    out = capsys.readouterr().out
    assert "Gateway Service" in out
    assert "Systemd linger disabled" in out
    assert "loginctl enable-linger" in out
    assert issues == [
        "Enable linger for the gateway user service: sudo loginctl enable-linger $USER"
    ]


def test_check_gateway_service_linger_skips_when_service_not_installed(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "missing.service"

    monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda: unit_path)

    issues = []
    doctor._check_gateway_service_linger(issues)

    out = capsys.readouterr().out
    assert out == ""
    assert issues == []


# ── Memory provider section (doctor should only check the *active* provider) ──


class TestDoctorMemoryProviderSection:
    """The ◆ Memory Provider section should respect memory.provider config."""

    def _make_hermes_home(self, tmp_path, provider=""):
        """Create a minimal HERMES_HOME with config.yaml."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        import yaml
        config = {"memory": {"provider": provider}} if provider else {"memory": {}}
        (home / "config.yaml").write_text(yaml.dump(config))
        return home

    def _run_doctor_and_capture(self, monkeypatch, tmp_path, provider=""):
        """Run doctor and capture stdout."""
        home = self._make_hermes_home(tmp_path, provider)
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        (tmp_path / "project").mkdir(exist_ok=True)

        # Stub tool availability (returns empty) so doctor runs past it
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        # Stub auth checks to avoid real API calls
        try:
            from hermes_cli import auth as _auth_mod
            monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
        except Exception:
            pass

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue()

    def test_no_provider_shows_builtin_ok(self, monkeypatch, tmp_path):
        out = self._run_doctor_and_capture(monkeypatch, tmp_path, provider="")
        assert "Memory Provider" in out
        assert "Built-in memory active" in out
        # Should NOT mention Honcho or Mem0 errors
        assert "Honcho API key" not in out
        assert "Mem0" not in out

    def test_honcho_provider_not_installed_shows_fail(self, monkeypatch, tmp_path):
        # Make honcho import fail
        monkeypatch.setitem(
            sys.modules, "plugins.memory.honcho.client", None
        )
        out = self._run_doctor_and_capture(monkeypatch, tmp_path, provider="honcho")
        assert "Memory Provider" in out
        # Should show failure since honcho is set but not importable
        assert "Built-in memory active" not in out

    def test_mem0_provider_not_installed_shows_fail(self, monkeypatch, tmp_path):
        # Make mem0 import fail
        monkeypatch.setitem(sys.modules, "plugins.memory.mem0", None)
        out = self._run_doctor_and_capture(monkeypatch, tmp_path, provider="mem0")
        assert "Memory Provider" in out
        assert "Built-in memory active" not in out


def test_run_doctor_termux_treats_docker_and_browser_warnings_as_expected(monkeypatch, tmp_path):
    helper = TestDoctorMemoryProviderSection()
    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")

    real_which = doctor_mod.shutil.which

    def fake_which(cmd):
        if cmd in {"docker", "node", "npm"}:
            return None
        return real_which(cmd)

    monkeypatch.setattr(doctor_mod.shutil, "which", fake_which)

    out = helper._run_doctor_and_capture(monkeypatch, tmp_path, provider="")

    assert "Docker backend is not available inside Termux" in out
    assert "Node.js not found (browser tools are optional in the tested Termux path)" in out
    assert "Install Node.js on Termux with: pkg install nodejs" in out
    assert "Termux browser setup:" in out
    assert "1) pkg install nodejs" in out
    assert "2) npm install -g agent-browser" in out
    assert "3) agent-browser install" in out
    assert "Termux compatibility fallbacks:" in out
    assert "use .[termux-all] for broad compatibility" in out
    assert "Matrix E2EE extra is excluded on Termux" in out
    assert "Local faster-whisper extra is excluded on Termux" in out
    assert "STT fallback: use Groq Whisper (set GROQ_API_KEY) or OpenAI Whisper (set VOICE_TOOLS_OPENAI_KEY)." in out
    assert "docker not found (optional)" not in out


def test_run_doctor_accepts_named_provider_from_providers_section(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)

    import yaml

    (home / "config.yaml").write_text(
        yaml.dump(
            {
                "model": {
                    "provider": "volcengine-plan",
                    "default": "doubao-seed-2.0-code",
                },
                "providers": {
                    "volcengine-plan": {
                        "name": "volcengine-plan",
                        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
                        "default_model": "doubao-seed-2.0-code",
                        "models": {"doubao-seed-2.0-code": {}},
                    }
                },
            }
        )
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'volcengine-plan' is not a recognised provider" not in out


def test_run_doctor_accepts_bare_custom_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: local-model\n"
        "  base_url: http://localhost:8000/v1\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'custom' is not a recognised provider" not in out


def test_run_doctor_flags_missing_credentials_for_active_openrouter_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-4.1-mini\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    try:
        from hermes_cli import auth as _auth_mod

        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'openrouter' is set but no API key is configured" in out
    assert "No credentials found for provider 'openrouter'." in out


@pytest.mark.parametrize(
    ("provider", "default_model"),
    [
        ("opencode-zen", "anthropic/claude-sonnet-4.6"),
        ("kilocode", "anthropic/claude-sonnet-4.6"),
        ("kimi-coding", "kimi-k2"),
        ("nvidia", "qwen/qwen3.5-122b-a10b"),
    ],
)
def test_run_doctor_accepts_hermes_provider_ids_that_catalog_aliases(
    monkeypatch, tmp_path, provider, default_model
):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        f"  provider: {provider}\n"
        f"  default: {default_model}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert f"model.provider '{provider}' is not a recognised provider" not in out
    assert f"model.provider '{provider}' is unknown" not in out
    if provider in {"opencode-zen", "kilocode", "nvidia"}:
        assert (
            f"model.default '{default_model}' uses a vendor/model slug but provider is '{provider}'"
            not in out
        )


def test_run_doctor_accepts_vendor_slugs_for_named_custom_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:hpc-ai\n"
        "  default: deepseek/deepseek-v4-flash\n"
        "custom_providers:\n"
        "  - name: hpc-ai\n"
        "    base_url: https://hpc-ai.example/v1\n"
        "    api_key: test-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'custom:hpc-ai' is not a recognised provider" not in out
    assert "model.provider 'custom:hpc-ai' is unknown" not in out
    assert (
        "model.default 'deepseek/deepseek-v4-flash' uses a vendor/model slug but provider is "
        "'custom:hpc-ai'"
        not in out
    )
    assert "Either set model.provider to 'openrouter', or drop the vendor prefix." not in out




def test_run_doctor_accepts_kimi_coding_cn_provider(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("KIMI_CN_API_KEY=***\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: kimi-coding-cn\n"
        "  default: kimi-k2.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    (tmp_path / "project").mkdir(exist_ok=True)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_auth_status", lambda provider: {"logged_in": True})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))

    out = buf.getvalue()
    assert "model.provider 'kimi-coding-cn' is not a recognised provider" not in out


def test_run_doctor_termux_does_not_mark_browser_available_without_agent_browser(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda cmd: "/data/data/com.termux/files/usr/bin/node" if cmd in {"node", "npm"} else None)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: (["terminal"], [{"name": "browser", "env_vars": [], "tools": ["browser_navigate"]}]),
        TOOLSET_REQUIREMENTS={
            "terminal": {"name": "terminal"},
            "browser": {"name": "browser"},
        },
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "✓ browser" not in out
    assert "browser" in out
    assert "system dependency not met" in out
    assert "agent-browser is not installed (expected in the tested Termux path)" in out
    assert "npm install -g agent-browser && agent-browser install" in out


def test_run_doctor_kimi_cn_env_is_detected_and_probe_is_null_safe(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("KIMI_CN_API_KEY=sk-test\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("KIMI_CN_API_KEY", "sk-test")

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return types.SimpleNamespace(status_code=200)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "API key or custom endpoint configured" in out
    assert "Kimi / Moonshot (China)" in out
    assert "str expected, not NoneType" not in out
    assert any(url == "https://api.moonshot.cn/v1/models" for url, _, _ in calls)


def test_run_doctor_dashscope_retries_china_endpoint_after_intl_unauthorized(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("DASHSCOPE_API_KEY=sk-test\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except ImportError:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        status = 200 if "dashscope.aliyuncs.com" in url else 401
        return types.SimpleNamespace(status_code=status)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Alibaba/DashScope" in out
    assert "invalid API key" not in out
    assert any(
        url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models"
        for url, _, _ in calls
    )
    assert any(
        url == "https://dashscope.aliyuncs.com/compatible-mode/v1/models"
        for url, _, _ in calls
    )


@pytest.mark.parametrize("base_url", [None, "https://opencode.ai/zen/go/v1"])
def test_run_doctor_opencode_go_skips_invalid_models_probe(monkeypatch, tmp_path, base_url):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    (home / ".env").write_text("OPENCODE_GO_API_KEY=***\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    if base_url:
        monkeypatch.setenv("OPENCODE_GO_BASE_URL", base_url)
    else:
        monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except ImportError:
        pass

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return types.SimpleNamespace(status_code=200)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert any(
        "OpenCode Go" in line and "(key configured)" in line
        for line in out.splitlines()
    )
    assert not any(url == "https://opencode.ai/zen/go/v1/models" for url, _, _ in calls)
    assert not any("opencode" in url.lower() and "models" in url.lower() for url, _, _ in calls)


class TestGitHubTokenCheck:
    """Tests for GitHub token / gh auth detection in doctor."""

    # This class *is* the test of the gh probe, so it opts out of the autouse
    # stub in ``_stub_doctor_externals``. Each test below still keeps itself
    # hermetic on its own -- two blank the PATH so the ``gh`` exec fails
    # immediately, and the third mocks ``subprocess.run``.
    exercises_real_gh_probe = True

    @staticmethod
    def _isolate_home(monkeypatch, home):
        """Point doctor at the temp HERMES_HOME.

        ``run_doctor`` reads the module-level ``HERMES_HOME`` constant (cached
        at import time), NOT the env var — so ``setenv("HERMES_HOME")`` alone
        leaves doctor probing the REAL ~/.hermes. On a dev machine with a
        large state.db that meant a multi-minute ``PRAGMA integrity_check``
        that blew the 300s per-file budget and killed the whole file.
        """
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setenv("HERMES_HOME", str(home))

    def test_no_token_and_not_gh_authenticated_shows_warn(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        self._isolate_home(monkeypatch, home)
        monkeypatch.setenv("PATH", "/nonexistent")  # gh not found

        from hermes_cli.doctor import run_doctor
        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_doctor(Namespace(fix=False))
        out = buf.getvalue()

        assert "No GITHUB_TOKEN" in out
        assert "60 req/hr" in out

    def test_token_env_present_shows_ok(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        self._isolate_home(monkeypatch, home)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        monkeypatch.setenv("PATH", "/nonexistent")  # gh not found

        from hermes_cli.doctor import run_doctor
        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_doctor(Namespace(fix=False))
        out = buf.getvalue()

        assert "GitHub token configured" in out

    def test_gh_authenticated_without_env_token_shows_ok(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        self._isolate_home(monkeypatch, home)
        # No GITHUB_TOKEN or GH_TOKEN
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        # Mock gh to return success
        import shutil
        real_which = shutil.which
        def mock_which(cmd):
            return "/usr/local/bin/gh" if cmd == "gh" else real_which(cmd)
        monkeypatch.setattr(shutil, "which", mock_which)

        call_log = []
        def mock_run(cmd, **kwargs):
            call_log.append(cmd)
            if cmd[:2] == ["gh", "auth"]:
                result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            else:
                result = types.SimpleNamespace(returncode=1, stdout="", stderr="")
            return result

        import subprocess
        monkeypatch.setattr(subprocess, "run", mock_run)

        from hermes_cli.doctor import run_doctor
        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_doctor(Namespace(fix=False))
        out = buf.getvalue()

        assert "gh auth" in str(call_log) or any(c[0] == "gh" for c in call_log), f"gh not called: {call_log}"
        assert "GitHub authenticated via gh CLI" in out or "token configured" in out


def _run_doctor_with_healthy_oauth_fallback(
    monkeypatch,
    tmp_path,
    *,
    env_key: str,
    bad_key: str,
    failing_host: str,
    minimax_oauth_status: dict,
    xai_oauth_status: dict | None = None,
) -> str:
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: nous\n"
        "  default: moonshotai/kimi-k2.6\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setenv(env_key, bad_key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_CN_API_KEY", raising=False)
    monkeypatch.setenv(env_key, bad_key)

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    from hermes_cli import auth as _auth_mod

    monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {"logged_in": True})
    monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: minimax_oauth_status)
    _xai_status = xai_oauth_status if xai_oauth_status is not None else {}
    monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: _xai_status)

    def fake_get(url, headers=None, timeout=None):
        status = 401 if failing_host in url else 200
        return types.SimpleNamespace(status_code=status)

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    return buf.getvalue()


@pytest.mark.parametrize(
    ("env_key", "bad_key", "failing_host", "minimax_oauth_status", "xai_oauth_status", "unexpected_issue"),
    [
        (
            "MINIMAX_API_KEY",
            "bad-minimax-key",
            "minimax.io",
            {"logged_in": True, "region": "global"},
            None,
            "Check MINIMAX_API_KEY in .env",
        ),
        (
            "XAI_API_KEY",
            "bad-xai-key",
            "api.x.ai",
            {},
            {"logged_in": True, "auth_mode": "oauth_pkce"},
            "Check XAI_API_KEY in .env",
        ),
    ],
)
def test_run_doctor_ignores_invalid_direct_keys_when_oauth_fallback_is_healthy(
    monkeypatch,
    tmp_path,
    env_key,
    bad_key,
    failing_host,
    minimax_oauth_status,
    xai_oauth_status,
    unexpected_issue,
):
    out = _run_doctor_with_healthy_oauth_fallback(
        monkeypatch,
        tmp_path,
        env_key=env_key,
        bad_key=bad_key,
        failing_host=failing_host,
        minimax_oauth_status=minimax_oauth_status,
        xai_oauth_status=xai_oauth_status,
    )

    assert "invalid API key" in out
    assert unexpected_issue not in out


def test_has_healthy_oauth_fallback_returns_false_for_unknown_provider():
    from hermes_cli.doctor import _has_healthy_oauth_fallback_for_apikey_provider
    assert _has_healthy_oauth_fallback_for_apikey_provider("unknown-provider") is False


class TestHasHealthyOauthFallbackForXai:
    def test_returns_true_when_xai_oauth_healthy(self, monkeypatch):
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {"logged_in": True})
        from hermes_cli.doctor import _has_healthy_oauth_fallback_for_apikey_provider
        assert _has_healthy_oauth_fallback_for_apikey_provider("xai") is True

    def test_returns_false_when_xai_oauth_not_logged_in(self, monkeypatch):
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {"logged_in": False})
        from hermes_cli.doctor import _has_healthy_oauth_fallback_for_apikey_provider
        assert _has_healthy_oauth_fallback_for_apikey_provider("xai") is False

    def test_returns_false_when_xai_oauth_returns_none(self, monkeypatch):
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: None)
        from hermes_cli.doctor import _has_healthy_oauth_fallback_for_apikey_provider
        assert _has_healthy_oauth_fallback_for_apikey_provider("xai") is False

    def test_returns_false_when_xai_import_unavailable(self, monkeypatch):
        import sys
        # Simulate get_xai_oauth_auth_status missing from auth module
        monkeypatch.delattr("hermes_cli.auth.get_xai_oauth_auth_status", raising=False)
        # Force doctor module to re-import the function
        monkeypatch.delitem(sys.modules, "hermes_cli.doctor", raising=False)
        from hermes_cli.doctor import _has_healthy_oauth_fallback_for_apikey_provider
        assert _has_healthy_oauth_fallback_for_apikey_provider("xai") is False


# ---------------------------------------------------------------------------
# ◆ Auth Providers — xAI OAuth display in run_doctor()
# ---------------------------------------------------------------------------


class TestDoctorXaiOAuthStatus:
    """The ◆ Auth Providers section must show xAI OAuth login state.

    xAI OAuth is checked in a *separate* try/except block so that an import
    failure (or runtime exception) cannot silence the Nous / Codex / Gemini /
    MiniMax rows that were already printed above it.
    """

    def _run(self, monkeypatch, tmp_path, *, xai_auth_fn) -> str:
        """Run doctor with a controlled xAI auth callable; return stdout."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", xai_auth_fn)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue()

    def test_logged_in_shows_ok(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": True},
        )
        assert "xAI OAuth" in out
        assert "(logged in)" in out

    def test_not_logged_in_shows_warn(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": False},
        )
        assert "xAI OAuth" in out
        assert "(not logged in)" in out

    def test_error_shown_when_not_logged_in_and_error_present(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": False, "error": "refresh token expired"},
        )
        assert "xAI OAuth" in out
        assert "refresh token expired" in out

    def test_no_error_line_when_error_key_absent(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": False},
        )
        assert "xAI OAuth" in out
        # The check_info line is only emitted when the "error" key is present.
        # Pick a token that would appear in no ordinary doctor output.
        assert "refresh token expired" not in out

    def test_logged_in_does_not_emit_not_logged_in_on_xai_line(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            xai_auth_fn=lambda: {"logged_in": True},
        )
        assert "xAI OAuth" in out
        # The xAI OAuth line itself must say "(logged in)", not "(not logged in)".
        xai_line = next(l for l in out.splitlines() if "xAI OAuth" in l)
        assert "(logged in)" in xai_line
        assert "(not logged in)" not in xai_line

    def test_import_failure_does_not_crash_doctor(self, monkeypatch, tmp_path):
        """Doctor must not crash when get_xai_oauth_auth_status cannot be imported."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.delattr(_auth_mod, "get_xai_oauth_auth_status", raising=False)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        out = buf.getvalue()
        # The ◆ Auth Providers header must still appear — other providers unaffected.
        assert "Auth Providers" in out

    def test_import_failure_does_not_affect_other_providers(self, monkeypatch, tmp_path):
        """Nous / Codex / Gemini / MiniMax rows must survive an xAI import failure."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {"logged_in": True})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.delattr(_auth_mod, "get_xai_oauth_auth_status", raising=False)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        out = buf.getvalue()
        assert "Nous Portal auth" in out
        assert "logged in" in out

    def test_function_raises_does_not_crash_doctor(self, monkeypatch, tmp_path):
        """A runtime exception from get_xai_oauth_auth_status must be swallowed."""
        def _raise():
            raise RuntimeError("simulated xAI status failure")

        out = self._run(monkeypatch, tmp_path, xai_auth_fn=_raise)
        assert "Auth Providers" in out

    def test_function_returns_none_does_not_crash_doctor(self, monkeypatch, tmp_path):
        """None return is normalised to {} via `or {}` — must not AttributeError."""
        out = self._run(monkeypatch, tmp_path, xai_auth_fn=lambda: None)
        # None → {} → logged_in falsy → shows not-logged-in warn
        assert "xAI OAuth" in out
        assert "(not logged in)" in out


# ---------------------------------------------------------------------------
# ◆ Auth Providers — codex CLI import hint placement (issue #27975)
# ---------------------------------------------------------------------------


class TestDoctorCodexCliHintPlacement:
    """The `codex CLI not installed` hint belongs under OpenAI Codex auth.

    Regression for #27975: the hint used to be emitted as a standalone block
    after all auth-provider rows, so it visually attached to whichever
    provider happened to print last (MiniMax OAuth in the reported repro),
    reading as remediation for an unrelated provider.
    """

    def _run(self, monkeypatch, tmp_path, *, codex_logged_in: bool, codex_cli_present: bool) -> str:
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {"logged_in": codex_logged_in})
        monkeypatch.setattr(_auth_mod, "get_minimax_oauth_auth_status", lambda: {"logged_in": False})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {"logged_in": False})

        real_which = doctor_mod.shutil.which
        monkeypatch.setattr(
            doctor_mod.shutil,
            "which",
            lambda cmd: ("/usr/local/bin/codex" if codex_cli_present else None) if cmd == "codex" else real_which(cmd),
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue()

    @staticmethod
    def _hint_line() -> str:
        return "codex CLI not installed"

    def test_hint_appears_under_codex_auth_when_missing(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, codex_logged_in=False, codex_cli_present=False)
        lines = out.splitlines()
        codex_idx = next(i for i, l in enumerate(lines) if "OpenAI Codex auth" in l)
        hint_idx = next(i for i, l in enumerate(lines) if self._hint_line() in l)
        minimax_idx = next(i for i, l in enumerate(lines) if "MiniMax OAuth" in l)
        # Hint must sit between Codex auth and the next provider row (#27975).
        assert codex_idx < hint_idx < minimax_idx

    def test_hint_suppressed_when_codex_cli_present(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, codex_logged_in=False, codex_cli_present=True)
        assert "OpenAI Codex auth" in out
        assert self._hint_line() not in out

    def test_hint_suppressed_when_codex_logged_in(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, codex_logged_in=True, codex_cli_present=False)
        assert "OpenAI Codex auth" in out
        assert "(logged in)" in out
        assert self._hint_line() not in out

    def test_hint_never_attaches_to_minimax_row(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, codex_logged_in=False, codex_cli_present=False)
        # The hint belongs to the Codex auth row that precedes it, never to the
        # MiniMax row that follows (#27975). The MiniMax row itself must not be
        # the hint line, and the hint must sit strictly above MiniMax.
        lines = [l for l in out.splitlines() if l.strip()]
        codex_idx = next(i for i, l in enumerate(lines) if "OpenAI Codex auth" in l)
        hint_idx = next(i for i, l in enumerate(lines) if self._hint_line() in l)
        minimax_idx = next(i for i, l in enumerate(lines) if "MiniMax OAuth" in l)
        # Hint sits under Codex and above MiniMax; the MiniMax row is not the hint.
        assert codex_idx < hint_idx < minimax_idx
        assert self._hint_line() not in lines[minimax_idx]


class TestDoctorStaleMaxIterationsDrift:
    """Regression for #17534: a stale HERMES_MAX_ITERATIONS in .env shadows
    agent.max_turns in config.yaml. The repro symptom is config.yaml saying
    400 while the gateway activity line reads N/90. Doctor must detect the
    drift, and `--fix` must remove the .env ghost (config.yaml wins).

    The detector reads the .env FILE directly, NOT os.environ — the gateway
    startup bridge can already have overridden os.environ to the config value,
    so the ghost is only visible in the file.
    """

    def _run_config_section(self, monkeypatch, tmp_path, *, fix, ghost, cfg_turns,
                            os_environ_value=None):
        import pathlib
        import contextlib
        import io
        from argparse import Namespace

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(
            f"agent:\n  max_turns: {cfg_turns}\n", encoding="utf-8"
        )
        env_lines = ["OPENAI_API_KEY=sk-test\n"]
        if ghost is not None:
            env_lines.append(f"HERMES_MAX_ITERATIONS={ghost}\n")
        (hermes_home / ".env").write_text("".join(env_lines), encoding="utf-8")

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
        monkeypatch.setattr(doctor_mod, "get_hermes_home", lambda: hermes_home)
        # Point the config helpers at the temp home.
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        if os_environ_value is not None:
            # Simulate the gateway bridge having already overridden os.environ.
            monkeypatch.setenv("HERMES_MAX_ITERATIONS", str(os_environ_value))
        else:
            monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)

        # Short-circuit at the Tool Availability stage — the drift check runs
        # well before it in the Configuration Files section.
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=fix))
        return buf.getvalue(), hermes_home

    def test_detects_drift_warn_only(self, monkeypatch, tmp_path):
        out, hermes_home = self._run_config_section(
            monkeypatch, tmp_path, fix=False, ghost=90, cfg_turns=400,
            os_environ_value=400,  # bridge contaminated os.environ
        )
        assert "HERMES_MAX_ITERATIONS=90" in out
        assert "shadows" in out
        # Warn-only must NOT mutate .env.
        assert "HERMES_MAX_ITERATIONS=90" in (hermes_home / ".env").read_text(encoding="utf-8")

    def test_fix_removes_ghost(self, monkeypatch, tmp_path):
        out, hermes_home = self._run_config_section(
            monkeypatch, tmp_path, fix=True, ghost=90, cfg_turns=400,
            os_environ_value=400,
        )
        assert "Removed stale HERMES_MAX_ITERATIONS" in out
        env_after = (hermes_home / ".env").read_text(encoding="utf-8")
        assert "HERMES_MAX_ITERATIONS" not in env_after
        assert "OPENAI_API_KEY=sk-test" in env_after  # other keys preserved

    def test_no_drift_when_values_match(self, monkeypatch, tmp_path):
        out, _ = self._run_config_section(
            monkeypatch, tmp_path, fix=False, ghost=400, cfg_turns=400,
        )
        assert "shadows" not in out

    def test_no_drift_when_ghost_absent(self, monkeypatch, tmp_path):
        out, _ = self._run_config_section(
            monkeypatch, tmp_path, fix=False, ghost=None, cfg_turns=400,
        )
        assert "shadows" not in out


def test_npm_audit_fix_hint_avoids_crashing_workspace_flag(monkeypatch, tmp_path):
    """`hermes doctor` must not hand users `npm audit fix --workspace <name>`:
    that exact form crashes npm with "Cannot read properties of null (reading
    'edgesOut')" (an arborist bug with workspace-filtered audit fix).

    It must not recommend root-level `npm audit fix` for workspace advisories
    either: current npm can crash there too with "Cannot read properties of null
    (reading 'isDescendantOf')" on this tree. The safe guidance is that these
    build-tool advisories clear via the lockfile/package bump.

    Regression for user reports where doctor flagged the web/ui-tui workspaces
    and the suggested fix command errored out.
    """
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    (project / "node_modules").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)

    # Only npm is "installed" — keeps the rest of run_doctor's external checks
    # quiet without affecting the npm-audit branch under test.
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda cmd: "/usr/bin/npm" if cmd == "npm" else None
    )

    def mock_audit(cmd, *, cwd, timeout):
        if "--workspace" in cmd:
            payload = (
                '{"metadata": {"vulnerabilities": '
                '{"critical": 0, "high": 2, "moderate": 0}}}'
            )
            return SimpleNamespace(returncode=1, stdout=payload, stderr="")
        payload = (
            '{"metadata": {"vulnerabilities": '
            '{"critical": 0, "high": 0, "moderate": 0}}}'
        )
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(doctor_mod, "_run_npm_audit", mock_audit)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # The audits are opt-in now (they cost 40-120s per target), so this
        # regression needs `--audit` to reach the code path it guards.
        doctor_mod.run_doctor(Namespace(fix=False, audit=True))
    out = buf.getvalue()

    # The workspace vulnerability is still reported ...
    assert "web workspace" in out
    # ... but the remediation must NOT use the npm-crashing per-workspace form
    # (`npm audit fix --workspace web` / `--workspace ui-tui`).
    assert "npm audit fix --workspace web" not in out
    assert "npm audit fix --workspace ui-tui" not in out
    # ... and it must not point at the root-level form either: npm can crash
    # there too with `isDescendantOf` on this monorepo tree.
    assert "npm audit fix" not in out
    # ... and it must still explain that a crashing manual remediation is a known
    # npm arborist bug, so the user isn't left thinking it means a broken Hermes
    # install, plus how to actually remediate (bump + re-resolve the lockfile).
    assert "known npm bug" in out
    assert "lockfile" in out

    # Doctor must NOT blanket-label the workspace block "build-time tooling".
    # The 2026-08-11 triage of this output found genuine runtime advisories in
    # the set — react-router-dom (GHSA-qwww-vcr4-c8h2, ships to the browser) in
    # `web` and a direct `undici` dependency in `ui-tui`. That blanket label is
    # precisely why they sat untriaged, so it is a regression to reintroduce it.
    assert "build-time tooling" not in out
    # Instead it points at the per-workspace detection command so each advisory
    # gets classified rather than dismissed as a group.
    assert "npm audit --json --workspace web" in out


class TestDoctorDeprecatedConfigAndEnv:
    """Doctor must surface deprecated/legacy config keys and env vars with
    modern replacements as non-failing warnings — without auto-migrating.
    """

    def test_collect_deprecated_config_keys_flags_legacy(self):
        raw = {
            "display": {"tool_progress_overrides": {"telegram": "all"}},
            "delegation": {"max_async_children": 5, "max_concurrent_children": 3},
            "compression": {"summary_model": "gpt-4o-mini", "enabled": True},
        }
        findings = doctor_mod.collect_deprecated_config_keys(raw)
        paths = {legacy for legacy, _ in findings}
        assert "display.tool_progress_overrides" in paths
        assert "delegation.max_async_children" in paths
        assert "compression.summary_model" in paths
        by_key = dict(findings)
        assert by_key["display.tool_progress_overrides"] == "display.platforms"
        assert by_key["delegation.max_async_children"] == (
            "delegation.max_concurrent_children"
        )
        assert by_key["compression.summary_model"] == "auxiliary.compression"

    def test_collect_deprecated_config_keys_clean(self):
        raw = {
            "display": {"platforms": {"telegram": {"tool_progress": "all"}}},
            "delegation": {"max_concurrent_children": 3},
            "compression": {"enabled": True},
        }
        assert doctor_mod.collect_deprecated_config_keys(raw) == []

    def test_collect_deprecated_env_vars(self):
        env = {
            "HERMES_TOOL_PROGRESS": "true",
            "TERMINAL_CWD": "/tmp/proj",
            "QQ_HOME_CHANNEL": "12345",
            "OPENAI_API_KEY": "sk-test",  # not deprecated
        }
        findings = doctor_mod.collect_deprecated_env_vars(env)
        names = {n for n, _ in findings}
        assert "HERMES_TOOL_PROGRESS" in names
        assert "TERMINAL_CWD" in names
        assert "QQ_HOME_CHANNEL" in names
        assert "OPENAI_API_KEY" not in names
        by_name = dict(findings)
        assert "display.tool_progress" in by_name["HERMES_TOOL_PROGRESS"]
        assert "terminal.cwd" in by_name["TERMINAL_CWD"]
        assert by_name["QQ_HOME_CHANNEL"] == "QQBOT_HOME_CHANNEL"

    def test_collect_deprecated_env_vars_ignores_empty(self):
        assert doctor_mod.collect_deprecated_env_vars({"TERMINAL_CWD": "  "}) == []
        assert doctor_mod.collect_deprecated_env_vars({}) == []
        assert doctor_mod.collect_deprecated_env_vars(None) == []

    def _run_doctor_with_config(self, monkeypatch, tmp_path, *, config_yaml: str, env_text: str = ""):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(config_yaml, encoding="utf-8")
        env_body = env_text if env_text else "OPENAI_API_KEY=sk-test\n"
        (hermes_home / ".env").write_text(env_body, encoding="utf-8")

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", hermes_home)
        monkeypatch.setattr(doctor_mod, "get_hermes_home", lambda: hermes_home)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # Clear process-level legacy env so tests only see the on-disk .env.
        for k in (
            "HERMES_TOOL_PROGRESS",
            "HERMES_TOOL_PROGRESS_MODE",
            "TERMINAL_CWD",
            "MESSAGING_CWD",
            "QQ_HOME_CHANNEL",
            "QQ_HOME_CHANNEL_NAME",
        ):
            monkeypatch.delenv(k, raising=False)

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue(), hermes_home

    def test_doctor_warns_on_tool_progress_overrides_and_max_async_children(
        self, monkeypatch, tmp_path
    ):
        cfg = """\
display:
  tool_progress_overrides:
    telegram: all
delegation:
  max_async_children: 8
  max_concurrent_children: 3
"""
        out, hermes_home = self._run_doctor_with_config(monkeypatch, tmp_path, config_yaml=cfg)
        assert "Deprecated: display.tool_progress_overrides" in out
        assert "display.platforms" in out
        assert "Deprecated: delegation.max_async_children" in out
        assert "max_concurrent_children" in out
        # Warn-only: must not mutate config.
        on_disk = (hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "tool_progress_overrides" in on_disk
        assert "max_async_children" in on_disk

    def test_doctor_warns_on_compression_summary_and_legacy_env(
        self, monkeypatch, tmp_path
    ):
        cfg = """\
compression:
  summary_model: gpt-4o-mini
  summary_provider: openai
"""
        env = (
            "OPENAI_API_KEY=sk-test\n"
            "HERMES_TOOL_PROGRESS=true\n"
            "TERMINAL_CWD=/old/path\n"
            "QQ_HOME_CHANNEL=999\n"
        )
        out, _ = self._run_doctor_with_config(
            monkeypatch, tmp_path, config_yaml=cfg, env_text=env
        )
        assert "Deprecated: compression.summary_model" in out
        assert "auxiliary.compression" in out
        assert "Deprecated: HERMES_TOOL_PROGRESS" in out
        assert "display.tool_progress" in out
        assert "Deprecated: TERMINAL_CWD" in out
        assert "terminal.cwd" in out
        assert "Deprecated: QQ_HOME_CHANNEL" in out
        assert "QQBOT_HOME_CHANNEL" in out

    def test_doctor_clean_config_has_no_deprecated_warning(self, monkeypatch, tmp_path):
        cfg = """\
display:
  platforms:
    telegram:
      tool_progress: all
delegation:
  max_concurrent_children: 3
compression:
  enabled: true
terminal:
  cwd: /project
"""
        out, _ = self._run_doctor_with_config(monkeypatch, tmp_path, config_yaml=cfg)
        assert "Deprecated: display.tool_progress_overrides" not in out
        assert "Deprecated: delegation.max_async_children" not in out
        assert "Deprecated: compression.summary_model" not in out
        assert "Deprecated: HERMES_TOOL_PROGRESS" not in out
        assert "Deprecated: TERMINAL_CWD" not in out
        assert "Deprecated: QQ_HOME_CHANNEL" not in out
        assert "No deprecated config keys or env vars" in out

    def test_report_does_not_count_as_blocking_issue(self, monkeypatch, tmp_path, capsys):
        """report_deprecated_config_and_env is warn-only — no issues list mutation."""
        findings = doctor_mod.report_deprecated_config_and_env(
            {"delegation": {"max_async_children": 2}},
            {"HERMES_TOOL_PROGRESS_MODE": "verbose"},
        )
        out = capsys.readouterr().out
        assert len(findings) == 2
        assert "Deprecated: delegation.max_async_children" in out
        assert "Deprecated: HERMES_TOOL_PROGRESS_MODE" in out
        assert "⚠" in out or "Deprecated" in out


class TestStateDbProbeBudget:
    """`hermes doctor` must terminate on a state.db too large to scan.

    The probe ends in ``PRAGMA integrity_check``, an O(database-size)
    page-by-page verification. On the 5.1 GB state.db that motivated this it
    ran >12 minutes with no output and the command never returned.
    """

    @staticmethod
    def _isolate_home(monkeypatch, home):
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setenv("HERMES_HOME", str(home))

    def _run_with_probe(self, monkeypatch, tmp_path, probe):
        import hermes_state

        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        self._isolate_home(monkeypatch, home)
        # A real (small) state.db so doctor reaches the health probe at all.
        db = hermes_state.SessionDB(db_path=home / "state.db")
        db.create_session(session_id="probe-budget-test", source="cli")
        db.close()

        monkeypatch.setattr(hermes_state, "_db_opens_cleanly", probe)
        repaired = []
        monkeypatch.setattr(
            hermes_state,
            "repair_state_db_schema",
            lambda *a, **k: repaired.append(a) or {"repaired": True},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        return buf.getvalue(), repaired

    def test_probe_is_called_with_a_bounded_budget(self, monkeypatch, tmp_path):
        """Doctor must never call the probe unbounded — that is the hang."""
        seen = {}

        def probe(path, *, timeout_seconds=None, include_fts_integrity=False):
            seen["timeout_seconds"] = timeout_seconds
            return None

        self._run_with_probe(monkeypatch, tmp_path, probe)

        assert seen, "doctor never reached the state.db health probe"
        budget = seen["timeout_seconds"]
        assert budget is not None, "doctor ran the probe unbounded"
        assert 0 < budget <= 60

    def test_timeout_renders_a_warn_and_does_not_claim_corruption(
        self, monkeypatch, tmp_path
    ):
        import hermes_state

        def probe(path, *, timeout_seconds=None, include_fts_integrity=False):
            raise hermes_state.StateDbProbeTimeout(
                "PRAGMA integrity_check", float(timeout_seconds or 5)
            )

        out, _ = self._run_with_probe(monkeypatch, tmp_path, probe)

        assert "health probe did not finish in budget" in out
        assert "not a corruption report" in out
        # It must not be reported through the corruption wording/remediation.
        # (The generic "Tip: run 'hermes doctor --fix'" footer is always
        # printed, so assert on the state.db-specific strings.)
        assert "FTS index may be corrupt" not in out
        assert "state.db FTS write corruption" not in out
        assert "schema is malformed" not in out

    def test_timeout_under_fix_does_not_trigger_repair(self, monkeypatch, tmp_path):
        """--fix must not "repair" a database we never found damage in."""
        import hermes_state

        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        self._isolate_home(monkeypatch, home)
        db = hermes_state.SessionDB(db_path=home / "state.db")
        db.create_session(session_id="probe-budget-fix", source="cli")
        db.close()

        def probe(path, *, timeout_seconds=None, include_fts_integrity=False):
            raise hermes_state.StateDbProbeTimeout(
                "PRAGMA integrity_check", float(timeout_seconds or 5)
            )

        repaired = []
        monkeypatch.setattr(hermes_state, "_db_opens_cleanly", probe)
        monkeypatch.setattr(
            hermes_state,
            "repair_state_db_schema",
            lambda *a, **k: repaired.append(a) or {"repaired": True},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=True))

        assert repaired == [], "a probe timeout must never escalate to repair"

    def test_budget_is_env_overridable(self, monkeypatch):
        default = doctor_mod._STATE_DB_PROBE_TIMEOUT_DEFAULT

        monkeypatch.delenv(doctor_mod._STATE_DB_PROBE_TIMEOUT_ENV, raising=False)
        assert doctor_mod._state_db_probe_budget() == default

        monkeypatch.setenv(doctor_mod._STATE_DB_PROBE_TIMEOUT_ENV, "120")
        assert doctor_mod._state_db_probe_budget() == 120.0

        # Garbage and non-positive values fall back rather than disabling it.
        for bad in ("nonsense", "0", "-1", ""):
            monkeypatch.setenv(doctor_mod._STATE_DB_PROBE_TIMEOUT_ENV, bad)
            assert doctor_mod._state_db_probe_budget() == default


class TestNpmAuditBudget:
    """`hermes doctor` must terminate on an `npm audit` that wedges.

    ``subprocess.run(capture_output=True, timeout=30)`` does NOT bound the
    call on Windows: ``npm`` resolves to ``npm.cmd``, so the real work runs
    in a ``node.exe`` GRANDCHILD that inherits the capture pipes. On timeout
    ``subprocess.run`` kills only the direct child and then blocks re-draining
    a pipe whose write end the surviving grandchild still holds. One audit
    measured 93s against that 30s timeout, and the four audits together
    accounted for a 6m34s silent gap in an ~11 minute doctor run.
    """

    @pytest.mark.timeout(90)  # backstop only; a bounded impl returns in ~timeout
    def test_audit_aborts_at_its_budget_when_a_grandchild_holds_the_pipe(
        self, tmp_path
    ):
        """The measured hang: the child outlives its parent holding the pipe."""
        wedged = tmp_path / "wedged_npm.py"
        wedged.write_text(
            "import subprocess, sys, time\n"
            # Stands in for npm.cmd's node.exe grandchild: inherits the capture
            # pipes and lingers, so the write end never reaches EOF.
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )

        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            doctor_mod._run_npm_audit(
                [sys.executable, str(wedged)], cwd=tmp_path, timeout=3
            )
        elapsed = time.monotonic() - start

        assert elapsed < 30, (
            f"raised TimeoutExpired but took {elapsed:.1f}s — "
            "the npm process tree was not killed"
        )

    def test_budget_is_env_overridable(self, monkeypatch):
        default = doctor_mod._NPM_AUDIT_TIMEOUT_DEFAULT
        on_demand = doctor_mod._NPM_AUDIT_TIMEOUT_ON_DEMAND

        monkeypatch.delenv(doctor_mod._NPM_AUDIT_TIMEOUT_ENV, raising=False)
        assert doctor_mod._npm_audit_budget() == default
        # `--audit` was asked for explicitly, so it gets room to finish.
        assert doctor_mod._npm_audit_budget(on_demand=True) == on_demand
        assert on_demand > default

        monkeypatch.setenv(doctor_mod._NPM_AUDIT_TIMEOUT_ENV, "120")
        assert doctor_mod._npm_audit_budget() == 120.0
        assert doctor_mod._npm_audit_budget(on_demand=True) == 120.0

        # Garbage and non-positive values fall back rather than disabling it.
        for bad in ("nonsense", "0", "-1", ""):
            monkeypatch.setenv(doctor_mod._NPM_AUDIT_TIMEOUT_ENV, bad)
            assert doctor_mod._npm_audit_budget() == default
            assert doctor_mod._npm_audit_budget(on_demand=True) == on_demand

    def test_timeout_renders_a_warn_and_is_not_a_blocking_issue(
        self, monkeypatch, tmp_path, capsys
    ):
        """A budget hit must be visible — the old code swallowed it silently."""

        def _timeout(argv, *, cwd, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(doctor_mod, "_run_npm_audit", _timeout)
        issues: list[str] = []
        doctor_mod._audit_npm_target(
            "npm", tmp_path, "web workspace", ["--workspace", "web"], issues
        )

        out = capsys.readouterr().out
        assert "web workspace deps" in out
        assert "budget" in out
        # A diagnostic that could not answer is not a vulnerability finding.
        assert issues == []

    def test_successful_audit_still_reports_vulnerabilities(
        self, monkeypatch, tmp_path, capsys
    ):
        """The bound must not change what a completed audit reports."""
        payload = json.dumps(
            {"metadata": {"vulnerabilities": {"critical": 1, "high": 2, "moderate": 3}}}
        )

        def _ok(argv, *, cwd, timeout):
            return subprocess.CompletedProcess(argv, 1, payload, "")

        monkeypatch.setattr(doctor_mod, "_run_npm_audit", _ok)
        issues: list[str] = []
        doctor_mod._audit_npm_target(
            "npm", tmp_path, "WhatsApp bridge", [], issues
        )

        out = capsys.readouterr().out
        assert "WhatsApp bridge deps" in out
        assert "1 critical, 2 high, 3 moderate" in out
        assert issues == ["WhatsApp bridge has 6 npm vulnerabilities"]


class TestNpmAuditFixHintTracksFixAvailable:
    """The `npm audit fix` hint must reflect whether npm says a fix EXISTS.

    Doctor used to pick the hint purely from which target it was, never from
    the audit payload. On the WhatsApp bridge that produced a command that
    cannot work: its two high advisories (link-preview-js GHSA-4gp8-rjrq-ch6q
    and @whiskeysockets/baileys, which only inherits it) both report
    `fixAvailable: false` — every published link-preview-js is <=4.0.0, all
    affected, and Baileys pins ^3.0.0. Running the suggested command is a
    no-op that leaves the warning byte-identical, so the reader concludes
    something is broken rather than that there is nothing to bump.
    """

    @staticmethod
    def _run(payload: str, capsys, monkeypatch, tmp_path):
        def _ok(argv, *, cwd, timeout):
            return subprocess.CompletedProcess(argv, 1, payload, "")

        monkeypatch.setattr(doctor_mod, "_run_npm_audit", _ok)
        issues: list[str] = []
        doctor_mod._audit_npm_target("npm", tmp_path, "WhatsApp bridge", [], issues)
        return capsys.readouterr().out, issues

    def test_unfixable_advisories_do_not_suggest_npm_audit_fix(
        self, monkeypatch, tmp_path, capsys
    ):
        payload = json.dumps(
            {
                "metadata": {"vulnerabilities": {"critical": 0, "high": 2, "moderate": 0}},
                "vulnerabilities": {
                    "link-preview-js": {"severity": "high", "fixAvailable": False},
                    "@whiskeysockets/baileys": {"severity": "high", "fixAvailable": False},
                },
            }
        )
        out, issues = self._run(payload, capsys, monkeypatch, tmp_path)

        assert "0 critical, 2 high, 0 moderate" in out
        # The whole point: no command that cannot accomplish anything.
        assert "npm audit fix" not in out
        # ... and the reader is told WHY, so the absence isn't mistaken for a gap.
        assert "no upstream fix" in out
        # Still a real finding — unfixable is not the same as absent.
        assert issues == ["WhatsApp bridge has 2 npm vulnerabilities"]

    def test_fixable_advisories_still_suggest_npm_audit_fix(
        self, monkeypatch, tmp_path, capsys
    ):
        """The hint must survive where it actually works — no over-correction."""
        payload = json.dumps(
            {
                "metadata": {"vulnerabilities": {"critical": 0, "high": 1, "moderate": 0}},
                "vulnerabilities": {
                    "body-parser": {"severity": "high", "fixAvailable": True},
                },
            }
        )
        out, _ = self._run(payload, capsys, monkeypatch, tmp_path)
        assert "npm audit fix" in out

    def test_partially_fixable_still_suggests_the_fix(
        self, monkeypatch, tmp_path, capsys
    ):
        """A mixed set is worth running: the command clears the fixable subset."""
        payload = json.dumps(
            {
                "metadata": {"vulnerabilities": {"critical": 0, "high": 2, "moderate": 0}},
                "vulnerabilities": {
                    "link-preview-js": {"severity": "high", "fixAvailable": False},
                    # npm reports an object, not a bool, when it has a target.
                    "body-parser": {
                        "severity": "high",
                        "fixAvailable": {
                            "name": "body-parser",
                            "version": "1.20.6",
                            "isSemVerMajor": False,
                        },
                    },
                },
            }
        )
        out, _ = self._run(payload, capsys, monkeypatch, tmp_path)
        assert "npm audit fix" in out

    def test_missing_vulnerabilities_block_does_not_invent_a_fix(
        self, monkeypatch, tmp_path, capsys
    ):
        """No per-advisory detail (older npm, trimmed payload) → claim nothing."""
        payload = json.dumps(
            {"metadata": {"vulnerabilities": {"critical": 0, "high": 1, "moderate": 0}}}
        )
        out, _ = self._run(payload, capsys, monkeypatch, tmp_path)
        assert "npm audit fix" not in out


class TestNpmAuditIsOptIn:
    """The audits cost 40-120s per target and are off unless asked for.

    They were the single largest cost in `hermes doctor` (6m44s of an ~11
    minute run) and reliably ran out of budget before reporting anything, so
    they moved behind `--audit`. A dropped check must still be *visible*.
    """

    def _run(self, monkeypatch, args):
        audited = []
        monkeypatch.setattr(
            doctor_mod,
            "_audit_npm_target",
            lambda *a, **k: audited.append((a, k)),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(args)
        return buf.getvalue(), audited

    def test_default_run_skips_the_audits_but_says_so(self, monkeypatch):
        out, audited = self._run(monkeypatch, Namespace(fix=False))

        assert audited == [], "a default `hermes doctor` must not run npm audit"
        assert "hermes doctor --audit" in out, "the skip must be discoverable"

    def test_audit_flag_runs_them_with_the_on_demand_budget(self, monkeypatch):
        _, audited = self._run(monkeypatch, Namespace(fix=False, audit=True))

        if not audited:
            pytest.skip("no npm on PATH / no node_modules in this checkout")
        assert all(kwargs.get("on_demand") for _, kwargs in audited), (
            "an explicitly requested audit must get the budget that lets it finish"
        )


class TestStateDbDeepProbe:
    """The FTS rank=1 integrity-check is opt-in, and bounded when it runs.

    It is the only probe that detects an index rowid whose ``messages`` row is
    gone (schema v32, external content) — but it re-reads every indexed row,
    so on the 5.1 GB production state.db it is a minutes-long scan. Putting it
    in the default run would recreate the documented `hermes doctor` hang.
    """

    def _run(self, monkeypatch, tmp_path, fts_probe, *, deep=False, fix=False):
        import hermes_state

        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setenv("HERMES_HOME", str(home))
        db = hermes_state.SessionDB(db_path=home / "state.db")
        db.create_session(session_id="deep-probe-test", source="cli")
        db.close()

        # The general probe always passes here; these tests are about the FTS
        # check that runs beside it.
        monkeypatch.setattr(
            hermes_state,
            "_db_opens_cleanly",
            lambda path, *, timeout_seconds=None, include_fts_integrity=False: None,
        )
        calls = []

        def _probe(path, *, timeout_seconds=None):
            calls.append(timeout_seconds)
            return fts_probe(path, timeout_seconds=timeout_seconds)

        monkeypatch.setattr(hermes_state, "check_state_db_fts_integrity", _probe)
        rebuilt = []
        monkeypatch.setattr(
            hermes_state.SessionDB,
            "rebuild_fts",
            lambda self: rebuilt.append(1) or 1,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=fix, deep=deep))
        return buf.getvalue(), calls, rebuilt

    def test_default_run_does_not_run_the_fts_check(self, monkeypatch, tmp_path):
        """The 70s check must not be on the path of every `hermes doctor`."""

        def probe(path, *, timeout_seconds=None):
            raise AssertionError("the FTS check ran without --deep")

        out, calls, _ = self._run(monkeypatch, tmp_path, probe)

        assert calls == []
        # …and it says so, rather than letting a clean run imply full coverage.
        assert "Full-text index verification was skipped" in out

    def test_deep_run_runs_it_on_its_own_budget(self, monkeypatch, tmp_path):
        def probe(path, *, timeout_seconds=None):
            return None

        out, calls, _ = self._run(monkeypatch, tmp_path, probe, deep=True)

        assert len(calls) == 1
        # Still bounded — --deep buys its own budget, not an unbounded scan.
        assert calls[0] == doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_DEFAULT
        assert calls[0] > doctor_mod._STATE_DB_PROBE_TIMEOUT_DEFAULT
        assert "full-text index verifies against its content" in out
        assert "Full-text index verification was skipped" not in out

    def test_deep_timeout_is_unknown_not_corruption(self, monkeypatch, tmp_path):
        """A deep check that runs out of budget must not accuse the index."""
        import hermes_state

        def probe(path, *, timeout_seconds=None):
            raise hermes_state.StateDbProbeTimeout(
                "the FTS integrity-check", float(timeout_seconds or 300)
            )

        out, _, rebuilt = self._run(
            monkeypatch, tmp_path, probe, deep=True, fix=True
        )

        assert "full-text index check did not finish in budget" in out
        assert "not a corruption report" in out
        assert "does not match its content" not in out
        assert rebuilt == [], "a probe timeout must never trigger a rebuild"
        # Unknown is not healthy either. The first draft set the reason to None
        # on timeout and fell straight into the clean branch, printing a green
        # "verifies against its content" for a check that never finished.
        assert "verifies against its content" not in out

    def test_deep_run_reports_a_real_orphan(self, monkeypatch, tmp_path):
        def probe(path, *, timeout_seconds=None):
            return "messages_fts: database disk image is malformed"

        out, _, rebuilt = self._run(monkeypatch, tmp_path, probe, deep=True)

        assert "does not match its content" in out
        assert "verifies against its content" not in out
        assert rebuilt == [], "no --fix, no rebuild"

    def test_deep_fix_rebuilds_the_index(self, monkeypatch, tmp_path):
        """--fix repairs it in place; 'rebuild' is the documented recovery."""

        def probe(path, *, timeout_seconds=None):
            return "messages_fts: database disk image is malformed"

        out, _, rebuilt = self._run(
            monkeypatch, tmp_path, probe, deep=True, fix=True
        )

        assert rebuilt == [1]
        assert "Rebuilt the state.db full-text index" in out

    def test_fts_budget_is_env_overridable(self, monkeypatch):
        default = doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_DEFAULT

        monkeypatch.delenv(doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_ENV, raising=False)
        assert doctor_mod._state_db_fts_probe_budget() == default

        monkeypatch.setenv(doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_ENV, "45")
        assert doctor_mod._state_db_fts_probe_budget() == 45.0

        for bad in ("nonsense", "0", "-1", ""):
            monkeypatch.setenv(doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_ENV, bad)
            assert doctor_mod._state_db_fts_probe_budget() == default

    def test_deep_flag_is_wired_into_the_cli(self):
        """The flag doctor reads must be the flag the CLI accepts."""
        import argparse

        from hermes_cli.subcommands.doctor import build_doctor_parser

        parser = argparse.ArgumentParser()
        build_doctor_parser(
            parser.add_subparsers(dest="command"), cmd_doctor=lambda args: None
        )
        assert parser.parse_args(["doctor"]).deep is False
        assert parser.parse_args(["doctor", "--deep"]).deep is True
        assert parser.parse_args(["doctor", "--deep", "--fix"]).fix is True

    def test_fts_budget_is_independent_of_the_general_budget(self, monkeypatch):
        """One knob must not silently retune the other.

        They bound checks that differ by an order of magnitude on the same
        file, so a 5s general budget must not clamp the FTS check.
        """
        monkeypatch.setenv(doctor_mod._STATE_DB_PROBE_TIMEOUT_ENV, "5")
        monkeypatch.delenv(doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_ENV, raising=False)
        assert doctor_mod._state_db_probe_budget() == 5.0
        assert (
            doctor_mod._state_db_fts_probe_budget()
            == doctor_mod._STATE_DB_FTS_PROBE_TIMEOUT_DEFAULT
        )
