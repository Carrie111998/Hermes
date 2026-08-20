"""Known plugin toolsets must not spam the pre-discovery warning.

Regression: ``hermes`` (bare) validated toolsets before discover_plugins()
ran, so a valid plugin toolset tracked in ``known_plugin_toolsets`` (e.g.
google_health) was reported as "Unknown toolsets" on every startup.
"""

import importlib
import sys
import types

import pytest


def _import_cli():
    for name in list(sys.modules):
        if name == "cli" or name == "run_agent" or name.startswith("tools"):
            sys.modules.pop(name, None)
    if "firecrawl" not in sys.modules:
        sys.modules["firecrawl"] = types.SimpleNamespace(Firecrawl=object)
    return importlib.import_module("cli")


@pytest.fixture
def pre_discovery_cli(monkeypatch):
    """A cli module where plugin toolsets are NOT yet registered."""
    cli = _import_cli()
    # Simulate validation running before discover_plugins(): the plugin
    # toolset isn't in the registry, so validate_toolset must return False.
    monkeypatch.setattr(cli, "validate_toolset", lambda name: False)
    # Config tracks the plugin toolset as known per-platform (written by
    # `hermes tools`), exactly like a real install.
    monkeypatch.setitem(
        cli.CLI_CONFIG, "known_plugin_toolsets", {"cli": ["google_health"]}
    )
    return cli


def test_known_plugin_toolset_no_warning(pre_discovery_cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        pre_discovery_cli.HermesCLI,
        "_console_print",
        lambda self, *a, **k: calls.append(a),
    )
    pre_discovery_cli.HermesCLI(toolsets=["google_health"], compact=True, max_turns=1)
    assert not any(str(c).find("Unknown toolsets") >= 0 for c in calls)


def test_truly_unknown_toolset_still_warns(pre_discovery_cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        pre_discovery_cli.HermesCLI,
        "_console_print",
        lambda self, *a, **k: calls.append(a),
    )
    pre_discovery_cli.HermesCLI(
        toolsets=["this_toolset_does_not_exist"], compact=True, max_turns=1
    )
    warned = [str(c) for c in calls if "Unknown toolsets" in str(c)]
    assert warned and "this_toolset_does_not_exist" in warned[0]
