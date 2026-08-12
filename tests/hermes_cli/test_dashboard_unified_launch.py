"""Tests for the unified profile→machine dashboard launch routing.

`<profile> dashboard` routes to ONE machine-level dashboard instead of
spawning a per-profile server: attach (open browser at ?profile=) when one
is already listening, else re-exec as the machine dashboard with the
launching profile preselected. `--isolated` opts out.
"""
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def main_mod():
    import hermes_cli.main as main_mod
    return main_mod


def _args(**kw):
    defaults = dict(
        status=False, stop=False, host="127.0.0.1", port=9119,
        no_open=True, insecure=False, skip_build=False,
        isolated=False, open_profile="",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture_reexec(main_mod, monkeypatch):
    """Record the machine-dashboard re-exec on BOTH platforms.

    POSIX re-execs via ``os.execvpe``; Windows spawns via
    ``subprocess.Popen`` + ``proc.wait()`` instead (execvpe can crash there
    under Python 3.14+ — see cmd_dashboard). A test that patches only
    ``execvpe`` therefore spawns a REAL ``hermes dashboard`` subprocess on
    Windows and then blocks forever in ``wait()`` — which additionally races
    the developer's live :9119 server. Patch both paths so the test is
    hermetic and platform-agnostic.

    Returns a list of ``(exe, argv, env)`` tuples.
    """
    execs = []

    def fake_exec(exe, argv, env):
        execs.append((exe, argv, env))
        raise SystemExit(0)  # execvpe never returns

    class _FakeProc:
        def wait(self):
            return 0

    def fake_popen(argv, env=None, **_kw):
        execs.append((argv[0], list(argv), env))
        return _FakeProc()

    monkeypatch.setattr(main_mod.os, "execvpe", fake_exec)
    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    return execs


class TestUnifiedDashboardRouting:
    def test_profile_launch_attaches_to_running_dashboard(self, main_mod, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit) as exc:
            main_mod.cmd_dashboard(_args())
        assert exc.value.code == 0
        assert execs == []  # attached, never re-exec'd

    def test_profile_launch_attach_opens_scoped_url(self, main_mod, monkeypatch):
        """The attach path must open the browser at ?profile=<name> — that
        URL is the entire point of attaching (preselects the switcher)."""
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)
        opened = []
        import webbrowser
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

        with pytest.raises(SystemExit) as exc:
            main_mod.cmd_dashboard(_args(no_open=False))
        assert exc.value.code == 0
        assert opened == ["http://127.0.0.1:9119/?profile=worker_x"]

    def test_profile_launch_reexecs_machine_dashboard(self, main_mod, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit):
            main_mod.cmd_dashboard(_args())

        assert len(execs) == 1
        exe, argv, env = execs[0]
        assert exe == sys.executable
        # Pinned to the default profile + launching profile preselected.
        assert "-p" in argv and argv[argv.index("-p") + 1] == "default"
        assert "--open-profile" in argv
        assert argv[argv.index("--open-profile") + 1] == "worker_x"
        # The child is pinned to the machine ROOT, not the launching profile's
        # HERMES_HOME.  For a standard install (HERMES_HOME unset) that root is
        # the platform-native default (~/.hermes), NOT dropped — see the Docker
        # test below for why we resolve explicitly instead of popping.
        from hermes_constants import get_default_hermes_root
        assert env.get("HERMES_HOME") == str(get_default_hermes_root())

    def test_reexec_pins_docker_machine_root(self, main_mod, monkeypatch):
        """In the Docker layout (HERMES_HOME=/opt/data, profiles under
        /opt/data/profiles/<name>) the reroute must pin the child to the
        machine root /opt/data — NOT drop HERMES_HOME.

        Dropping it makes the child fall back to $HOME/.hermes
        (= /opt/data/.hermes), an empty auto-seeded home, so the dashboard
        shows only the default profile and the .install_method stamp is
        missing (which also misfires the Docker update-button guard).
        Regression test for the support report.
        """
        monkeypatch.setenv("HERMES_HOME", "/opt/data/profiles/oracle")
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "oracle"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit):
            main_mod.cmd_dashboard(_args())

        assert len(execs) == 1
        _exe, _argv, env = execs[0]
        # get_default_hermes_root() strips the trailing profiles/<name>, so the
        # child binds /opt/data — where the real default/oracle/saga profiles
        # and the .install_method stamp actually live. Compare through Path so
        # the assertion is separator-native: get_default_hermes_root() returns a
        # Path, which stringifies to "\opt\data" on Windows. (This mismatch was
        # invisible until the Popen patch above stopped the test hanging here.)
        assert env.get("HERMES_HOME") == str(Path("/opt/data"))

    def test_desktop_profile_backend_skips_machine_dashboard_reroute(self, main_mod, monkeypatch):
        """A desktop-spawned named-profile backend (HERMES_DESKTOP=1) must NOT
        reroute into the machine dashboard. The reroute re-execs as the default
        profile and exits, so the desktop never sees a ready backend → boot
        loop. The guard keeps desktop pool backends per-profile."""
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        # Port free, so the only thing that could re-exec is the routing block.
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args())
        assert execs == []

    def test_isolated_flag_skips_routing(self, main_mod, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        # Port free: a named profile WITHOUT --isolated would take the routing
        # block's re-exec branch here, so "no re-exec" is the proof that
        # routing was skipped. (The port probe itself is no longer a routing
        # tell — the startup preflight consults it on every launch.)
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)
        # With --isolated the routing block is skipped entirely; the command
        # proceeds to dependency checks. Make the first post-routing step
        # bail so the test doesn't actually start a server.
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args(isolated=True))
        assert execs == []

    def test_default_profile_launch_skips_routing(self, main_mod, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "default"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args())
        assert execs == []

    def test_reexec_child_does_not_reroute(self, main_mod, monkeypatch):
        """The re-exec'd child carries --open-profile; the guard must treat
        that as 'already routed' and never re-exec again (no exec loop)."""
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        # Stub the probe: unpatched, the startup preflight would open a real
        # socket to the developer's live :9119.
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args(open_profile="worker_x"))
        assert execs == []

    def test_dashboard_starts_mcp_discovery_for_ws_backend(self, main_mod, monkeypatch):
        """The dashboard process serves the /api/ws gateway but never runs
        tui_gateway/entry.py, so it must kick off MCP discovery itself or
        desktop sessions never see a profile's MCP tools."""
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "default"
        )
        # Stub the probe: unpatched, the startup preflight would open a real
        # socket to the developer's live :9119 and abort the launch.
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        monkeypatch.delenv("HERMES_WEB_DIST", raising=False)
        monkeypatch.setattr(main_mod, "_sync_bundled_skills_quietly", lambda: None)
        monkeypatch.setattr(main_mod, "_build_web_ui", lambda *_a, **_k: True)
        monkeypatch.setitem(sys.modules, "fastapi", types.SimpleNamespace())
        monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace())
        monkeypatch.setitem(
            sys.modules,
            "hermes_logging",
            types.SimpleNamespace(setup_logging=lambda **_k: None),
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.plugins",
            types.SimpleNamespace(discover_plugins=lambda: None),
        )
        calls = []
        monkeypatch.setattr(
            "hermes_cli.mcp_startup.start_background_mcp_discovery",
            lambda **kwargs: calls.append(kwargs),
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.web_server",
            types.SimpleNamespace(
                start_server=lambda **_kwargs: None,
                # Pre-existing gap: the stub omitted WEB_DIST, which
                # cmd_dashboard imports for the build gate, so this test failed
                # with ImportError before ever reaching its assertion.
                WEB_DIST=Path(__file__).parent / "_no_such_web_dist",
            ),
        )

        main_mod.cmd_dashboard(_args())

        assert calls == [
            {
                "logger": main_mod.logger,
                "thread_name": "dashboard-mcp-discovery",
            }
        ]


