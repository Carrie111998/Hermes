"""Regression tests for the fork-local patches (DoGMaTiiC/hermes-agent).

Local patches are the silent-death risk on `hermes update`: upstream refactors
the code around them, git merges cleanly (no conflict), and the behavior
disappears without a trace. These tests pin the two load-bearing patches so a
clean merge that eats one fails loudly.

Patches under test:
  6fc5f190f  global context file (HERMES_HOME/.hermes.md) ADDS to project
             context instead of replacing it  -> TestGlobalContextAdds
  6122d85ec  model.temperature / model.top_p from config.yaml reach the API
             via request_overrides            -> TestSamplingOverrides/Wire

The hermetic test environment (tests/conftest.py, autouse) already redirects
HERMES_HOME to a per-test tempdir — no extra env patching needed here.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.auxiliary_client import OMIT_TEMPERATURE, _fixed_temperature_for_model
from agent.prompt_builder import build_context_files_prompt
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_constants import get_hermes_home

GLOBAL_MARKER = "GLOBAL-OPERATING-POLICY-MARKER"
PROJECT_MARKER = "PROJECT-AGENTS-MARKER"


def _hermes_home() -> Path:
    return Path(get_hermes_home())


class TestGlobalContextAdds:
    """Commit 6fc5f190f: HERMES_HOME/.hermes.md is ADDED TO the project context,
    not a fallback for it. The global must be present even when the cwd has its
    own AGENTS.md, and must not be injected twice when cwd IS HERMES_HOME."""

    def test_global_adds_to_project_agents_md(self, tmp_path):
        (_hermes_home() / ".hermes.md").write_text(
            f"{GLOBAL_MARKER}: regras globais do Hermes.\n", encoding="utf-8"
        )
        project = tmp_path / "project-with-agents"
        project.mkdir()
        (project / "AGENTS.md").write_text(
            f"{PROJECT_MARKER}: regras do projeto.\n", encoding="utf-8"
        )

        out = build_context_files_prompt(
            cwd=str(project), skip_soul=True, allow_install_tree_fallback=True
        )

        assert PROJECT_MARKER in out
        assert GLOBAL_MARKER in out, (
            "global HERMES_HOME/.hermes.md vanished from the prompt: the global "
            "context regressed to fallback-only (only loaded when the cwd has "
            "no context file of its own)"
        )

    def test_global_not_duplicated_when_cwd_is_hermes_home(self):
        (_hermes_home() / ".hermes.md").write_text(
            f"{GLOBAL_MARKER}: regras globais do Hermes.\n", encoding="utf-8"
        )

        out = build_context_files_prompt(
            cwd=str(_hermes_home()), skip_soul=True, allow_install_tree_fallback=True
        )

        assert GLOBAL_MARKER in out
        assert out.count(GLOBAL_MARKER) == 1, (
            "global context injected twice when cwd is HERMES_HOME: the dedupe "
            "guard (_find_hermes_md resolves the same file as the global) is gone"
        )


def _sampling_config() -> dict:
    return {"model": {"temperature": 1.0, "top_p": 0.95}}


def _patch_init_noise(monkeypatch):
    """init_agent touches provider/client machinery that must not run in a unit
    test — same scaffolding as
    tests/run_agent/test_63425_credential_pool_auto_detect.py.

    NOTE: cfg_get and load_config_readonly are intentionally NOT patched here;
    the sampling block under test reads them at call time."""
    import agent.anthropic_adapter as anthropic_adapter
    import agent.auxiliary_client as auxiliary_client
    import agent.azure_identity_adapter as azure_identity_adapter
    import agent.credential_pool as credential_pool
    import agent.iteration_budget as iteration_budget
    import hermes_cli.config as cfg_mod
    import hermes_cli.model_normalize as model_normalize
    import run_agent

    monkeypatch.setattr(
        auxiliary_client, "resolve_provider_client", lambda *a, **k: (None, None)
    )
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr(
        anthropic_adapter, "build_anthropic_client", lambda *a, **k: MagicMock()
    )
    monkeypatch.setattr(
        anthropic_adapter, "resolve_anthropic_token", lambda *a, **k: ""
    )
    monkeypatch.setattr(anthropic_adapter, "_is_oauth_token", lambda *a, **k: False)
    monkeypatch.setattr(
        azure_identity_adapter, "is_token_provider", lambda *a, **k: False
    )
    monkeypatch.setattr(
        model_normalize,
        "normalize_model_for_provider",
        lambda model, provider=None: (
            model
        ),  # identity: keep the model name (agent_init.py:687)
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(cfg_mod, "get_compatible_custom_providers", lambda *a, **k: [])
    monkeypatch.setattr(
        iteration_budget,
        "IterationBudget",
        lambda *a, **k: SimpleNamespace(max_iterations=1),
    )


def _make_agent():
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""
    return agent


def _init_agent_with_sampling(monkeypatch, *, model, request_overrides=None):
    import hermes_cli.config as cfg_mod

    from agent.agent_init import init_agent

    monkeypatch.setattr(cfg_mod, "load_config_readonly", _sampling_config)
    _patch_init_noise(monkeypatch)
    agent = _make_agent()
    init_agent(
        agent,
        model=model,
        # Explicit creds keep init_agent's provider guard (agent_init.py:1311)
        # from raising; the router itself is stubbed in _patch_init_noise.
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        request_overrides=request_overrides,
        skip_context_files=True,
        skip_memory=True,
        quiet_mode=True,
    )
    return agent


class TestSamplingOverrides:
    """Commit 6122d85ec: model.temperature / model.top_p from config.yaml land
    in agent.request_overrides (setdefault, so an explicit turn override wins),
    scoped to deepseek-v4-flash — the model those values were tuned for."""

    def test_temperature_and_top_p_enter_request_overrides(self, monkeypatch):
        agent = _init_agent_with_sampling(monkeypatch, model="deepseek-v4-flash")

        assert agent.request_overrides.get("temperature") == 1.0
        assert agent.request_overrides.get("top_p") == 0.95

    def test_explicit_turn_override_wins_over_config(self, monkeypatch):
        agent = _init_agent_with_sampling(
            monkeypatch,
            model="deepseek-v4-flash",
            request_overrides={"temperature": 0.7},
        )

        assert agent.request_overrides["temperature"] == 0.7  # setdefault: turn wins
        assert agent.request_overrides["top_p"] == 0.95

    def test_provider_prefixed_model_still_matches(self, monkeypatch):
        """Routers hand the model through as ``<provider>/<model>``; the gate
        compares the bare name so the knob survives that shape."""
        agent = _init_agent_with_sampling(
            monkeypatch, model="opencode-go/deepseek-v4-flash"
        )

        assert agent.request_overrides.get("temperature") == 1.0
        assert agent.request_overrides.get("top_p") == 0.95

    def test_codex_model_gets_no_sampling_params(self, monkeypatch):
        """The regression this scoping exists for: the Codex Responses adapter
        validates against an allowlist holding ``temperature`` but not
        ``top_p`` (codex_responses_adapter.py). An unscoped top_p raised
        ValueError on every gpt-5.6-luna request, killing the fallback
        provider precisely when the primary one was down."""
        agent = _init_agent_with_sampling(monkeypatch, model="gpt-5.6-luna")

        assert "top_p" not in agent.request_overrides
        assert "temperature" not in agent.request_overrides

    def test_kimi_gets_no_sampling_params(self, monkeypatch):
        """Kimi is out of scope like every other non-deepseek model, so the
        server keeps owning temperature without needing the OMIT_TEMPERATURE
        special case the unscoped version required."""
        agent = _init_agent_with_sampling(monkeypatch, model="kimi-k2.7-code")

        assert "temperature" not in agent.request_overrides
        assert "top_p" not in agent.request_overrides


class TestSamplingWire:
    """The other half of commit 6122d85ec: request_overrides lands in the API
    kwargs, and the Kimi OMIT_TEMPERATURE contract holds end to end."""

    def test_kimi_contract_is_omit_temperature(self):
        assert _fixed_temperature_for_model("kimi-k2.7-code") is OMIT_TEMPERATURE
        assert _fixed_temperature_for_model("deepseek-v4-flash") is None

    def test_request_overrides_reach_api_kwargs(self):
        kw = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            request_overrides={"temperature": 1.0, "top_p": 0.95},
        )

        assert kw["temperature"] == 1.0
        assert kw["top_p"] == 0.95

    def test_kimi_overrides_keep_top_p_without_temperature(self):
        kw = ChatCompletionsTransport().build_kwargs(
            model="kimi-k2.7-code",
            messages=[{"role": "user", "content": "hi"}],
            request_overrides={
                "top_p": 0.95
            },  # an explicit per-turn override, not what init_agent emits
        )

        assert kw.get("top_p") == 0.95
        assert "temperature" not in kw

    def test_codex_responses_still_rejects_top_p(self):
        """Why TestSamplingOverrides scopes the knob to deepseek-v4-flash. If
        this ever fails, upstream started accepting top_p on the Responses API
        and the scoping could widen."""
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs

        base = {
            "model": "gpt-5.6-luna",
            "instructions": "hi",
            "input": [{"role": "user", "content": "hi"}],
            "store": False,
        }

        _preflight_codex_api_kwargs({**base, "temperature": 1.0})  # allowlisted

        with pytest.raises(ValueError, match="unsupported field"):
            _preflight_codex_api_kwargs({**base, "top_p": 0.95})
