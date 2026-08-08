from __future__ import annotations

import json

import pytest

from hermes_cli.dashboard_plugin_pages import (
    filter_active_dashboard_plugins,
    list_dashboard_plugin_pages,
    safe_dashboard_plugin_tab_path,
    strict_dashboard_plugin_activation_sets,
)
from hermes_cli.dashboard_pages import build_dashboard_link


@pytest.mark.parametrize(
    "path",
    [
        "kanban",
        "//evil.test",
        "/../admin",
        "/%2e%2e/admin",
        "/foo?token=secret",
        "/foo#fragment",
        "/foo\\bar",
        "/foo bar",
        "/api/config",
        "/assets/plugin.js",
        "/ws/events",
        "/openapi.json",
        "/redoc",
        "/docs",
        "/models",
        "/docs) [x](https://evil.test",
    ],
)
def test_unsafe_plugin_tab_paths_are_rejected(path):
    assert safe_dashboard_plugin_tab_path(path) is None


@pytest.mark.parametrize("path", ["/kanban", "/achievements", "/tools/board-v2"])
def test_safe_plugin_tab_paths_are_accepted(path):
    assert safe_dashboard_plugin_tab_path(path) == path


def test_explicit_plugin_overrides_may_target_builtin_routes():
    assert safe_dashboard_plugin_tab_path("/models", allow_builtin=True) == "/models"
    assert safe_dashboard_plugin_tab_path("/", allow_builtin=True) == "/"
    assert safe_dashboard_plugin_tab_path("/Models", allow_builtin=True) == "/models"
    assert safe_dashboard_plugin_tab_path("/custom", allow_builtin=True) is None


@pytest.mark.parametrize(
    "config",
    [
        {"plugins": "invalid"},
        {"dashboard": "invalid"},
        {"plugins": {"enabled": "plugin-x"}},
        {"plugins": {"disabled": "plugin-x"}},
        {"dashboard": {"hidden_plugins": "plugin-x"}},
        {"plugins": {"disabled": [None]}},
    ],
)
def test_strict_asset_activation_policy_rejects_malformed_config(config):
    with pytest.raises(ValueError):
        strict_dashboard_plugin_activation_sets(config)


def test_strict_asset_activation_policy_preserves_missing_list_defaults():
    assert strict_dashboard_plugin_activation_sets({}) == (set(), set(), set())


def test_active_plugin_filter_applies_global_denylist_and_deduplicates_routes():
    plugins = [
        {"name": "project-board", "source": "project", "tab": {"path": "/board"}},
        {"name": "bundled-board", "source": "bundled", "tab": {"path": "/board"}},
        {"name": "disabled", "source": "bundled", "tab": {"path": "/disabled"}},
    ]

    active = filter_active_dashboard_plugins(
        plugins,
        hidden=set(),
        enabled=set(),
        disabled={"project-board", "disabled"},
    )

    assert [plugin["name"] for plugin in active] == ["bundled-board"]


def test_canonical_discovery_deduplicates_case_variant_routes(
    monkeypatch, tmp_path
):
    from hermes_cli import config, dashboard_plugin_pages, plugins_cmd

    root = tmp_path / "plugins"
    for name, path in (("alpha", "/Board"), ("beta", "/board")):
        dashboard = root / name / "dashboard"
        dashboard.mkdir(parents=True)
        (dashboard / "manifest.json").write_text(
            json.dumps({"name": name, "tab": {"path": path}}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        dashboard_plugin_pages,
        "_plugin_roots",
        lambda: [(root, "bundled")],
    )
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())

    pages = dashboard_plugin_pages.list_dashboard_plugin_pages()

    assert [page["path"] for page in pages] == ["/Board"]


def test_active_bundled_plugin_pages_are_canonical_and_linkable():
    pages = {page["id"]: page for page in list_dashboard_plugin_pages()}

    assert pages["plugin-kanban"]["path"] == "/kanban"
    assert pages["plugin-hermes-achievements"]["path"] == "/achievements"
    assert {page["group"] for page in pages.values()} == {"extensions"}
    assert build_dashboard_link("plugin-kanban")["url"] == (
        "http://127.0.0.1:9119/kanban"
    )
