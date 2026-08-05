from __future__ import annotations

import pytest


def test_dashboard_pages_are_unique_canonical_safe_routes():
    from hermes_cli.dashboard_pages import list_dashboard_pages

    pages = list_dashboard_pages()
    ids = [page["id"] for page in pages]
    paths = [page["path"] for page in pages]

    assert {"sessions", "chat", "models", "mcp"}.issubset(ids)
    assert len(ids) == len(set(ids))
    assert len(paths) == len(set(paths))
    assert all(path.startswith("/") for path in paths)
    assert all("?" not in path and "#" not in path and "\\" not in path for path in paths)
    assert all(page["group"] in {"workspace", "automations", "integrations", "manage"} for page in pages)


def test_dashboard_page_search_matches_id_label_description_and_group():
    from hermes_cli.dashboard_pages import list_dashboard_pages

    assert [page["id"] for page in list_dashboard_pages("conversation")] == ["sessions", "chat"]
    assert any(page["id"] == "mcp" for page in list_dashboard_pages("integrations"))
    assert list_dashboard_pages("definitely-not-a-page") == []


def test_build_dashboard_link_returns_credential_free_canonical_url():
    from hermes_cli.dashboard_pages import build_dashboard_link

    result = build_dashboard_link("sessions", "http://127.0.0.1:9119/")

    assert result["id"] == "sessions"
    assert result["path"] == "/sessions"
    assert result["url"] == "http://127.0.0.1:9119/sessions"
    assert result["markdown"] == "[Open Sessions](http://127.0.0.1:9119/sessions)"
    assert "token" not in result["url"].lower()


def test_build_dashboard_link_supports_reverse_proxy_prefix():
    from hermes_cli.dashboard_pages import build_dashboard_link

    result = build_dashboard_link("models", "https://example.test/hermes")

    assert result["url"] == "https://example.test/hermes/models"


@pytest.mark.parametrize(
    "base_url",
    [
        "javascript:alert(1)",
        "file:///tmp/dashboard",
        "http://user:password@127.0.0.1:9119",
        "http://127.0.0.1:9119/?token=secret",
        "http://127.0.0.1:9119/#secret",
        "https://example.test/hermes/../admin",
        "https://example.test/%2e%2e/admin",
        "https://example.test/%2F%2Fevil.test",
        "https://example.test//evil.test",
        "https://example.test/\\evil.test",
        "https://example.test:bad/hermes",
        "https://example.test/foo) [Click](https://evil.test",
        "https://example.test/foo bar",
        "https://example.test/foo%29bar",
        "https://example.test) [Click](https://evil.test/hermes",
        "http://[fe80::1%foo)]/hermes",
        "http://[fe80::1%foo bar]/hermes",
        "http://[fe80::1%foo\\bar]/hermes",
    ],
)
def test_build_dashboard_link_rejects_unsafe_base_urls(base_url: str):
    from hermes_cli.dashboard_pages import build_dashboard_link

    with pytest.raises(ValueError):
        build_dashboard_link("sessions", base_url)


def test_build_dashboard_link_rejects_unknown_pages():
    from hermes_cli.dashboard_pages import build_dashboard_link

    with pytest.raises(KeyError):
        build_dashboard_link("not-real", "http://127.0.0.1:9119")


def test_build_dashboard_link_accepts_safe_scoped_ipv6_prefix():
    from hermes_cli.dashboard_pages import build_dashboard_link

    result = build_dashboard_link("sessions", "http://[fe80::1%25en0]/hermes")

    assert result["url"] == "http://[fe80::1%25en0]/hermes/sessions"


def test_configured_dashboard_link_uses_operator_environment(monkeypatch):
    from hermes_cli.dashboard_pages import build_configured_dashboard_link

    monkeypatch.setenv("HERMES_DASHBOARD_URL", "https://dashboard.example/hermes")

    result = build_configured_dashboard_link("models")

    assert result["url"] == "https://dashboard.example/hermes/models"
