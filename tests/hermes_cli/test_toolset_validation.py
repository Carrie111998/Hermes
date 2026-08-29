"""Unit tests for hermes_cli.toolset_validation (see #38798).

Pure logic — the validity predicate is injected, so these tests need neither the
tool registry nor a running Hermes.
"""

import pytest

from hermes_cli.toolset_validation import validate_platform_toolsets

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


def test_plugin_registered_toolsets_are_not_flagged_unknown():
    # Plugin toolsets (e.g. eikon, buzz, a2a) register in the tool registry
    # only after plugins load — which is after config-time validation runs —
    # so ``is_valid_toolset`` alone flags them as unknown even though they
    # resolve fine at runtime. Names carried via ``plugin_toolset_names``
    # (the union of ``known_plugin_toolsets``) must validate clean. See #81163.
    cfg = {
        "cli": ["hermes-cli", "eikon", "buzz", "a2a"],
        "discord": ["hermes-discord", "eikon"],
    }
    warnings = validate_platform_toolsets(
        cfg, _is_valid, plugin_toolset_names=["a2a", "buzz", "eikon", "spotify"]
    )
    assert warnings == []


def test_plugin_toolset_names_do_not_mask_real_corruption():
    # A genuinely corrupted name is still flagged even when plugin toolsets
    # are supplied — the plugin set only widens what is considered valid.
    cfg = {"cli": ["hermes-cli", "eikon", "hermes"]}
    warnings = validate_platform_toolsets(
        cfg, _is_valid, plugin_toolset_names=["eikon", "buzz"]
    )
    assert any("unknown toolset 'hermes'" in w for w in warnings)


def test_per_platform_plugin_names_do_not_cross_mask():
    # A plugin known only for one platform must NOT mask an invalid entry
    # on another. Before the per-platform fix, the flat union carried ``a2a``
    # (known only for ``discord``) to every platform, silently accepting the
    # genuinely wrong ``a2a`` entry on ``cli`` — the exact live-config case.
    cfg = {"cli": ["hermes-cli", "a2a"], "discord": ["hermes-discord", "a2a"]}
    known = {"cli": ["eikon", "buzz"], "discord": ["a2a", "eikon"]}
    warnings = validate_platform_toolsets(
        cfg, _is_valid, plugin_toolset_names=known
    )
    # discord's a2a is legitimately known; cli's a2a is not.
    assert not any("platform 'discord'" in w for w in warnings)
    assert any("platform 'cli'" in w and "unknown toolset 'a2a'" in w for w in warnings)


def test_per_platform_mapping_still_accepts_known_plugin():
    # Same mapping shape as production: names valid under their own platform
    # validate clean, matching the pre-existing flat-union behavior.
    cfg = {"cli": ["hermes-cli", "eikon"], "discord": ["hermes-discord", "a2a"]}
    known = {"cli": ["eikon", "buzz"], "discord": ["a2a", "eikon"]}
    warnings = validate_platform_toolsets(
        cfg, _is_valid, plugin_toolset_names=known
    )
    assert warnings == []




