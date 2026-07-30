"""Regression for #74973 — `hermes update` must not leave the gateway down.

On macOS the update's launchd branch guarded the restart behind
``launchctl list <label>`` exiting 0. A job that has been *booted out* of
launchd exits non-zero there, so the whole restart branch was skipped — with
no ``else`` and no message. The update printed ``✓ Update complete!`` and
exited 0 while the gateway was stopped *and* deregistered, which ``KeepAlive``
cannot recover because the job definition is gone. Messaging adapters and
cron stayed dark until someone manually ran ``hermes gateway restart``.

The state the guard screened out was precisely the state needing recovery:
``launchd_start()`` already bootstraps an unloaded plist.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd


class _FakePlist:
    def __init__(self, exists: bool = True) -> None:
        self._exists = exists

    def exists(self) -> bool:
        return self._exists


@pytest.fixture
def launchd(monkeypatch):
    """Stub hermes_cli.gateway so no real launchctl call is made."""
    calls: list[str] = []
    state = {"plist": _FakePlist(True), "returncode": 0, "run_exc": None}

    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "get_launchd_label", lambda: "ai.hermes.gateway", raising=False)
    monkeypatch.setattr(gateway_mod, "get_launchd_plist_path", lambda: state["plist"], raising=False)
    monkeypatch.setattr(gateway_mod, "launchd_restart", lambda: calls.append("restart"), raising=False)
    monkeypatch.setattr(gateway_mod, "launchd_start", lambda: calls.append("start"), raising=False)

    def fake_run(*args, **kwargs):
        if state["run_exc"] is not None:
            raise state["run_exc"]
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=state["returncode"], stdout="", stderr=""
        )

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    return calls, state


class TestLaunchdRestartAfterUpdate:
    def test_loaded_job_is_restarted(self, launchd, capsys):
        calls, state = launchd
        state["returncode"] = 0

        assert update_cmd._restart_launchd_gateway_after_update() == ["ai.hermes.gateway"]
        assert calls == ["restart"]
        assert "NOT running" not in capsys.readouterr().out

    def test_unloaded_job_is_reloaded_not_skipped(self, launchd, capsys):
        """The #74973 case: booted out, plist still installed."""
        calls, state = launchd
        state["returncode"] = 1  # `launchctl list` on a booted-out job

        restarted = update_cmd._restart_launchd_gateway_after_update()

        # launchd_start() bootstraps the definition; launchd_restart() would
        # assume a live process to drain and is the wrong call here.
        assert calls == ["start"]
        assert restarted == ["ai.hermes.gateway"]
        out = capsys.readouterr().out
        assert "unloaded from launchd" in out

    def test_restart_failure_warns_that_gateway_is_down(self, launchd, capsys):
        calls, state = launchd
        state["returncode"] = 0

        import hermes_cli.gateway as gateway_mod

        def boom():
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["launchctl", "kickstart"], stderr="kickstart refused"
            )

        gateway_mod.launchd_restart = boom

        assert update_cmd._restart_launchd_gateway_after_update() == []
        out = capsys.readouterr().out
        assert "Gateway restart failed" in out
        assert "kickstart refused" in out
        assert "hermes gateway restart" in out

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("launchctl"),
            subprocess.TimeoutExpired(cmd=["launchctl", "list"], timeout=5),
        ],
    )
    def test_launchctl_unusable_is_not_swallowed(self, launchd, capsys, exc):
        """A missing binary or a timeout used to `pass` silently."""
        calls, state = launchd
        state["run_exc"] = exc

        assert update_cmd._restart_launchd_gateway_after_update() == []
        assert calls == []
        out = capsys.readouterr().out
        assert "Could not restart the gateway" in out
        assert "hermes gateway restart" in out

    def test_no_plist_is_not_a_launchd_install(self, launchd, capsys):
        """No service definition → nothing to restart, and nothing to warn about."""
        calls, state = launchd
        state["plist"] = _FakePlist(False)

        assert update_cmd._restart_launchd_gateway_after_update() == []
        assert calls == []
        assert capsys.readouterr().out == ""
