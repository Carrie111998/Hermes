import argparse
import json
from types import SimpleNamespace

import pytest

from hermes_cli import plugins_cmd
from hermes_cli.plugin_activation import PluginActivationState


_GROUP_DENY_CANDIDATES = [
    ("legacy-copy", "1.0", "Bundled", "bundled", None, "shared", "backend"),
    ("new-copy", "2.0", "User", "user", None, "shared", "standalone"),
]
_PORTABLE_SHADOW_CANDIDATES = [
    ("portable-user", "1.0", "Portable", "user", None, "shared", "standalone"),
    (
        "project-shadow",
        "2.0",
        "Project",
        "project",
        None,
        "shared",
        "standalone",
    ),
]
_SHARED_ALLOW_SOURCE_CANDIDATES = [
    ("shared", "1.0", "Target", "user", None, "target", "standalone"),
    ("shared", "1.0", "Sibling", "user", None, "sibling", "standalone"),
    (
        "project-shadow",
        "2.0",
        "Project",
        "project",
        None,
        "sibling",
        "standalone",
    ),
]
_XAI_DEPENDENT_SOURCE_CANDIDATES = [
    ("xai", "1.0", "Images", "user", None, "image_gen/xai", "standalone"),
    ("xai", "1.0", "Video", "user", None, "video_gen/xai", "standalone"),
    (
        "project-shadow",
        "2.0",
        "Project",
        "project",
        None,
        "video_gen/xai",
        "standalone",
    ),
]


def _args(**kwargs):
    defaults = {
        "enabled": False,
        "user": False,
        "no_bundled": False,
        "plain": False,
        "json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_filter_plugin_entries_enabled_only():
    entries = [
        ("disk-cleanup", "2.0.0", "Bundled", "bundled", None, "disk-cleanup", "backend"),
        ("web-search-plus", "2.2.0", "Search", "git", None, "web-search-plus", "standalone"),
        ("old-plugin", "1.0.0", "Old", "user", None, "old-plugin", "standalone"),
    ]

    records = [
        (entries[0], "enabled"),
        (entries[1], "enabled"),
        (entries[2], "disabled"),
    ]
    filtered = plugins_cmd._filter_plugin_entries(records, _args(enabled=True))

    assert [entry[0][0] for entry in filtered] == [
        "disk-cleanup",
        "web-search-plus",
    ]


def test_cmd_list_plain_compact_output(monkeypatch, capsys):
    entries = [
        ("disk-cleanup", "2.0.0", "Bundled", "bundled", None, "disk-cleanup", "backend"),
        ("web-search-plus", "2.2.0", "Search", "git", None, "web-search-plus", "standalone"),
    ]
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_management_records",
        lambda: [(entry, "enabled") for entry in entries],
    )
    plugins_cmd.cmd_list(_args(plain=True, no_bundled=True))

    out = capsys.readouterr().out
    assert "web-search-plus" in out
    assert "enabled" in out
    assert "disk-cleanup" not in out
    assert "Search" not in out  # plain mode stays compact, no descriptions


