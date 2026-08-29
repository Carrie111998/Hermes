"""Local runtime-platform ownership checks for ``/api/status``."""

from __future__ import annotations

import pytest
import yaml


_MISSING = object()
_START_PROBE_MUST_NOT_RUN = object()


class TestStatusPlatformWriterIdentity:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as web_server

        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"discord": {"enabled": True}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(web_server, "check_config_version", lambda: (1, 1))
        monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None)
        monkeypatch.setattr(
            web_server, "_load_configured_gateway_platforms", lambda: {"discord"}
        )
        monkeypatch.setattr(
            web_server,
            "_collect_profile_gateway_topology_cached",
            lambda: {
                "profile_platforms": {},
                "profiles": ["default"],
                "gateway_mode": "single",
                "gateways": [],
            },
        )

        self.web_server = web_server
        self.client = TestClient(web_server.app)
        self.client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    @staticmethod
    def _runtime(pid=_MISSING, *, state="running", start_time=_MISSING):
        record = {
            "gateway_state": state,
            "platforms": {"discord": {"state": "connected"}},
            "exit_reason": None,
            "updated_at": "2026-08-28T00:00:00+00:00",
        }
        if pid is not _MISSING:
            record["pid"] = pid
        if start_time is not _MISSING:
            record["start_time"] = start_time
        return record

    def _get_status(self, monkeypatch, runtime, *, live_pid, live_start):
        monkeypatch.setattr(
            self.web_server, "get_running_pid_cached", lambda *args, **kwargs: live_pid
        )
        monkeypatch.setattr(
            self.web_server, "read_runtime_status", lambda *args, **kwargs: runtime
        )

        def _start_time(pid):
            assert live_start is not _START_PROBE_MUST_NOT_RUN
            return live_start

        monkeypatch.setattr(self.web_server, "_get_process_start_time", _start_time)
        return self.client.get("/api/status").json()

    def test_matching_local_writer_identity_is_surfaced(self, monkeypatch):
        data = self._get_status(
            monkeypatch,
            self._runtime(4242, start_time=111.0),
            live_pid=4242,
            live_start=111.0,
        )

        assert data["gateway_running"] is True
        assert data["gateway_pid"] == 4242
        assert data["gateway_platforms"] == {"discord": {"state": "connected"}}
        assert data["gateway_updated_at"] == "2026-08-28T00:00:00+00:00"

    @pytest.mark.parametrize(
        ("runtime_pid", "runtime_start", "live_pid", "live_start"),
        [
            pytest.param(
                4242,
                111.0,
                9999,
                _START_PROBE_MUST_NOT_RUN,
                id="pid-mismatch",
            ),
            pytest.param(4242, 111.0, 4242, 222.0, id="pid-reuse"),
            pytest.param(
                "4242",
                _MISSING,
                9999,
                _START_PROBE_MUST_NOT_RUN,
                id="string-pid",
            ),
        ],
    )
    def test_stale_local_writer_identity_is_suppressed(
        self,
        monkeypatch,
        runtime_pid,
        runtime_start,
        live_pid,
        live_start,
    ):
        data = self._get_status(
            monkeypatch,
            self._runtime(runtime_pid, start_time=runtime_start),
            live_pid=live_pid,
            live_start=live_start,
        )

        assert data["gateway_running"] is True
        assert data["gateway_pid"] == live_pid
        assert data["gateway_platforms"] == {}

    @pytest.mark.parametrize("runtime_pid", [_MISSING, None, "nonsense"])
    def test_missing_or_unparseable_runtime_pid_preserves_platforms(
        self, monkeypatch, runtime_pid
    ):
        data = self._get_status(
            monkeypatch,
            self._runtime(runtime_pid),
            live_pid=9999,
            live_start=_START_PROBE_MUST_NOT_RUN,
        )

        assert data["gateway_running"] is True
        assert data["gateway_platforms"] == {"discord": {"state": "connected"}}

    def test_remote_health_platforms_are_not_compared_to_local_processes(
        self, monkeypatch
    ):
        remote = self._runtime(4242, start_time=111.0)
        monkeypatch.setattr(
            self.web_server, "get_running_pid_cached", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            self.web_server, "read_runtime_status", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            self.web_server,
            "get_runtime_status_running_pid",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            self.web_server, "_GATEWAY_HEALTH_URL", "http://gateway/health"
        )
        monkeypatch.setattr(
            self.web_server, "_probe_gateway_health", lambda: (True, remote)
        )

        data = self.client.get("/api/status").json()

        assert data["gateway_running"] is True
        assert data["gateway_pid"] == 4242
        assert data["gateway_platforms"] == {"discord": {"state": "connected"}}

    def test_stopped_local_gateway_still_clears_platforms(self, monkeypatch):
        monkeypatch.setattr(
            self.web_server, "get_running_pid_cached", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            self.web_server,
            "get_runtime_status_running_pid",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            self.web_server,
            "read_runtime_status",
            lambda *args, **kwargs: self._runtime(4242, state="stopped"),
        )

        data = self.client.get("/api/status").json()

        assert data["gateway_running"] is False
        assert data["gateway_state"] == "stopped"
        assert data["gateway_platforms"] == {}


class TestStatusSourceLabelInvariant:
    """Diagnostic liveness labels must not select product behavior."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as web_server

        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"discord": {"enabled": True}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(web_server, "check_config_version", lambda: (1, 1))
        monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None)
        monkeypatch.setattr(
            web_server, "_load_configured_gateway_platforms", lambda: {"discord"}
        )
        monkeypatch.setattr(
            web_server,
            "_collect_profile_gateway_topology_cached",
            lambda: {
                "profile_platforms": {},
                "profiles": ["default"],
                "gateway_mode": "single",
                "gateways": [],
            },
        )

        self.web_server = web_server
        self.client = TestClient(web_server.app)
        self.client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    def _platforms_for_source(self, monkeypatch, source):
        from gateway.status import GatewayLiveness

        runtime = {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111.0,
            "platforms": {"discord": {"state": "connected"}},
            "exit_reason": None,
            "updated_at": "2026-08-28T00:00:00+00:00",
        }
        monkeypatch.setattr(
            self.web_server, "read_runtime_status", lambda *args, **kwargs: runtime
        )
        monkeypatch.setattr(
            self.web_server,
            "resolve_gateway_liveness",
            lambda *args, **kwargs: GatewayLiveness(
                running=True,
                pid=9999,
                source=source,
                health_body=None,
            ),
        )

        return self.client.get("/api/status").json()["gateway_platforms"]

    def test_remote_authority_is_invariant_across_diagnostic_sources(self, monkeypatch):
        sources = (
            "pid",
            "health",
            "runtime_status",
            "none",
            "remote_registry",
            "future_remote_mesh",
        )

        platforms_by_source = {
            source: self._platforms_for_source(monkeypatch, source)
            for source in sources
        }

        assert platforms_by_source == {
            source: {"discord": {"state": "connected"}} for source in sources
        }

    def test_source_invariant_text_remains_in_status_module(self):
        from pathlib import Path

        import gateway.status as status_module

        text = Path(status_module.__file__).read_text(encoding="utf-8")
        assert "never branch product behavior on it" in text
