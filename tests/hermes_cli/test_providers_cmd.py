"""CLI tests for the ``hermes providers`` subcommand.

Hermetic: provider detection, pricing fetches, and config reads are all
monkeypatched — no network, no credentials, no real ~/.hermes access.
"""
from types import SimpleNamespace

import pytest

from hermes_cli import providers_cmd as pc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

FAKE_AUTH_ROWS = [
    {
        "slug": "openrouter",
        "name": "OpenRouter",
        "is_current": True,
        "is_user_defined": False,
        "models": ["deepseek/deepseek-chat", "qwen/qwen3.8-27b"],
        "total_models": 2,
        "source": "built-in",
    }
]

FAKE_CANONICAL = [
    SimpleNamespace(slug="openrouter", label="OpenRouter", tui_desc="x"),
    SimpleNamespace(slug="anthropic", label="Anthropic", tui_desc="y"),
    SimpleNamespace(slug="nous", label="Nous Portal", tui_desc="z"),
]

FAKE_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "nous": "Nous Portal",
}

FAKE_REGISTRY = {
    "openrouter": SimpleNamespace(auth_type="api_key", api_key_env_vars=("OPENROUTER_API_KEY",)),
    "anthropic": SimpleNamespace(auth_type="api_key", api_key_env_vars=("ANTHROPIC_API_KEY",)),
    "nous": SimpleNamespace(auth_type="oauth_device_code", api_key_env_vars=()),
}


class FakeOverlay:
    def __init__(self, transport):
        self.transport = transport


FAKE_OVERLAYS = {
    "openrouter": FakeOverlay("openai_chat"),
    "anthropic": FakeOverlay("anthropic_messages"),
}

FAKE_ROWS = [
    {
        "id": "qwen/qwen3.8-27b",
        "lab": "qwen",
        "name": "Qwen: Qwen3.8 27B",
        "context": 262144,
        "in": 0.45,
        "out": 3.2,
        "agentic": True,
        "reasoning": True,
    },
    {
        "id": "deepseek/deepseek-chat",
        "lab": "deepseek",
        "name": "DeepSeek: DeepSeek Chat",
        "context": 64000,
        "in": 0.27,
        "out": 1.10,
        "agentic": True,
        "reasoning": False,
    },
]