def test_cmd_list_json_preserves_name_and_adds_canonical_key(monkeypatch, capsys):
    entries = [
        ("xai", "1.0.0", "Images", "bundled", None, "image_gen/xai", "backend"),
    ]
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_management_records",
        lambda: [(entry, "enabled") for entry in entries],
    )
    plugins_cmd.cmd_list(_args(json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "xai"
    assert payload[0]["key"] == "image_gen/xai"


def test_cmd_list_plain_disambiguates_duplicate_manifest_names(monkeypatch, capsys):
    entries = [
        ("xai", "1.0.0", "Images", "bundled", None, "image_gen/xai", "backend"),
        ("xai", "1.0.0", "Video", "bundled", None, "video_gen/xai", "backend"),
    ]
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_management_records",
        lambda: [(entry, "enabled") for entry in entries],
    )
    plugins_cmd.cmd_list(_args(plain=True))

    out = capsys.readouterr().out
    assert "xai [image_gen/xai]" in out
    assert "xai [video_gen/xai]" in out


def test_cmd_list_keeps_inactive_user_override_actionable(
    monkeypatch,
    capsys,
):
    candidates = [
        (
            "bundled-shared",
            "1.0.0",
            "Bundled",
            "bundled",
            None,
            "shared",
            "backend",
        ),
        (
            "user-shared",
            "2.0.0",
            "User",
            "user",
            None,
            "shared",
            "backend",
        ),
    ]
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_plugin_activation_state",
        lambda: PluginActivationState(),
    )
    plugins_cmd.cmd_list(_args(json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [(row["name"], row["source"], row["status"]) for row in payload] == [
        ("user-shared", "user", "not enabled")
    ]


def test_display_records_preserve_group_deny_and_list_uses_it(
    monkeypatch,
    capsys,
):
    candidates = _GROUP_DENY_CANDIDATES
    activation = PluginActivationState(
        enabled=frozenset({"new-copy"}),
        disabled=frozenset({"legacy-copy"}),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_plugin_activation_state",
        lambda: activation,
    )

    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == []
    assert plugins_cmd._discover_plugin_display_records() == [
        (candidates[1], "disabled")
    ]
    assert plugins_cmd._discover_plugin_display_entries() == [candidates[1]]
    assert plugins_cmd._discover_plugin_management_records() == [
        (candidates[1], "disabled")
    ]

    plugins_cmd.cmd_list(_args(json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "new-copy"
    assert payload[0]["status"] == "disabled"

    plugins_cmd.cmd_list(_args(plain=True))
    assert "disabled" in capsys.readouterr().out

    plugins_cmd.cmd_list(_args())
    assert "disabled" in capsys.readouterr().out

    plugins_cmd.cmd_list(_args(json=True, enabled=True))
    assert json.loads(capsys.readouterr().out) == []


def test_cmd_toggle_keeps_inactive_project_override_actionable(
    monkeypatch,
):
    candidates = _PORTABLE_SHADOW_CANDIDATES
    activation = PluginActivationState(
        enabled=frozenset({"portable-user"}),
    )
    captured = {}

    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_plugin_activation_state",
        lambda: activation,
    )
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", set)
    monkeypatch.setattr(plugins_cmd, "_get_current_memory_provider", lambda: "")
    monkeypatch.setattr(
        plugins_cmd,
        "_get_current_context_engine",
        lambda: "compressor",
    )
    monkeypatch.setattr(plugins_cmd.sys.stdin, "isatty", lambda: True)

    def _capture(_curses, keys, labels, selected, *_args):
        captured.update(keys=keys, labels=labels, selected=selected)

    monkeypatch.setattr(plugins_cmd, "_run_composite_ui", _capture)
    monkeypatch.setattr(
        plugins_cmd,
        "_run_composite_fallback",
        lambda keys, labels, selected, *_args: captured.update(
            keys=keys,
            labels=labels,
            selected=selected,
        ),
    )

    plugins_cmd.cmd_toggle()

    assert captured["keys"] == ["shared"]
    assert captured["selected"] == set()
    assert "project-shadow" in captured["labels"][0]
    assert "portable-user" not in captured["labels"][0]


def test_composite_toggle_enable_clears_same_key_legacy_deny(monkeypatch):
    candidates = _GROUP_DENY_CANDIDATES
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", set)
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )
    changed = plugins_cmd._persist_composite_plugin_selection(
        ["shared"],
        set(),
        {0},
        {"legacy-copy"},
    )

    assert changed is True
    assert saved == {"enabled": {"shared"}, "disabled": set()}
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[1]
    ]


def test_composite_confirm_preserves_user_alias_and_active_winner(monkeypatch):
    candidates = _PORTABLE_SHADOW_CANDIDATES
    saves = []
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_get_enabled_set",
        lambda: {"portable-user"},
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saves.append(("enabled", set(value))),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saves.append(("disabled", set(value))),
    )
    changed = plugins_cmd._persist_composite_plugin_selection(
        ["shared"],
        {0},
        {0},
        set(),
    )

    assert changed is False
    assert saves == []
    activation = PluginActivationState(
        enabled=frozenset({"portable-user"}),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[0]
    ]


@pytest.mark.parametrize("surface", ["cli", "dashboard", "delta"])
def test_disable_shared_allow_preserves_sibling_source_winner(
    monkeypatch,
    surface,
):
    candidates = _SHARED_ALLOW_SOURCE_CANDIDATES
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"shared"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", set)
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )

    if surface == "cli":
        monkeypatch.setattr(
            plugins_cmd,
            "_resolve_plugin_key_and_source",
            lambda _name: ("target", "user", "shared", "standalone"),
        )
        plugins_cmd.cmd_disable("target")
    elif surface == "dashboard":
        monkeypatch.setattr(
            plugins_cmd,
            "_resolve_plugin_key_and_source",
            lambda _name: ("target", "user", "shared", "standalone"),
        )
        monkeypatch.setattr(
            plugins_cmd,
            "_toggle_plugin_toolset",
            lambda *args, **kwargs: None,
        )
        result = plugins_cmd.dashboard_set_agent_plugin_enabled(
            "target",
            enabled=False,
        )
        assert result["unchanged"] is False
    else:
        changed = plugins_cmd._persist_composite_plugin_selection(
            ["target"],
            {0},
            set(),
            set(),
        )
        assert changed is True

    assert saved == {"enabled": {"shared"}, "disabled": {"target"}}
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[1]
    ]


