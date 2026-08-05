from __future__ import annotations

import pytest


@pytest.fixture
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_dashboard_pages_endpoint_returns_public_route_metadata(client):
    response = client.get("/api/dashboard/pages")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["pages"])
    sessions = next(page for page in body["pages"] if page["id"] == "sessions")
    assert sessions["path"] == "/sessions"
    assert set(sessions) == {"id", "label", "path", "group", "description"}


def test_dashboard_pages_endpoint_filters_pages(client):
    response = client.get("/api/dashboard/pages", params={"query": "models"})

    assert response.status_code == 200
    assert [page["id"] for page in response.json()["pages"]] == ["models"]


def test_dashboard_pages_endpoint_includes_active_plugin_routes(client):
    response = client.get("/api/dashboard/pages", params={"query": "kanban"})

    assert response.status_code == 200
    pages = response.json()["pages"]
    assert len(pages) == 1
    assert pages[0]["id"] == "plugin-kanban"
    assert pages[0]["label"] == "Kanban"
    assert pages[0]["path"] == "/kanban"
    assert pages[0]["group"] == "extensions"


def test_dashboard_theme_api_lists_all_studio_appearance_modes(client):
    response = client.get("/api/dashboard/themes")

    assert response.status_code == 200
    themes = {theme["name"]: theme for theme in response.json()["themes"]}
    assert themes["studio-system"]["label"] == "Hermes Studio — System"
    assert themes["studio-light"]["label"] == "Hermes Studio — Light"
    assert themes["studio-dark"]["label"] == "Hermes Studio — Dark"


def test_dashboard_docs_deep_link_is_not_shadowed_by_swagger(client):
    dashboard_docs = client.get("/docs")
    api_docs = client.get("/api/docs")

    assert dashboard_docs.status_code == 200
    assert "Swagger UI" not in dashboard_docs.text
    assert '<div id="root"></div>' in dashboard_docs.text
    assert api_docs.status_code == 200
    assert "Swagger UI" in api_docs.text


def test_dashboard_pages_endpoint_requires_dashboard_auth():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.web_server import app

    response = TestClient(app).get("/api/dashboard/pages")
    assert response.status_code == 401