def args(**overrides):
    """Namespace shaped like the argparse result for providers subcommands."""
    base = dict(
        providers_command=None,
        offline=False,
        top=10,
        min_context=0,
        task=None,
        include_all=False,
        query="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# build_list_rows / format_list_rows (pure)
# ---------------------------------------------------------------------------


class TestBuildListRows:
    def test_merges_auth_rows_with_canonical_skeletons(self):
        rows = pc.build_list_rows(
            FAKE_AUTH_ROWS, FAKE_CANONICAL, FAKE_LABELS, FAKE_REGISTRY, FAKE_OVERLAYS
        )
        by_slug = {r["slug"]: r for r in rows}
        assert set(by_slug) == {"openrouter", "anthropic", "nous"}
        # Auth row keeps its data and gains transport/auth metadata.
        assert by_slug["openrouter"]["authenticated"] is True
        assert by_slug["openrouter"]["total_models"] == 2
        assert by_slug["openrouter"]["transport"] == "openai_chat"
        assert by_slug["openrouter"]["key_env"] == "OPENROUTER_API_KEY"
        # Canonical skeletons are unauthenticated.
        assert by_slug["anthropic"]["authenticated"] is False
        assert by_slug["anthropic"]["transport"] == "anthropic_messages"
        assert by_slug["anthropic"]["key_env"] == "ANTHROPIC_API_KEY"
        assert by_slug["nous"]["auth_type"] == "oauth_device_code"

    def test_empty_auth_rows_still_lists_canonical(self):
        rows = pc.build_list_rows([], FAKE_CANONICAL, FAKE_LABELS, FAKE_REGISTRY, FAKE_OVERLAYS)
        assert {r["slug"] for r in rows} == {"openrouter", "anthropic", "nous"}

    def test_current_flag_survives(self):
        rows = pc.build_list_rows(
            FAKE_AUTH_ROWS, FAKE_CANONICAL, FAKE_LABELS, FAKE_REGISTRY, FAKE_OVERLAYS
        )
        by_slug = {r["slug"]: r for r in rows}
        assert by_slug["openrouter"]["is_current"] is True


class TestFormatListRows:
    def test_renders_table_with_auth_column(self):
        rows = pc.build_list_rows(
            FAKE_AUTH_ROWS, FAKE_CANONICAL, FAKE_LABELS, FAKE_REGISTRY, FAKE_OVERLAYS
        )
        out = "\n".join(pc.format_list_rows(rows))
        assert "PROVIDER" in out and "AUTH" in out and "TRANSPORT" in out
        assert "* OpenRouter" in out
        assert "yes" in out and "no" in out
        assert "openai_chat" in out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_defaults_to_list(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_list", lambda a: called.append("list"))
        pc.cmd_providers(args(providers_command=None))
        assert called == ["list"]

    def test_ls_alias(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_list", lambda a: called.append("list"))
        pc.cmd_providers(args(providers_command="ls"))
        assert called == ["list"]

    def test_compare_and_cmp(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_compare", lambda a: called.append("compare"))
        pc.cmd_providers(args(providers_command="compare"))
        pc.cmd_providers(args(providers_command="cmp"))
        assert called == ["compare", "compare"]

    def test_search_and_best(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_search", lambda a: called.append("search"))
        monkeypatch.setattr(pc, "_cmd_best", lambda a: called.append("best"))
        pc.cmd_providers(args(providers_command="search"))
        pc.cmd_providers(args(providers_command="best"))
        assert called == ["search", "best"]

    def test_unknown_subcommand_prints_hint(self, capsys):
        pc.cmd_providers(args(providers_command="nope"))
        captured = capsys.readouterr().out
        assert "Unknown providers subcommand" in captured


# ---------------------------------------------------------------------------
# _cmd_compare / _cmd_search / _cmd_best (fetches monkeypatched)
# ---------------------------------------------------------------------------


class TestValueCommands:
    def test_compare_prints_ranked_table(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_openrouter_models", lambda: FAKE_ROWS
        )
        pc._cmd_compare(args(providers_command="compare", top=10))
        out = capsys.readouterr().out
        assert "Top 2 value picks" in out
        # Cheapest first (deepseek 1.37/M vs qwen 3.65/M).
        assert out.index("deepseek/deepseek-chat") < out.index("qwen/qwen3.8-27b")
        assert "source: live OpenRouter API" in out

    def test_compare_offline_source_label(self, monkeypatch, capsys):
        monkeypatch.setattr("hermes_cli.provider_pricing.offline_rows", lambda: FAKE_ROWS)
        pc._cmd_compare(args(providers_command="compare", offline=True))
        out = capsys.readouterr().out
        assert "source: bundled models.dev catalog" in out

    def test_compare_no_data_hint(self, monkeypatch, capsys):
        monkeypatch.setattr("hermes_cli.provider_pricing.fetch_openrouter_models", lambda: [])
        pc._cmd_compare(args(providers_command="compare"))
        out = capsys.readouterr().out
        assert "Could not load the OpenRouter model catalog" in out
        assert "--offline" in out

    def test_search_prints_matches(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_openrouter_models", lambda: FAKE_ROWS
        )
        pc._cmd_search(args(providers_command="search", query="deepseek"))
        out = capsys.readouterr().out
        assert "1 match(es) for 'deepseek'" in out
        assert "deepseek/deepseek-chat" in out
        assert "qwen/qwen3.8-27b" not in out

    def test_best_prints_apply_commands(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_openrouter_models", lambda: FAKE_ROWS
        )
        pc._cmd_best(args(providers_command="best", task="chat", top=5))
        out = capsys.readouterr().out
        assert "Best-value models for task='chat'" in out
        assert "hermes config set model.provider openrouter" in out
        assert "hermes config set model.default deepseek/deepseek-chat" in out

    def test_best_task_reasoning_filters(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_openrouter_models", lambda: FAKE_ROWS
        )
        pc._cmd_best(args(providers_command="best", task="reasoning", top=5))
        out = capsys.readouterr().out
        assert "qwen/qwen3.8-27b" in out
        assert "deepseek/deepseek-chat" not in out

    def test_best_no_matches_hint(self, monkeypatch, capsys):
        monkeypatch.setattr("hermes_cli.provider_pricing.offline_rows", lambda: [])
        pc._cmd_best(args(providers_command="best", offline=True, task="chat"))
        out = capsys.readouterr().out
        assert "Could not load the OpenRouter model catalog" in out


# ---------------------------------------------------------------------------
# Parser wiring (imports main — the rest of the suite already does)
# ---------------------------------------------------------------------------


class TestParserWiring:
    def test_providers_in_builtin_subcommands(self):
        from hermes_cli.main import _BUILTIN_SUBCOMMANDS

        assert "providers" in _BUILTIN_SUBCOMMANDS


# ---------------------------------------------------------------------------
# endpoints / route
# ---------------------------------------------------------------------------

FAKE_ENDPOINT_ROWS = [
    {
        "provider": "DeepInfra",
        "in": 0.09,
        "out": 0.18,
        "cache": 0.018,
        "discount_pct": None,
        "context": 1048576,
        "latency": 0.54,
        "throughput": 53,
        "uptime": 99.93,
    },
    {
        "provider": "Decart",
        "in": 0.0657,
        "out": 0.1314,
        "cache": 0.01314,
        "discount_pct": 27,
        "context": 1048576,
        "latency": None,
        "throughput": None,
        "uptime": 98.86,
    },
]


def route_args(**overrides):
    base = dict(
        providers_command="route",
        sort=None,
        order=None,
        only=None,
        ignore=None,
        require_parameters=False,
        data_collection=None,
        clear=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEndpointsCommand:
    def test_prints_table_with_routing_tip(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_model_endpoints",
            lambda model: FAKE_ENDPOINT_ROWS,
        )
        pc._cmd_endpoints(args(providers_command="endpoints", model="deepseek/deepseek-v4-flash"))
        out = capsys.readouterr().out
        assert "Providers serving deepseek/deepseek-v4-flash" in out
        assert "DeepInfra" in out and "Decart" in out
        assert "route --sort price" in out

    def test_fetch_failure_prints_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.provider_pricing.fetch_model_endpoints", lambda model: []
        )
        pc._cmd_endpoints(args(providers_command="endpoints", model="nope/nope"))
        out = capsys.readouterr().out
        assert "Could not fetch endpoint data for 'nope/nope'" in out
        assert "--order DeepInfra" in out

    def test_dispatch_endpoints_and_endpoint_alias(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_endpoints", lambda a: called.append("endpoints"))
        pc.cmd_providers(args(providers_command="endpoints"))
        pc.cmd_providers(args(providers_command="endpoint"))
        assert called == ["endpoints", "endpoints"]

    def test_dispatch_route(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc, "_cmd_route", lambda a: called.append("route"))
        pc.cmd_providers(route_args())
        assert called == ["route"]


class TestParseCsv:
    def test_basic(self):
        assert pc._parse_csv("DeepInfra, Decart ,  ") == ["DeepInfra", "Decart"]

    def test_empty(self):
        assert pc._parse_csv("") == []


class TestRouteCommand:
    def _fake_config_store(self, monkeypatch):
        store = {}

        def fake_load():
            return store

        def fake_save(cfg):
            data = dict(cfg)
            store.clear()
            store.update(data)

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load)
        monkeypatch.setattr("hermes_cli.config.save_config", fake_save)
        return store

    def test_set_sort_writes_config(self, monkeypatch, capsys):
        store = self._fake_config_store(monkeypatch)
        pc._cmd_route(route_args(sort="price"))
        assert store.get("provider_routing") == {"sort": "price"}
        out = capsys.readouterr().out
        assert "provider_routing updated" in out
        assert "sort: price" in out
        assert "next session" in out

    def test_set_order_parses_csv(self, monkeypatch):
        store = self._fake_config_store(monkeypatch)
        pc._cmd_route(route_args(order="DeepInfra,Decart"))
        assert store["provider_routing"]["order"] == ["DeepInfra", "Decart"]

    def test_merge_preserves_existing_keys(self, monkeypatch):
        store = self._fake_config_store(monkeypatch)
        store["provider_routing"] = {"only": ["Anthropic"]}
        pc._cmd_route(route_args(sort="price"))
        assert store["provider_routing"] == {"only": ["Anthropic"], "sort": "price"}

    def test_clear_removes_section(self, monkeypatch):
        store = self._fake_config_store(monkeypatch)
        store["provider_routing"] = {"sort": "price"}
        pc._cmd_route(route_args(clear=True))
        assert "provider_routing" not in store

    def test_no_args_shows_current(self, monkeypatch, capsys):
        store = self._fake_config_store(monkeypatch)
        store["provider_routing"] = {"sort": "price"}
        pc._cmd_route(route_args())
        out = capsys.readouterr().out
        assert "Current provider_routing" in out
        assert "price" in out
        assert "provider_routing updated" not in out

    def test_no_args_when_unset_shows_hints(self, monkeypatch, capsys):
        self._fake_config_store(monkeypatch)
        pc._cmd_route(route_args())
        out = capsys.readouterr().out
        assert "provider_routing is not set" in out
        assert "--sort price" in out

    def test_require_parameters_and_data_collection(self, monkeypatch):
        store = self._fake_config_store(monkeypatch)
        pc._cmd_route(route_args(require_parameters=True, data_collection="deny"))
        assert store["provider_routing"]["require_parameters"] is True
        assert store["provider_routing"]["data_collection"] == "deny"


# ---------------------------------------------------------------------------
# /provider slash command — parse + apply
# ---------------------------------------------------------------------------


class TestParseProviderSlashArgs:
    def test_bare(self):
        assert pc.parse_provider_slash_args("") == {"global": False}

    def test_bare_global_flag_only(self):
        assert pc.parse_provider_slash_args("--global") == {"global": True}

    def test_single_provider_forces_order(self):
        assert pc.parse_provider_slash_args("DeepInfra") == {
            "order": ["DeepInfra"],
            "global": False,
        }

    def test_csv_providers(self):
        assert pc.parse_provider_slash_args("DeepInfra, Decart") == {
            "order": ["DeepInfra", "Decart"],
            "global": False,
        }

    def test_sort(self):
        assert pc.parse_provider_slash_args("sort price") == {"sort": "price", "global": False}
        assert pc.parse_provider_slash_args("sort latency") == {"sort": "latency", "global": False}

    def test_sort_invalid_raises(self):
        with pytest.raises(ValueError):
            pc.parse_provider_slash_args("sort cheapest")

    def test_only_and_ignore(self):
        assert pc.parse_provider_slash_args("only Anthropic, Google") == {
            "only": ["Anthropic", "Google"],
            "global": False,
        }
        assert pc.parse_provider_slash_args("ignore DeepInfra") == {
            "ignore": ["DeepInfra"],
            "global": False,
        }

    def test_require_parameters(self):
        assert pc.parse_provider_slash_args("require-parameters") == {
            "require_parameters": True,
            "global": False,
        }

    def test_data_collection(self):
        assert pc.parse_provider_slash_args("data-collection deny") == {
            "data_collection": "deny",
            "global": False,
        }

    def test_data_collection_invalid_raises(self):
        with pytest.raises(ValueError):
            pc.parse_provider_slash_args("data-collection maybe")

    def test_clear_variants(self):
        for value in ("clear", "reset", "off"):
            assert pc.parse_provider_slash_args(value) == {"clear": True, "global": False}

    def test_global_flag(self):
        assert pc.parse_provider_slash_args("DeepInfra --global") == {
            "order": ["DeepInfra"],
            "global": True,
        }
        assert pc.parse_provider_slash_args("--global DeepInfra") == {
            "order": ["DeepInfra"],
            "global": True,
        }
        assert pc.parse_provider_slash_args("clear --global") == {"clear": True, "global": True}


class TestApplySessionProviderRouting:
    def _session(self):
        return SimpleNamespace(
            _providers_order=None,
            _provider_sort=None,
            _providers_only=None,
            _providers_ignore=None,
            _provider_require_params=False,
            _provider_data_collection=None,
        )

    def _agent(self):
        return SimpleNamespace(
            providers_order=None,
            provider_sort=None,
            providers_allowed=None,
            providers_ignored=None,
            provider_require_parameters=False,
            provider_data_collection=None,
        )

    def test_order_applied_to_session_and_agent(self):
        session, agent = self._session(), self._agent()
        changes = pc.apply_session_provider_routing(session, agent, {"order": ["DeepInfra"]})
        assert session._providers_order == ["DeepInfra"]
        assert agent.providers_order == ["DeepInfra"]
        assert changes == ["order=['DeepInfra']"]

    def test_sort_and_ignore(self):
        session, agent = self._session(), self._agent()
        pc.apply_session_provider_routing(session, agent, {"sort": "price", "ignore": ["X"]})
        assert session._provider_sort == "price"
        assert session._providers_ignore == ["X"]
        assert agent.provider_sort == "price"
        assert agent.providers_ignored == ["X"]

    def test_agent_none_still_mutates_session(self):
        session = self._session()
        pc.apply_session_provider_routing(session, None, {"sort": "price"})
        assert session._provider_sort == "price"

    def test_clear_resets_session_and_agent(self):
        session, agent = self._session(), self._agent()
        session._providers_order = ["DeepInfra"]
        agent.providers_order = ["DeepInfra"]
        changes = pc.apply_session_provider_routing(session, agent, {"clear": True})
        assert session._providers_order is None
        assert agent.providers_order is None
        assert session._provider_require_params is False
        assert "cleared" in changes[0]

    def test_empty_parsed_no_changes(self):
        session, agent = self._session(), self._agent()
        assert pc.apply_session_provider_routing(session, agent, {}) == []


class TestSlashRegistry:
    def test_provider_in_command_registry(self):
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("provider")
        assert cmd is not None
        assert cmd.name == "provider"
        assert cmd.cli_only is True
