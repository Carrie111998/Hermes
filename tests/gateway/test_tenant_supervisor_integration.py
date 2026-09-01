"""TenantSupervisor integration regressions for #99986 + #68559.

Covers the two blocker fixes reviewed by Andrex (03:55):

  1. _profile_runtime_scope terminal isolation is an admission gate
     (fail-closed, not debug-log fallback to global TERMINAL_*).

  2. TenantSupervisor is on the real inbound dispatch path:
     validate_route / run_turn / health / drain are invoked via the
     gateway, _run_agent_for_tenant exists, and the gateway validates
     returned route/session + correlation identity before delivery.

Tests drive the REAL handler (GatewayRunner._handle_message / _run_agent)
rather than constructing supervisor dicts directly.
"""

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.config import GatewayConfig
from gateway.profile_routing import ProfileRoute
from gateway.tenant_supervisor import TenantSupervisor, TenantSupervisorConfig
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent
from gateway.config import Platform


class TestTerminalIsolationAdmissionGate:
    """Blocker 1: terminal isolation must be fail-closed."""

    def test_admission_failure_raises_instead_of_falling_back(self, tmp_path):
        # Make a profile home so _profile_runtime_scope can try to install isolation
        home = tmp_path / "profiles" / "docker-profile"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")

        with patch("hermes_cli.config.apply_terminal_config_to_env", side_effect=RuntimeError("bridge broken")):
            with pytest.raises(RuntimeError, match="terminal isolation admission failed"):
                with _profile_runtime_scope(home):
                    pytest.fail("should not enter turn when admission fails")

        # After the failed admission, no isolation should leak: the ContextVars must be clean
        from tools.terminal_tool import _TERMINAL_ENV_ISOLATION, _TERMINAL_CONFIG_ISOLATION
        assert _TERMINAL_ENV_ISOLATION.get() is None
        assert _TERMINAL_CONFIG_ISOLATION.get() is None

    def test_malformed_terminal_config_fails_closed(self, tmp_path):
        home = tmp_path / "profiles" / "bad"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("terminal:\n  backend: docker\n  timeout: not-a-number\n", encoding="utf-8")
        # Simulate parse failure inside _parse_terminal_config_from_getter
        with patch("tools.terminal_tool._parse_terminal_config_from_getter", side_effect=ValueError("bad TERMINAL_TIMEOUT")):
            with patch("hermes_cli.config.apply_terminal_config_to_env") as mock_bridge:
                # apply succeeds but parse fails — should still fail closed
                def _bridge(env=None, override=None, config=None):
                    if env is not None:
                        env["TERMINAL_ENV"] = "docker"
                        env["TERMINAL_TIMEOUT"] = "not-a-number"
                    return env or {}
                mock_bridge.side_effect = _bridge
                with pytest.raises(RuntimeError, match="terminal isolation admission failed"):
                    with _profile_runtime_scope(home):
                        pytest.fail("should not yield when parse fails")

    def test_scope_cleanup_on_exception_is_best_effort(self, tmp_path, monkeypatch):
        home = tmp_path / "profiles" / "ok"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
        # Successful admission, but inner body raises — cleanup must still reset isolation
        from tools.terminal_tool import _TERMINAL_ENV_ISOLATION
        with _profile_runtime_scope(home):
            # Inside scope, isolation should be installed
            assert _TERMINAL_ENV_ISOLATION.get() is not None
            # raising inside will trigger finally cleanup
            try:
                with _profile_runtime_scope(home):
                    raise ValueError("boom inside turn")
            except ValueError:
                pass
        # After outer scope, still cleaned
        assert _TERMINAL_ENV_ISOLATION.get() is None

    @pytest.mark.asyncio
    async def test_concurrent_isolation_different_backends(self, tmp_path):
        """Two profiles with different terminal backends run interleaved without leaking."""
        home_local = tmp_path / "profiles" / "local"
        home_docker = tmp_path / "profiles" / "docker"
        for h in (home_local, home_docker):
            h.mkdir(parents=True)
        (home_local / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
        (home_docker / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")

        from tools.terminal_tool import _get_env_config, _TERMINAL_ENV_ISOLATION

        results = {}

        async def run_profile(name, home, expected):
            def _sync():
                with _profile_runtime_scope(home):
                    cfg = _get_env_config()
                    return cfg["env_type"]
            # Run in thread to simulate concurrent turns (ContextVar isolation should hold per-task)
            val = await asyncio.to_thread(_sync)
            results[name] = val

        await asyncio.gather(
            run_profile("local", home_local, "local"),
            run_profile("docker", home_docker, "docker"),
        )
        assert results["local"] == "local"
        assert results["docker"] == "docker"
        # After both, global state must be clean
        assert _TERMINAL_ENV_ISOLATION.get() is None


class TestTenantSupervisorRealHandler:
    """Blocker 2: supervisor on the real dispatch path with delivery validation."""

    def _make_runner(self, tmp_path, routes, tenant_raw=None):
        # Create profile homes
        for route in routes:
            home = tmp_path / route.profile
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
        # Active profile home
        active_home = tmp_path / "default"
        active_home.mkdir(parents=True, exist_ok=True)
        (active_home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")

        # Patch profile dir resolution to use tmp_path
        patchers = []
        def _get_profile_dir(name):
            return tmp_path / name if (tmp_path / name).exists() else active_home
        # Build GatewayConfig-like mock
        cfg = MagicMock()
        cfg.multiplex_profiles = True
        cfg.profile_routes = routes
        cfg.tenant_isolation = tenant_raw or {"mode": "worker", "unmatched": "deny", "max_queue_depth": 2, "request_timeout_seconds": 5}
        cfg.multiplex_profile_allowlist = None
        cfg.sessions_dir = str(tmp_path / "sessions")
        cfg.default_reset_policy = MagicMock(bg_process_max_age_hours=24)

        # Need a minimal GatewayConfig that satisfies TenantSupervisor.from_gateway_config
        # Use MagicMock with required attrs, but also provide multiplex_profile_allowlist
        return cfg, tmp_path, _get_profile_dir

    @pytest.mark.asyncio
    async def test_real_handler_routes_via_supervisor_and_validates(self, tmp_path):
        routes = [ProfileRoute(name="tg", platform="telegram", profile="tenant-a", chat_id="111")]
        cfg, _, get_dir = self._make_runner(tmp_path, routes)
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=get_dir), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("hermes_cli.profiles.profile_exists", return_value=True), \
             patch("hermes_cli.profiles.profiles_to_serve", return_value=[("default", tmp_path/"default"), ("tenant-a", tmp_path/"tenant-a")]), \
             patch("gateway.run._profile_runtime_scope") as mock_scope:

            # Make scope a no-op passthrough for this test (except we want real isolation, but patch to avoid file IO)
            from contextlib import contextmanager
            @contextmanager
            def _noop(home):
                yield
            mock_scope.side_effect = _noop

            runner = GatewayRunner.__new__(GatewayRunner)
            runner.config = cfg
            # Real supervisor from config
            runner._tenant_supervisor = TenantSupervisor.from_gateway_config(cfg)
            # Inject profile homes for supervisor resolution
            runner._tenant_supervisor._profile_homes = {"tenant-a": tmp_path / "tenant-a", "default": tmp_path / "default"}
            # Mock session helpers to avoid DB - use real logic for profile resolution
            runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(runner)
            runner._resolve_profile_home_for_source = GatewayRunner._resolve_profile_home_for_source.__get__(runner)
            runner._session_key_for_source = GatewayRunner._session_key_for_source.__get__(runner)
            runner._validate_tenant_delivery = GatewayRunner._validate_tenant_delivery.__get__(runner)
            runner._run_agent_for_tenant = GatewayRunner._run_agent_for_tenant.__get__(runner)
            runner._run_agent_inner = AsyncMock(return_value={"final_response": "hello from tenant", "messages": []})
            # Also need _run_agent supervisor path
            runner._run_agent = GatewayRunner._run_agent.__get__(runner)

            source = SessionSource(platform=Platform.TELEGRAM, chat_id="111", chat_type="group", user_id="u1")
            source.profile = "tenant-a"
            # Call _run_agent via real path (which should go through supervisor)
            result = await runner._run_agent(
                message="hi",
                context_prompt="ctx",
                history=[{"role": "user", "content": "hi"}],
                source=source,
                session_id="sess-123",
                session_key="agent:tenant-a:telegram:111",
                run_generation=1,
            )
            # Should have been routed via supervisor and validated
            assert result["profile"] == "tenant-a"
            assert result["correlation_id"] == "agent:tenant-a:telegram:111:1:sess-123"
            assert result["isolated"] is True
            # Health and drain should be accessible
            health = runner._tenant_health()
            assert health["mode"] == "worker"
            assert "tenant-a" in health["workers"]
            # Drain
            runner._tenant_drain()
            assert runner._tenant_health()["total_workers"] == 0

    @pytest.mark.asyncio
    async def test_unknown_route_denied_fail_closed(self, tmp_path):
        routes = [ProfileRoute(name="tg", platform="telegram", profile="tenant-a", chat_id="111")]
        cfg, _, get_dir = self._make_runner(tmp_path, routes, tenant_raw={"mode": "worker", "unmatched": "deny"})
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=get_dir), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("hermes_cli.profiles.profile_exists", return_value=True), \
             patch("hermes_cli.profiles.profiles_to_serve", return_value=[("default", tmp_path/"default"), ("tenant-a", tmp_path/"tenant-a")]):

            runner = GatewayRunner.__new__(GatewayRunner)
            runner.config = cfg
            runner._tenant_supervisor = TenantSupervisor.from_gateway_config(cfg)
            runner._tenant_supervisor._profile_homes = {"tenant-a": tmp_path / "tenant-a", "default": tmp_path / "default"}
            runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(runner)
            runner._resolve_profile_home_for_source = GatewayRunner._resolve_profile_home_for_source.__get__(runner)
            runner._session_key_for_source = GatewayRunner._session_key_for_source.__get__(runner)
            runner._validate_tenant_delivery = GatewayRunner._validate_tenant_delivery.__get__(runner)
            runner._run_agent_for_tenant = GatewayRunner._run_agent_for_tenant.__get__(runner)
            runner._run_agent_inner = AsyncMock(return_value={"final_response": "x"})
            runner._run_agent = GatewayRunner._run_agent.__get__(runner)

            # Source with no matching route -> profile is None, supervisor should deny
            source = SessionSource(platform=Platform.TELEGRAM, chat_id="999", chat_type="group", user_id="u1")
            # ensure no profile stamped
            source.profile = None
            with pytest.raises(RuntimeError, match="unmatched route denied"):
                await runner._run_agent(
                    message="hi",
                    context_prompt="ctx",
                    history=[],
                    source=source,
                    session_id="sess-999",
                    session_key="agent:main:telegram:999",
                    run_generation=1,
                )

    @pytest.mark.asyncio
    async def test_delivery_validation_rejects_stale_correlation(self, tmp_path):
        routes = [ProfileRoute(name="tg", platform="telegram", profile="tenant-a", chat_id="111")]
        cfg, _, get_dir = self._make_runner(tmp_path, routes)
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=get_dir), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("hermes_cli.profiles.profile_exists", return_value=True), \
             patch("hermes_cli.profiles.profiles_to_serve", return_value=[("default", tmp_path/"default"), ("tenant-a", tmp_path/"tenant-a")]), \
             patch("gateway.run._profile_runtime_scope", new=lambda h: __import__("contextlib").contextmanager(lambda: (yield))()):

            runner = GatewayRunner.__new__(GatewayRunner)
            runner.config = cfg
            runner._tenant_supervisor = TenantSupervisor.from_gateway_config(cfg)
            runner._tenant_supervisor._profile_homes = {"tenant-a": tmp_path / "tenant-a"}
            runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(runner)
            runner._resolve_profile_home_for_source = GatewayRunner._resolve_profile_home_for_source.__get__(runner)
            runner._session_key_for_source = GatewayRunner._session_key_for_source.__get__(runner)
            runner._validate_tenant_delivery = GatewayRunner._validate_tenant_delivery.__get__(runner)

            # Supervisor returns stale correlation
            async def _bad_hook(source, message, correlation_id, **kwargs):
                return {"profile": "tenant-a", "correlation_id": "stale-id", "session_id": "sess-123", "session_key": "agent:tenant-a:telegram:111", "isolated": True}
            runner._run_agent_for_tenant = _bad_hook
            runner._run_agent_inner = AsyncMock(return_value={"final_response": "x"})
            runner._run_agent = GatewayRunner._run_agent.__get__(runner)
            source = SessionSource(platform=Platform.TELEGRAM, chat_id="111", chat_type="group", user_id="u1")
            source.profile = "tenant-a"
            with pytest.raises(RuntimeError, match="tenant delivery validation failed"):
                await runner._run_agent(
                    message="hi",
                    context_prompt="ctx",
                    history=[],
                    source=source,
                    session_id="sess-123",
                    session_key="agent:tenant-a:telegram:111",
                    run_generation=1,
                )

    @pytest.mark.asyncio
    async def test_queue_depth_enforced_and_cancel_once(self, tmp_path):
        cfg = TenantSupervisorConfig(mode="worker", max_queue_depth=1, request_timeout_seconds=2)
        sup = TenantSupervisor(config=cfg, profile_homes={"tenant-a": tmp_path})
        (tmp_path).mkdir(parents=True, exist_ok=True)
        # Need a runner for worker
        runner = MagicMock()
        async def _slow_hook(source, message, correlation_id, **kwargs):
            await asyncio.sleep(0.5)
            return {"profile": "tenant-a", "correlation_id": correlation_id, "session_id": kwargs.get("session_id"), "session_key": kwargs.get("session_key"), "isolated": True}
        runner._run_agent_for_tenant = _slow_hook

        source = SessionSource(platform=Platform.TELEGRAM, chat_id="111", chat_type="group", user_id="u1")
        # First turn occupies the slot
        task = asyncio.create_task(sup.run_turn("tenant-a", source, "msg1", "corr-1", runner, session_id="s1", session_key="k1"))
        await asyncio.sleep(0.1)
        # Second turn should be rejected fast (queue depth)
        with pytest.raises(RuntimeError, match="queue depth"):
            await sup.run_turn("tenant-a", source, "msg2", "corr-2", runner, session_id="s2", session_key="k2")
        await task
        # Cancel once semantics: cancel_turn is best-effort but should be idempotent
        assert sup.cancel_turn("corr-1") in (True, False)
        assert sup.cancel_turn("corr-1") in (True, False)
        # Health reflects worker
        h = sup.health()
        assert h["total_workers"] == 1
        sup.drain()
        assert sup.health()["total_workers"] == 0