def test_composite_enable_keeps_shared_allow_and_sibling_source_winner(monkeypatch):
    candidates = _SHARED_ALLOW_SOURCE_CANDIDATES
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"shared"})
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )

    changed = plugins_cmd._persist_composite_plugin_selection(
        ["target"],
        set(),
        {0},
        {"target"},
    )

    assert changed is True
    assert saved == {
        "enabled": {"shared", "target"},
        "disabled": set(),
    }
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[0],
        candidates[1],
    ]


@pytest.mark.parametrize("surface", ["cli", "dashboard"])
def test_sequential_disable_clears_last_shared_allow(
    monkeypatch,
    surface,
):
    candidates = _XAI_DEPENDENT_SOURCE_CANDIDATES
    state = {"enabled": {"xai"}, "disabled": set()}
    resolutions = {
        "image_gen/xai": (
            "image_gen/xai",
            "user",
            "xai",
            "standalone",
        ),
        "video_gen/xai": (
            "video_gen/xai",
            "user",
            "xai",
            "standalone",
        ),
    }
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        resolutions.__getitem__,
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_get_enabled_set",
        lambda: set(state["enabled"]),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_get_disabled_set",
        lambda: set(state["disabled"]),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: state.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: state.update(disabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_toggle_plugin_toolset",
        lambda *args, **kwargs: None,
    )

    def _disable(key):
        if surface == "cli":
            plugins_cmd.cmd_disable(key)
        else:
            result = plugins_cmd.dashboard_set_agent_plugin_enabled(
                key,
                enabled=False,
            )
            assert result["unchanged"] is False

    _disable("image_gen/xai")
    assert state == {
        "enabled": {"xai"},
        "disabled": {"image_gen/xai"},
    }
    first_activation = PluginActivationState(
        enabled=frozenset(state["enabled"]),
        disabled=frozenset(state["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(
        candidates,
        first_activation,
    ) == [candidates[1]]

    _disable("video_gen/xai")
    assert state == {
        "enabled": set(),
        "disabled": {"image_gen/xai", "video_gen/xai"},
    }
    final_activation = PluginActivationState(
        enabled=frozenset(state["enabled"]),
        disabled=frozenset(state["disabled"]),
    )
    assert final_activation.status(
        name="xai",
        key="xai",
        source="user",
        kind="standalone",
    ) == "not enabled"


def test_composite_disable_two_groups_clears_last_shared_allow(monkeypatch):
    candidates = _XAI_DEPENDENT_SOURCE_CANDIDATES
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"xai"})
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )

    changed = plugins_cmd._persist_composite_plugin_selection(
        ["image_gen/xai", "video_gen/xai"],
        {0, 1},
        set(),
        set(),
    )

    assert changed is True
    assert saved == {
        "enabled": set(),
        "disabled": {"image_gen/xai", "video_gen/xai"},
    }
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert activation.status(
        name="xai",
        key="xai",
        source="user",
        kind="standalone",
    ) == "not enabled"


def test_dashboard_toggle_response_keeps_input_name_and_adds_key(monkeypatch):
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        lambda _name: ("image_gen/xai", "user", "xai", "standalone"),
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_save_enabled_set", lambda _value: None)
    monkeypatch.setattr(plugins_cmd, "_save_disabled_set", lambda _value: None)
    monkeypatch.setattr(plugins_cmd, "_toggle_plugin_toolset", lambda *args, **kwargs: None)

    result = plugins_cmd.dashboard_set_agent_plugin_enabled("xai", enabled=True)

    assert result["name"] == "xai"
    assert result["key"] == "image_gen/xai"


def test_dashboard_enable_clears_every_same_key_candidate_deny(monkeypatch):
    candidates = [
        ("bundled-shared", "1", "", "bundled", None, "shared", "backend"),
        ("user-shared", "2", "", "user", None, "shared", "backend"),
    ]
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        lambda _name: ("shared", "user", "user-shared", "backend"),
    )
    monkeypatch.setattr(
        plugins_cmd, "_discover_plugin_candidates", lambda: candidates
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", set)
    monkeypatch.setattr(
        plugins_cmd, "_get_disabled_set", lambda: {"bundled-shared"}
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd, "_toggle_plugin_toolset", lambda *args, **kwargs: None
    )

    result = plugins_cmd.dashboard_set_agent_plugin_enabled("shared", enabled=True)

    assert result["unchanged"] is False
    assert saved == {"enabled": {"shared"}, "disabled": set()}
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[1]
    ]


