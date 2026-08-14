"""Core resolves installation preferences from SUPPORTED contracts only.

Two boundaries, both exercised end-to-end with real imports against a temp
``HERMES_HOME`` — not mocks, because mocks are exactly what would hide a
resolution-chain bug here (AGENTS.md: "E2E validation, not just green unit
mocks").

**No private-Workspace coupling.** A private orchestration Workspace keeps its
own state under ``$HERMES_HOME/workspace/``. Hermes core must never read or
parse that private schema — an installation's Workspace layout is not a config
contract, and coupling core to it means a Workspace change silently reroutes
delegation. The guard is behavioural: plant a file that WOULD change delegation
if core parsed it, and assert resolution is unmoved. (A grep-the-source test
would be the banned antipattern, and would also pass against an implementation
that reads the file through a computed path.)

**No installation-specific route inference.** API-mode routing is resolved from
canonical *provider* identity plus that provider's published endpoint table, so
the same model id routes differently under different providers rather than
being guessed from its name prefix.
"""

import json

import pytest
import yaml


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A real, isolated HERMES_HOME with a real config.yaml on disk."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_config(home, delegation):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"delegation": delegation}), encoding="utf-8"
    )


def _resolve_delegation():
    """Resolve through the real chain, with caches cleared like a fresh process."""
    from hermes_cli import config as config_module

    for attr in ("_CONFIG_CACHE", "_config_cache"):
        if hasattr(config_module, attr):
            try:
                setattr(config_module, attr, None)
            except Exception:
                pass
    cache_clear = getattr(getattr(config_module, "load_config", None), "cache_clear", None)
    if cache_clear:
        cache_clear()

    from tools.delegate_tool import _load_config

    return _load_config()


# ── the supported contract is the one that decides ─────────────────────────


def test_delegation_settings_come_from_config_yaml(hermes_home):
    _write_config(hermes_home, {"max_spawn_depth": 2, "max_iterations": 7})
    resolved = _resolve_delegation()
    assert resolved.get("max_spawn_depth") == 2
    assert resolved.get("max_iterations") == 7


def test_a_private_workspace_state_file_cannot_reroute_delegation(hermes_home):
    """Plant private Workspace state that contradicts config.yaml.

    If core ever grows a read of this private schema, the assertion below flips
    — which is the whole point of keeping the guard behavioural.
    """
    _write_config(hermes_home, {"max_spawn_depth": 2, "max_iterations": 7})

    workspace = hermes_home / "workspace"
    workspace.mkdir()
    (workspace / "orchestration-sessions.json").write_text(
        json.dumps(
            {
                "sessions": [{"id": "private-1", "surface": "workspace"}],
                # Values a private Workspace might plausibly carry, all of which
                # would visibly change behaviour if core treated them as config.
                "delegation": {"max_spawn_depth": 3, "max_iterations": 999},
                "defaults": {"nested_delegation": False, "shared_memory": True},
            }
        ),
        encoding="utf-8",
    )

    resolved = _resolve_delegation()
    assert resolved.get("max_spawn_depth") == 2, (
        "core resolved delegation depth from private Workspace state instead of "
        "the supported config.yaml contract"
    )
    assert resolved.get("max_iterations") == 7


def test_conservative_upstream_defaults_survive_an_empty_config(hermes_home):
    """No config.yaml at all → the shipped defaults, not an installation's."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    resolved = _resolve_delegation()
    shipped = DEFAULT_CONFIG["delegation"]
    assert resolved.get("max_iterations") == shipped["max_iterations"]
    assert resolved.get("inherit_mcp_toolsets") == shipped["inherit_mcp_toolsets"]


# ── routing is provider-scoped, never name-prefix-scoped ───────────────────


def test_api_mode_is_resolved_from_provider_identity_not_a_name_prefix():
    """Provider decides whether its published family rules are even consulted."""
    from hermes_cli.models import opencode_model_api_mode

    # Zen's published endpoint table currently groups Claude and Qwen families
    # onto /v1/messages.  Qwen is the discriminator: this cannot pass through a
    # provider-gated ``claude-*`` shortcut.
    assert opencode_model_api_mode("opencode-zen", "claude-x") == "anthropic_messages"
    assert opencode_model_api_mode("opencode-zen", "qwen3-coder") == "anthropic_messages"
    # The identical id under any other provider gets that provider's default —
    # the prefix alone never decides the route.
    assert opencode_model_api_mode("openrouter", "claude-x") == "chat_completions"
    assert opencode_model_api_mode("", "claude-x") == "chat_completions"
    assert opencode_model_api_mode(None, "claude-x") == "chat_completions"