class TestDashboardPortPreflight:
    """A dashboard whose port is already held must fail fast, before the
    expensive startup work.

    Root cause of the 2026-08-12 incident: the bind is the LAST step of
    startup, so a redundant launch against a healthy incumbent first pays for
    imports, skills sync, plugin discovery, MCP discovery and the ~19k-line
    web_server import. On an idle box that wasted ~20s; under memory pressure
    the process sat for 29 minutes and died before ever reaching the bind —
    so it never emitted the WSAEADDRINUSE that would have explained it.
    """

    def _stub_startup(self, main_mod, monkeypatch, started):
        """Neutralize every expensive startup step after the preflight point."""
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "default"
        )
        monkeypatch.delenv("HERMES_WEB_DIST", raising=False)
        monkeypatch.setattr(main_mod, "_sync_bundled_skills_quietly", lambda: None)
        monkeypatch.setattr(main_mod, "_build_web_ui", lambda *_a, **_k: True)
        monkeypatch.setitem(sys.modules, "fastapi", types.SimpleNamespace())
        monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace())
        monkeypatch.setitem(
            sys.modules,
            "hermes_logging",
            types.SimpleNamespace(setup_logging=lambda **_k: None),
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.plugins",
            types.SimpleNamespace(discover_plugins=lambda: None),
        )
        monkeypatch.setattr(
            "hermes_cli.mcp_startup.start_background_mcp_discovery",
            lambda **_kwargs: None,
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.web_server",
            types.SimpleNamespace(
                start_server=lambda **kwargs: started.append(kwargs),
                # cmd_dashboard also does `from hermes_cli.web_server import
                # WEB_DIST` for the build gate. Point it at a path that does
                # not exist so `_already_built` is False and the (stubbed)
                # _build_web_ui branch runs instead.
                WEB_DIST=Path(__file__).parent / "_no_such_web_dist",
            ),
        )

    def test_exits_nonzero_when_port_already_held(self, main_mod, monkeypatch):
        started = []
        self._stub_startup(main_mod, monkeypatch, started)
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)

        with pytest.raises(SystemExit) as exc:
            main_mod.cmd_dashboard(_args())

        assert exc.value.code != 0
        assert started == []  # never reached start_server

    def test_logs_an_error_naming_host_and_port(self, main_mod, monkeypatch, caplog):
        """The conflict must reach the log files, not just stderr — uvicorn's
        own bind error goes to its non-propagating logger and never lands in
        agent.log/gui.log (measured: 0 hits during the incident)."""
        started = []
        self._stub_startup(main_mod, monkeypatch, started)
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)

        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit):
                main_mod.cmd_dashboard(_args(port=9119, host="127.0.0.1"))

        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert any("9119" in m for m in errors), errors
        assert any("127.0.0.1" in m for m in errors), errors

    def test_starts_normally_when_port_is_free(self, main_mod, monkeypatch):
        started = []
        self._stub_startup(main_mod, monkeypatch, started)
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)

        main_mod.cmd_dashboard(_args())

        assert len(started) == 1

    def test_auto_assign_port_zero_is_never_preflighted(self, main_mod, monkeypatch):
        """``--port 0`` asks the OS for any free port, so 'something is already
        listening on 0' is meaningless — the probe must be skipped entirely."""
        started = []
        self._stub_startup(main_mod, monkeypatch, started)
        probes = []
        monkeypatch.setattr(
            main_mod,
            "_dashboard_listening",
            lambda host, port: probes.append(port) or True,
        )

        main_mod.cmd_dashboard(_args(port=0))

        assert probes == []
        assert len(started) == 1

    def test_preflight_runs_for_isolated_launches_too(self, main_mod, monkeypatch):
        """``--isolated`` opts out of machine-dashboard *routing*, but it still
        binds a port, so a held port must still fail fast."""
        started = []
        self._stub_startup(main_mod, monkeypatch, started)
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)

        with pytest.raises(SystemExit) as exc:
            main_mod.cmd_dashboard(_args(isolated=True))

        assert exc.value.code != 0
        assert started == []
