"""Integration tests for hybrid-routing agent construction and fallback.

Covers the two seams that wrap the pure classifier
(``hermes_cli.hybrid_routing``, unit-tested separately):

* ``_apply_hybrid_route`` — overrides a turn's route to the local endpoint on a
  LOCAL decision and snapshots the primary (cloud) route into ``_primary``.
* ``_init_agent_for_turn`` — rebuilds the agent for the turn's route and, when a
  hybrid-*local* route fails to initialize, degrades to the saved primary route
  instead of dropping the turn.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _import_cli():
    import hermes_cli.config as config_mod

    if not hasattr(config_mod, "save_env_value_secure"):
        config_mod.save_env_value_secure = lambda key, value: {
            "success": True,
            "stored_as": key,
            "validated": False,
        }

    import cli as cli_mod

    return cli_mod


def _routing_cfg(**overrides):
    cfg = {
        "enabled": True,
        "local": {
            "provider": "ollama",
            "model": "llama3.2:latest",
            "base_url": "http://localhost:11434/v1",
        },
        "complexity": {
            "max_prompt_chars": 1500,
            "max_prompt_tokens": 400,
            "cloud_keywords": ["refactor", "debug", "analyze"],
            "escalate_on_images": True,
            "escalate_on_code_fence": True,
        },
    }
    cfg.update(overrides)
    return cfg


def _primary_route():
    """The route dict as built by _resolve_turn_agent_config before hybrid."""
    runtime = {
        "api_key": "primary-key",
        "base_url": "https://api.deepseek.com/v1",
        "provider": "deepseek",
        "requested_provider": "deepseek",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    return {
        "model": "deepseek-v4-flash",
        "runtime": runtime,
        "signature": (
            "deepseek-v4-flash",
            "deepseek",
            "deepseek",
            "https://api.deepseek.com/v1",
            "chat_completions",
            None,
            (),
        ),
    }


class TestApplyHybridRoute(unittest.TestCase):
    def _stub(self, routing_cfg):
        cli_mod = _import_cli()
        return SimpleNamespace(
            provider="deepseek",
            config={"routing": routing_cfg},
            _route_force=None,
            # Static helper on HermesCLI; the stub calls self._turn_has_attachments.
            _turn_has_attachments=cli_mod.HermesCLI._turn_has_attachments,
        )

    def test_simple_prompt_overrides_to_local_and_snapshots_primary(self):
        cli_mod = _import_cli()
        stub = self._stub(_routing_cfg())
        route = _primary_route()

        fake_local = {
            "api_key": None,
            "base_url": "http://localhost:11434/v1",
            "provider": "ollama",
            "requested_provider": "ollama",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }
        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_local,
        ):
            cli_mod.HermesCLI._apply_hybrid_route(stub, route, "what time is it?")

        # Route now points at the local endpoint.
        self.assertEqual(route["model"], "llama3.2:latest")
        self.assertEqual(route["runtime"]["provider"], "ollama")
        self.assertEqual(route["runtime"]["base_url"], "http://localhost:11434/v1")
        # Keyless local gets the placeholder key.
        self.assertEqual(route["runtime"]["api_key"], "no-key-required")
        # Primary was snapshotted for fallback.
        self.assertIn("_primary", route)
        self.assertEqual(route["_primary"]["model"], "deepseek-v4-flash")
        self.assertEqual(route["_primary"]["runtime"]["provider"], "deepseek")

    def test_complex_prompt_keeps_primary_and_no_snapshot(self):
        cli_mod = _import_cli()
        stub = self._stub(_routing_cfg())
        route = _primary_route()

        cli_mod.HermesCLI._apply_hybrid_route(stub, route, "please refactor this parser")

        self.assertEqual(route["model"], "deepseek-v4-flash")
        # No fallback snapshot on a cloud turn — its presence is the local signal.
        self.assertNotIn("_primary", route)

    def test_unconfigured_local_is_noop(self):
        cli_mod = _import_cli()
        stub = self._stub(_routing_cfg(local={"provider": "", "model": "", "base_url": ""}))
        route = _primary_route()

        cli_mod.HermesCLI._apply_hybrid_route(stub, route, "hi")

        self.assertEqual(route["model"], "deepseek-v4-flash")
        self.assertNotIn("_primary", route)

    def test_moa_provider_is_noop(self):
        cli_mod = _import_cli()
        stub = self._stub(_routing_cfg())
        stub.provider = "moa"
        route = _primary_route()

        cli_mod.HermesCLI._apply_hybrid_route(stub, route, "hi")

        self.assertEqual(route["model"], "deepseek-v4-flash")
        self.assertNotIn("_primary", route)

    def test_resolution_error_falls_back_to_primary(self):
        cli_mod = _import_cli()
        stub = self._stub(_routing_cfg())
        route = _primary_route()

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=RuntimeError("boom"),
        ):
            cli_mod.HermesCLI._apply_hybrid_route(stub, route, "hi")

        # Never raises; leaves the primary route intact.
        self.assertEqual(route["model"], "deepseek-v4-flash")


class TestInitAgentForTurn(unittest.TestCase):
    def _local_route(self):
        route = _primary_route()
        # Simulate a hybrid-local route: local runtime + snapshotted primary.
        route["_primary"] = {
            "model": route["model"],
            "runtime": route["runtime"],
            "signature": route["signature"],
        }
        route["model"] = "llama3.2:latest"
        route["runtime"] = {
            "api_key": "no-key-required",
            "base_url": "http://localhost:11434/v1",
            "provider": "ollama",
            "requested_provider": "ollama",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }
        route["signature"] = (
            "llama3.2:latest",
            "ollama",
            "ollama",
            "http://localhost:11434/v1",
            "chat_completions",
            None,
            (),
        )
        return route

    def _stub(self):
        return SimpleNamespace(
            agent=None,
            _active_agent_route_signature=None,
            _init_agent=MagicMock(),
        )

    def test_local_success_no_fallback(self):
        cli_mod = _import_cli()
        stub = self._stub()
        stub._init_agent.return_value = True
        route = self._local_route()

        with patch.object(cli_mod, "_cprint"):
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route)

        self.assertTrue(ok)
        # Only one init call — the local route.
        stub._init_agent.assert_called_once()
        self.assertEqual(
            stub._init_agent.call_args.kwargs["model_override"], "llama3.2:latest"
        )

    def test_local_failure_falls_back_to_primary(self):
        cli_mod = _import_cli()
        stub = self._stub()
        # First (local) init fails, second (primary) init succeeds.
        stub._init_agent.side_effect = [False, True]
        route = self._local_route()

        with patch.object(cli_mod, "_cprint") as mock_cprint:
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route)

        self.assertTrue(ok)
        self.assertEqual(stub._init_agent.call_count, 2)
        # Second call used the primary model.
        self.assertEqual(
            stub._init_agent.call_args_list[1].kwargs["model_override"],
            "deepseek-v4-flash",
        )
        # User was told about the degradation.
        printed = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("falling back", printed.lower())

    def test_context_window_failure_degrades_to_cloud(self):
        """A local model below the 64K minimum raises in agent_init; the turn
        must degrade to the primary route, not crash or drop.

        Simulates the real failure mode: ``_init_agent`` returns False for the
        local route (it catches the ValueError from the context-length guard),
        so the fallback path kicks in.
        """
        cli_mod = _import_cli()
        stub = self._stub()

        def _init(*, model_override, runtime_override, request_overrides):
            # Local model rejected for small context; primary accepted.
            return model_override != "llama3.2:latest"

        stub._init_agent.side_effect = _init
        route = self._local_route()

        with patch.object(cli_mod, "_cprint"):
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route)

        self.assertTrue(ok)
        self.assertEqual(stub._init_agent.call_count, 2)
        self.assertEqual(
            stub._init_agent.call_args_list[-1].kwargs["model_override"],
            "deepseek-v4-flash",
        )

    def test_both_routes_fail_returns_false(self):
        cli_mod = _import_cli()
        stub = self._stub()
        stub._init_agent.side_effect = [False, False]
        route = self._local_route()

        with patch.object(cli_mod, "_cprint"):
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route)

        self.assertFalse(ok)
        self.assertEqual(stub._init_agent.call_count, 2)

    def test_non_local_turn_failure_has_no_fallback(self):
        cli_mod = _import_cli()
        stub = self._stub()
        stub._init_agent.return_value = False
        route = _primary_route()  # no _primary key → cloud turn

        with patch.object(cli_mod, "_cprint"):
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route)

        self.assertFalse(ok)
        # No fallback attempt.
        stub._init_agent.assert_called_once()

    def test_quiet_routes_notice_to_stderr(self):
        cli_mod = _import_cli()
        stub = self._stub()
        stub._init_agent.side_effect = [False, True]
        route = self._local_route()

        with (
            patch.object(cli_mod, "_cprint") as mock_cprint,
            patch("sys.stderr"),
        ):
            ok = cli_mod.HermesCLI._init_agent_for_turn(stub, route, quiet=True)

        self.assertTrue(ok)
        # Quiet mode: no "Initializing agent…" banner via _cprint.
        printed = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertNotIn("Initializing agent", printed)


if __name__ == "__main__":
    unittest.main()
