"""Tests for the Chronos cron-fire webhook ON THE DASHBOARD APP (web_server).

Regression guard for the relocation bug: the fire webhook MUST live on the
dashboard FastAPI app (`hermes_cli.web_server.app`) — the agent's public HTTP
surface on hosted deployments — not only on the aiohttp APIServerAdapter (which
hosted agents don't expose). It must:
  - be a registered route on the dashboard app,
  - be in PUBLIC_API_PATHS so the dashboard cookie gate doesn't 401 it before
    the JWT verifier runs,
  - reject a bad/missing NAS-JWT with 401 (the JWT is the real gate),
  - 400 on missing job_id,
  - on a valid token, resolve the job's profile and complete the admitted
    fire/re-arm before returning 202.
"""

import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


def _client(auth_required: bool):
    prev_auth = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = auth_required
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    return client, prev_auth, prev_host


def _restore(prev_auth, prev_host):
    if prev_auth is None:
        if hasattr(web_server.app.state, "auth_required"):
            delattr(web_server.app.state, "auth_required")
    else:
        web_server.app.state.auth_required = prev_auth
    if prev_host is None:
        if hasattr(web_server.app.state, "bound_host"):
            delattr(web_server.app.state, "bound_host")
    else:
        web_server.app.state.bound_host = prev_host


def test_route_registered_on_dashboard_app():
    """The fire webhook is served by the dashboard app (the hosted-agent public
    surface), not only the aiohttp adapter."""
    paths = {r.path for r in web_server.app.routes if hasattr(r, "path")}
    assert "/api/cron/fire" in paths


def test_fire_path_is_public():
    """Must bypass the dashboard cookie gate so the NAS bearer-JWT callback
    reaches the verifier (the JWT is the real auth)."""
    assert "/api/cron/fire" in PUBLIC_API_PATHS


def test_bad_token_401(monkeypatch):
    """Invalid NAS-JWT -> 401, even with the dashboard auth gate ENGAGED
    (proves the route is reachable past the cookie gate and the verifier is the
    gate). fire_due must NOT run."""
    fired = []
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: None),  # verification fails
    )
    monkeypatch.setattr(
        web_server, "_find_exact_cron_job_profile", lambda jid: "default"
    )
    monkeypatch.setattr(web_server, "_fire_hosted_cron_job_for_profile",
                        lambda p, j: fired.append((p, j)))

    client, pa, ph = _client(auth_required=True)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer forged"},
                           json={"job_id": "abc"})
        assert resp.status_code == 401
        assert fired == []
    finally:
        _restore(pa, ph)
        client.close()


def test_missing_job_id_400(monkeypatch):
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={})
        assert resp.status_code == 400
    finally:
        _restore(pa, ph)
        client.close()


def test_unknown_job_200_gone(monkeypatch):
    """Valid token but the job isn't found in any profile -> 200 'gone'
    (NAS shouldn't retry a fire for a cancelled/completed job)."""
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    monkeypatch.setattr(web_server, "_find_exact_cron_job_profile", lambda jid: None)
    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={"job_id": "ghost"})
        assert resp.status_code == 200
        assert resp.json().get("status") == "gone"
    finally:
        _restore(pa, ph)
        client.close()


def test_duplicate_exact_job_ids_are_rejected(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_cron_profile_dicts",
        lambda: [{"name": "first"}, {"name": "second"}],
    )
    monkeypatch.setattr(
        web_server,
        "_call_cron_for_profile",
        lambda _profile, _func, _include_disabled: [{"id": "duplicate"}],
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: pytest.fail("Ambiguous jobs must be rejected before authentication"),
    )

    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire", json={"job_id": "duplicate"})
        assert resp.status_code == 409
        assert "multiple profiles" in resp.json()["detail"]
    finally:
        _restore(pa, ph)
        client.close()


def test_generic_dashboard_lookup_rejects_cross_profile_ambiguity(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_cron_profile_dicts",
        lambda: [{"name": "by-id"}, {"name": "by-name"}],
    )
    jobs = {
        "by-id": [{"id": "target", "name": "first"}],
        "by-name": [{"id": "other", "name": "target"}],
    }
    monkeypatch.setattr(
        web_server,
        "_call_cron_for_profile",
        lambda profile, _func, _include_disabled: jobs[profile],
    )

    with pytest.raises(web_server.HTTPException) as exc:
        web_server._find_cron_job_profile("target")
    assert exc.value.status_code == 409


