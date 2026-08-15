"""Regression test for upstream issue #84451.

A custom provider endpoint configured for a CHILD profile must land in that
profile's config.yaml, be listed under that profile, and leave the Default
profile unchanged. These tests pin the profile-scoping of
/api/providers/custom-endpoints (POST upsert + GET list + activate) to a
named child profile.
"""
import pytest
import yaml


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch, _isolate_hermes_home):
    """Isolated default home + one named profile, each with config + .env."""
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_beta"
    for home in (default_home, worker_home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (worker_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_beta": worker_home}


@pytest.fixture
def client(monkeypatch, isolated_profiles):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _cfg(home):
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def _providers(home):
    providers = _cfg(home).get("providers")
    return providers if isinstance(providers, dict) else {}


_BODY = {
    "name": "Worker Endpoint",
    "base_url": "http://127.0.0.1:9999/v1",
    "model": "worker-model",
}


def test_custom_endpoint_written_for_child_lands_in_child(client, isolated_profiles):
    """POST ?profile=worker_beta must write the endpoint into worker_beta's
    config.yaml, not the default profile's."""
    resp = client.post(
        "/api/providers/custom-endpoints?profile=worker_beta",
        json=dict(_BODY),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    endpoint_id = body.get("id")
    assert endpoint_id

    # The endpoint landed in the CHILD profile.
    child_providers = _providers(isolated_profiles["worker_beta"])
    assert endpoint_id in child_providers, \
        f"endpoint {endpoint_id} not in child config: {child_providers}"
    assert child_providers[endpoint_id]["base_url"] == "http://127.0.0.1:9999/v1"

    # The DEFAULT profile's config is untouched.
    default_providers = _providers(isolated_profiles["default"])
    assert endpoint_id not in default_providers, \
        f"endpoint leaked into Default: {default_providers}"


def test_custom_endpoint_body_profile_scopes_write(client, isolated_profiles):
    """#84451: an explicit body ``profile`` scopes the write even with no
    query param (the desktop carries the child profile in the body)."""
    resp = client.post(
        "/api/providers/custom-endpoints",
        json={**_BODY, "profile": "worker_beta"},
    )
    assert resp.status_code == 200, resp.text
    endpoint_id = resp.json()["id"]
    assert endpoint_id in _providers(isolated_profiles["worker_beta"])
    assert endpoint_id not in _providers(isolated_profiles["default"])


def test_body_profile_beats_query_profile(client, isolated_profiles):
    """The explicit body profile wins over the query param (the repo's
    scoped-write precedence: MessagingPlatformUpdate et al.)."""
    resp = client.post(
        "/api/providers/custom-endpoints?profile=default",
        json={**_BODY, "profile": "worker_beta"},
    )
    assert resp.status_code == 200, resp.text
    endpoint_id = resp.json()["id"]
    assert endpoint_id in _providers(isolated_profiles["worker_beta"])
    assert endpoint_id not in _providers(isolated_profiles["default"])


def test_custom_endpoint_list_scoped_to_child(client, isolated_profiles):
    """GET ?profile=worker_beta lists the child's endpoint; Default's list
    stays empty."""
    client.post("/api/providers/custom-endpoints?profile=worker_beta",
                json=dict(_BODY))
    listed = client.get(
        "/api/providers/custom-endpoints?profile=worker_beta").json()
    child_ids = [e["id"] for e in listed.get("endpoints", [])]
    assert "worker-endpoint" in child_ids

    default_listed = client.get(
        "/api/providers/custom-endpoints?profile=default").json()
    default_ids = [e["id"] for e in default_listed.get("endpoints", [])]
    assert "worker-endpoint" not in default_ids


def test_no_profile_writes_to_default_unchanged(client, isolated_profiles):
    """Single-profile behavior is unchanged: no profile -> Default home."""
    resp = client.post("/api/providers/custom-endpoints", json=dict(_BODY))
    assert resp.status_code == 200, resp.text
    endpoint_id = resp.json()["id"]
    assert endpoint_id in _providers(isolated_profiles["default"])
    assert endpoint_id not in _providers(isolated_profiles["worker_beta"])


def test_activate_scoped_to_child(client, isolated_profiles):
    """The activate path also honors the child profile: it sets the child's
    model.provider, not Default's."""
    client.post("/api/providers/custom-endpoints?profile=worker_beta",
                json=dict(_BODY))
    resp = client.post(
        "/api/providers/custom-endpoints/worker-endpoint/activate"
        "?profile=worker_beta")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    child_model = _cfg(isolated_profiles["worker_beta"]).get("model", {})
    assert child_model.get("provider") == "worker-endpoint"
    default_model = _cfg(isolated_profiles["default"]).get("model", {})
    assert default_model.get("provider") != "worker-endpoint"
