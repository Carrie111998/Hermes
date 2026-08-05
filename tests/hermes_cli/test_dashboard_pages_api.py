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


def test_dashboard_theme_api_lists_all_studio_appearance_modes(client):
    response = client.get("/api/dashboard/themes")

    assert response.status_code == 200
    themes = {theme["name"]: theme for theme in response.json()["themes"]}
    assert themes["studio-system"]["label"] == "Hermes Studio — System"
    assert themes["studio-light"]["label"] == "Hermes Studio — Light"
    assert themes["studio-dark"]["label"] == "Hermes Studio — Dark"


def test_dashboard_pages_endpoint_requires_dashboard_auth():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.web_server import app

    response = TestClient(app).get("/api/dashboard/pages")
    assert response.status_code == 401