def test_exact_id_wins_over_other_profile_name_and_scopes_token(
    tmp_path, monkeypatch
):
    named_home = tmp_path / "named"
    exact_home = tmp_path / "exact"
    for home, audience in (
        (named_home, "agent:named"),
        (exact_home, "agent:exact"),
    ):
        home.mkdir()
        (home / "config.yaml").write_text(
            "cron:\n  chronos:\n"
            f"    expected_audience: {audience}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        web_server,
        "_cron_profile_dicts",
        lambda: [{"name": "named"}, {"name": "exact"}],
    )
    jobs = {
        "named": [{"id": "other-id", "name": "target-id"}],
        "exact": [{"id": "target-id", "name": "actual target"}],
    }
    monkeypatch.setattr(
        web_server,
        "_call_cron_for_profile",
        lambda profile, _func, _include_disabled: jobs[profile],
    )
    homes = {"named": named_home, "exact": exact_home}
    monkeypatch.setattr(
        web_server,
        "_cron_profile_home",
        lambda profile: (profile, homes[profile]),
    )
    fired = []
    monkeypatch.setattr(
        web_server,
        "_fire_hosted_cron_job_for_profile",
        lambda profile, job_id: fired.append((profile, job_id)) or True,
    )

    def verifier(**kwargs):
        return (
            {"purpose": "cron_fire"}
            if kwargs["token"] == kwargs["expected_audience"]
            else None
        )

    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: verifier,
    )
    client, pa, ph = _client(auth_required=False)
    try:
        wrong_profile_token = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer agent:named"},
            json={"job_id": "target-id"},
        )
        exact_profile_token = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer agent:exact"},
            json={"job_id": "target-id"},
        )
        assert wrong_profile_token.status_code == 401
        assert exact_profile_token.status_code == 202
    finally:
        _restore(pa, ph)
        client.close()
    assert fired == [("exact", "target-id")]


def test_valid_token_accepts_and_fires(monkeypatch):
    """Valid token + known job -> 202 and fire_due invoked for the resolved
    profile."""
    fired = []
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire", "aud": "agent:x"}),
    )
    monkeypatch.setattr(
        web_server, "_find_exact_cron_job_profile", lambda jid: "default"
    )
    monkeypatch.setattr(web_server, "_fire_hosted_cron_job_for_profile",
                        lambda p, j: fired.append((p, j)) or True)

    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={"job_id": "j1"})
        assert resp.status_code == 202
        assert resp.json()["job_id"] == "j1"
    finally:
        _restore(pa, ph)
        client.close()
    assert fired == [("default", "j1")]


def test_unavailable_exact_profile_provider_returns_retryable_503(monkeypatch):
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **_kw: {"purpose": "cron_fire"}),
    )
    monkeypatch.setattr(
        web_server, "_find_exact_cron_job_profile", lambda _jid: "default"
    )
    monkeypatch.setattr(
        web_server, "_fire_hosted_cron_job_for_profile", lambda _p, _j: None
    )
    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer good"},
            json={"job_id": "j1"},
        )
        assert resp.status_code == 503
    finally:
        _restore(pa, ph)
        client.close()


def test_fire_token_verification_uses_job_profile_config(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    for home, audience in (
        (default_home, "agent:default"),
        (worker_home, "agent:worker"),
    ):
        home.mkdir()
        (home / "config.yaml").write_text(
            "cron:\n  chronos:\n"
            f"    expected_audience: {audience}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        web_server, "_find_exact_cron_job_profile", lambda _jid: "worker"
    )
    monkeypatch.setattr(
        web_server, "_cron_profile_home", lambda _profile: ("worker", worker_home)
    )
    monkeypatch.setattr(
        web_server, "_fire_hosted_cron_job_for_profile", lambda _p, _j: True
    )

    def verifier(**kwargs):
        expected = kwargs["expected_audience"]
        token = kwargs["token"]
        return {"purpose": "cron_fire"} if token == expected else None

    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: verifier,
    )
    client, pa, ph = _client(auth_required=False)
    try:
        default = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer agent:default"},
            json={"job_id": "worker-job"},
        )
        worker = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer agent:worker"},
            json={"job_id": "worker-job"},
        )
        assert default.status_code == 401
        assert worker.status_code == 202
    finally:
        _restore(pa, ph)
        client.close()
