"""Regression tests: ``hermes chat -q -m <alias>`` must resolve direct
aliases from config.yaml ``model_aliases:``.

Bug: ``cmd_chat`` → ``cli.main()`` never ran alias resolution, so the raw
alias string (e.g. ``local8b``) was sent to the default provider as the
model id — HTTP 400, then a *silent* fallback to the first configured
fallback provider. The answer looked fine but came from a different model.

The oneshot path (``hermes_cli/oneshot.py``, DIRECT_ALIASES check) and
interactive ``/model`` (``model_switch.switch_model``) already resolved
aliases; this file pins the same behaviour for ``HermesCLI.__init__`` with
an explicit ``-m`` value.
"""

import sys
from unittest.mock import MagicMock, patch

from hermes_cli.model_switch import DirectAlias


def _make_cli(**kwargs):
    """Create a HermesCLI instance with minimal mocking (mirrors
    tests/cli/test_cli_init.py::_make_cli, without env overrides)."""
    import importlib

    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    try:
        with patch.dict(sys.modules, prompt_toolkit_stubs), \
             patch.dict("os.environ", clean_env, clear=False):
            import cli as _cli_mod
            _cli_mod = importlib.reload(_cli_mod)
            with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), \
                 patch.dict(_cli_mod.__dict__, {"CLI_CONFIG": _clean_config}):
                return _cli_mod.HermesCLI(**kwargs)
    finally:
        import cli as _cli_restore
        importlib.reload(_cli_restore)


_LOCAL_ALIASES = {
    "local8b": DirectAlias(
        model="qwen3-8b", provider="custom", base_url="http://localhost:8080/v1"
    ),
    "fav": DirectAlias(model="claude-sonnet-4.6", provider="anthropic", base_url=""),
}


def _patched_aliases():
    """Patch DIRECT_ALIASES for the duration of a HermesCLI construction."""
    import hermes_cli.model_switch as ms
    return patch.object(ms, "DIRECT_ALIASES", _LOCAL_ALIASES)


class TestExplicitModelAliasResolution:
    """``-m <direct-alias>`` routes to the alias's model + provider/endpoint."""

    def test_custom_alias_resolves_model_and_pins_base_url(self):
        with _patched_aliases():
            cli = _make_cli(model="local8b")
        # The alias's model id is sent, not the raw alias string.
        assert cli.model == "qwen3-8b"
        # Bare-custom alias: provider routes to custom and the alias's
        # base_url is pinned via _explicit_base_url so
        # _ensure_runtime_credentials resolves the direct-alias runtime.
        assert cli.requested_provider == "custom"
        assert cli._explicit_base_url == "http://localhost:8080/v1"
        # An explicit -m is not "the default model".
        assert cli._model_is_default is False

    def test_named_provider_alias_routes_provider(self):
        with _patched_aliases():
            cli = _make_cli(model="fav")
        assert cli.model == "claude-sonnet-4.6"
        assert cli.requested_provider == "anthropic"
        # No base_url pin — the provider's own resolution applies.
        assert not cli._explicit_base_url

    def test_explicit_provider_arg_wins_over_alias(self):
        with _patched_aliases():
            cli = _make_cli(model="local8b", provider="anthropic")
        # Model still resolves through the alias, but the explicit
        # --provider arg wins for routing: no custom/base_url override.
        assert cli.model == "qwen3-8b"
        assert cli.requested_provider == "anthropic"
        assert not cli._explicit_base_url

    def test_explicit_base_url_arg_wins_over_alias(self):
        with _patched_aliases():
            cli = _make_cli(model="local8b", base_url="https://my-gateway.example/v1")
        assert cli.model == "qwen3-8b"
        assert cli._explicit_base_url == "https://my-gateway.example/v1"
        # No provider rerouting to custom — the explicit endpoint stands.
        assert cli.requested_provider != "custom"

    def test_non_alias_model_untouched(self):
        with _patched_aliases():
            cli = _make_cli(model="glm-5.2")
        assert cli.model == "glm-5.2"
        # No alias resolution for a plain model id: routing keeps the
        # config default and no endpoint pin appears.
        assert cli.requested_provider == "auto"
        assert not cli._explicit_base_url

    def test_no_model_arg_untouched(self):
        # No -m: config default flows through unchanged (aliases must not
        # apply — the config default was resolved when it was written).
        with _patched_aliases():
            cli = _make_cli()
        assert cli.model == "anthropic/claude-opus-4.6"
        assert cli.requested_provider == "auto"
        assert not cli._explicit_base_url
