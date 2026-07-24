"""Shared declarative configuration coverage for every Gateway platform."""

from __future__ import annotations

from gateway.config import Platform
from gateway.platform_configuration import (
    BUILTIN_PLATFORM_SPECS,
    StaticConfigurationState,
    evaluate_static_configuration,
)


def test_all_builtins_have_shared_pure_configuration_semantics():
    """Every shipped platform must be classified without adapter imports."""
    expected = {
        member.value
        for member in Platform.__members__.values()
        if member is not Platform.LOCAL
    }
    expected.update(Platform._scan_bundled_plugin_platforms())

    assert expected <= BUILTIN_PLATFORM_SPECS.keys()


def test_every_builtin_spec_handles_minimal_config():
    for name, spec in BUILTIN_PLATFORM_SPECS.items():
        state = evaluate_static_configuration(
            {"enabled": True},
            spec,
            getenv={}.get,
        )
        assert isinstance(state, StaticConfigurationState), name


def test_api_server_shared_key_validity():
    spec = BUILTIN_PLATFORM_SPECS["api_server"]

    for invalid in (None, "", "changeme", "shortkey"):
        block = {"enabled": True, "extra": {"key": invalid}}
        assert (
            evaluate_static_configuration(block, spec, getenv={}.get)
            is StaticConfigurationState.DISABLED
        )

    assert (
        evaluate_static_configuration(
            {"enabled": True, "extra": {"key": "opensslrandhex32strongkey"}},
            spec,
            getenv={}.get,
        )
        is StaticConfigurationState.CONFIGURED
    )
