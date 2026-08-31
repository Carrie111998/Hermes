"""Unit tests for hermes_cli.toolset_validation (see #38798).

Pure logic — the validity predicate is injected, so these tests need neither the
tool registry nor a running Hermes.
"""

import pytest

from hermes_cli.toolset_validation import (
    effective_toolset_validator,
    validate_disabled_toolset_declarations,
    validate_platform_toolsets,
)

# A representative set of real toolset names. `hermes` is deliberately absent —
# that is the corruption #38798 reported (`hermes-cli` rewritten to `hermes`).
_KNOWN = {
    "hermes-cli",
    "hermes-telegram",
    "hermes-discord",
    "terminal",
    "web",
}


def _is_valid(name):
    return name in _KNOWN




def test_38798_corruption_warns_and_suggests_correct_name():
    # The exact reported shape: cli holds 'hermes' instead of 'hermes-cli'.
    warnings = validate_platform_toolsets({"cli": ["hermes"]}, _is_valid)
    unknown = [w for w in warnings if "unknown toolset 'hermes'" in w]
    assert len(unknown) == 1
    # Actionable: points at the valid name the entry should have been.
    assert "did you mean 'hermes-cli'?" in unknown[0]
    # And the zero-valid-toolsets safety net fires.
    assert any("zero valid toolsets" in w for w in warnings)


def test_mixed_valid_and_invalid_flags_only_the_invalid():
    cfg = {"cli": ["hermes-cli"], "discord": ["bogus"]}
    warnings = validate_platform_toolsets(cfg, _is_valid)
    # One valid entry exists, so no zero-valid warning.
    assert not any("zero valid toolsets" in w for w in warnings)
    assert len(warnings) == 1
    assert "platform 'discord'" in warnings[0]
    assert "unknown toolset 'bogus'" in warnings[0]


def test_disabled_toolset_validation_reports_every_inert_declaration():
    warnings = validate_disabled_toolset_declarations(
        {
            "disabled_toolsets": ["terminal"],
            "agent": {"disabled_toolsets": ["terminal", "execute_code"]},
        },
        _is_valid,
        environ={"HERMES_DISABLED_TOOLSETS": "code_execution"},
    )

    assert len(warnings) == 3
    assert any("HERMES_DISABLED_TOOLSETS" in warning for warning in warnings)
    assert any("root-level 'disabled_toolsets'" in warning for warning in warnings)
    assert any("unknown toolset 'execute_code'" in warning for warning in warnings)
    assert all("agent.disabled_toolsets" in warning for warning in warnings)


def test_disabled_toolset_validation_accepts_effective_legacy_names():
    warnings = validate_disabled_toolset_declarations(
        {"agent": {"disabled_toolsets": ["terminal_tools"]}},
        _is_valid,
        legacy_names={"terminal_tools"},
    )

    assert warnings == []


def test_disabled_toolset_validation_parses_list_shaped_strings():
    warnings = validate_disabled_toolset_declarations(
        {"agent": {"disabled_toolsets": "['terminal', 'execute_code']"}},
        _is_valid,
    )

    assert len(warnings) == 1
    assert "unknown toolset 'execute_code'" in warnings[0]


def test_dynamic_extension_toolsets_are_valid_before_registry_discovery(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_toolset_keys_nowait",
        lambda: {"plugin_bundle"},
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.get_portable_mcp_server_names_nowait",
        lambda: {"portable_server"},
    )
    config = {
        "mcp_servers": {"native_server": {"enabled": True}},
        "agent": {
            "disabled_toolsets": [
                "plugin_bundle",
                "portable_server",
                "native_server",
                "bogus",
            ]
        },
    }

    is_valid = effective_toolset_validator(config, _is_valid)
    warnings = validate_disabled_toolset_declarations(config, is_valid)

    assert len(warnings) == 1
    assert "unknown toolset 'bogus'" in warnings[0]