def test_dashboard_enable_preserves_other_shared_manifest_key_deny(monkeypatch):
    candidates = [
        ("xai", "1", "Images", "bundled", None, "image_gen/xai", "backend"),
        ("xai", "1", "Video", "bundled", None, "video_gen/xai", "backend"),
    ]
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        lambda _name: ("image_gen/xai", "bundled", "xai", "backend"),
    )
    monkeypatch.setattr(
        plugins_cmd, "_discover_plugin_candidates", lambda: candidates
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", set)
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: {"xai"})
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd, "_toggle_plugin_toolset", lambda *args, **kwargs: None
    )

    result = plugins_cmd.dashboard_set_agent_plugin_enabled(
        "image_gen/xai",
        enabled=True,
    )

    assert result["unchanged"] is False
    assert saved == {
        "enabled": {"image_gen/xai"},
        "disabled": {"video_gen/xai"},
    }
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[0]
    ]


def test_dashboard_disable_drops_redundant_bundled_allow(monkeypatch):
    candidates = [
        ("xai", "1", "Images", "bundled", None, "image_gen/xai", "backend"),
        ("xai", "1", "Video", "bundled", None, "video_gen/xai", "backend"),
    ]
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        lambda _name: ("image_gen/xai", "bundled", "xai", "backend"),
    )
    monkeypatch.setattr(
        plugins_cmd, "_discover_plugin_candidates", lambda: candidates
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"xai"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", set)
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd, "_toggle_plugin_toolset", lambda *args, **kwargs: None
    )

    result = plugins_cmd.dashboard_set_agent_plugin_enabled(
        "image_gen/xai",
        enabled=False,
    )

    assert result["unchanged"] is False
    assert saved == {
        "enabled": set(),
        "disabled": {"image_gen/xai"},
    }
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == [
        candidates[1]
    ]


def test_dashboard_disable_user_override_preserves_shared_deny(monkeypatch):
    candidates = [
        ("shared", "1", "Target", "bundled", None, "target/key", "backend"),
        ("shared", "1", "Sibling", "bundled", None, "sibling/key", "backend"),
        ("target-user", "2", "Target", "user", None, "target/key", "backend"),
    ]
    saved = {}
    monkeypatch.setattr(
        plugins_cmd,
        "_resolve_plugin_key_and_source",
        lambda _name: ("target/key", "user", "target-user", "backend"),
    )
    monkeypatch.setattr(
        plugins_cmd, "_discover_plugin_candidates", lambda: candidates
    )
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"target/key"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: {"shared"})
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda value: saved.update(enabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_save_disabled_set",
        lambda value: saved.update(disabled=set(value)),
    )
    monkeypatch.setattr(
        plugins_cmd, "_toggle_plugin_toolset", lambda *args, **kwargs: None
    )

    result = plugins_cmd.dashboard_set_agent_plugin_enabled(
        "target/key",
        enabled=False,
    )

    assert result["unchanged"] is False
    assert saved == {
        "enabled": set(),
        "disabled": {"shared", "target/key"},
    }
    activation = PluginActivationState(
        enabled=frozenset(saved["enabled"]),
        disabled=frozenset(saved["disabled"]),
    )
    assert plugins_cmd._select_active_plugin_entries(candidates, activation) == []


def test_runtime_identities_do_not_treat_key_leaf_as_identity(monkeypatch):
    candidates = [
        (
            "xai-provider",
            "1",
            "Models",
            "bundled",
            None,
            "model-providers/xai",
            "model-provider",
        ),
        ("xai", "1", "Images", "bundled", None, "image_gen/xai", "backend"),
        ("xai", "1", "Video", "bundled", None, "video_gen/xai", "backend"),
    ]
    monkeypatch.setattr(
        plugins_cmd, "_discover_plugin_candidates", lambda: candidates
    )

    identities, preserved = plugins_cmd._plugin_runtime_identity_changes(
        "model-providers/xai",
        {"xai"},
    )

    assert identities == {"model-providers/xai", "xai-provider"}
    assert preserved == set()


def test_discover_all_plugins_includes_entrypoint_plugins(monkeypatch, tmp_path):
    bundled_dir = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled_dir.mkdir()
    user_dir.mkdir()

    dist = SimpleNamespace(
        version="0.1.0",
        metadata={"Summary": "Karpathy-style LLM Wikis for Hermes"},
    )
    entry_point = SimpleNamespace(
        name="wiki",
        value="adapters.hermes.cli_plugin",
        group="hermes_agent.plugins",
        dist=dist,
    )

    monkeypatch.setattr(plugins_cmd, "_plugins_dir", lambda: user_dir)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_bundled_plugins_dir",
        lambda: bundled_dir,
    )
    monkeypatch.setattr(
        plugins_cmd.importlib.metadata,
        "entry_points",
        lambda: [entry_point],
    )

    entries = plugins_cmd._discover_all_plugins()

    assert entries == [
        (
            "wiki",
            "0.1.0",
            "Karpathy-style LLM Wikis for Hermes",
            "entrypoint",
            "adapters.hermes.cli_plugin",
            "wiki",
            "standalone",
        )
    ]


