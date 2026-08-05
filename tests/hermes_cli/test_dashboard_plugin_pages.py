from __future__ import annotations

import pytest

from hermes_cli.dashboard_plugin_pages import (
    list_dashboard_plugin_pages,
    safe_dashboard_plugin_tab_path,
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


def test_active_bundled_plugin_pages_are_canonical_and_linkable():
    pages = {page["id"]: page for page in list_dashboard_plugin_pages()}

    assert pages["plugin-kanban"]["path"] == "/kanban"
    assert pages["plugin-hermes-achievements"]["path"] == "/achievements"
    assert {page["group"] for page in pages.values()} == {"extensions"}
    assert build_dashboard_link("plugin-kanban")["url"] == (
        "http://127.0.0.1:9119/kanban"
    )
