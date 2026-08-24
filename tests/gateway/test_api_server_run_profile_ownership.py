"""Run-scoped endpoints must enforce per-profile ownership (#93689).

Under ``gateway.multiplex_profiles`` the five run-scoped api_server
endpoints authenticated the caller but never asked *which* profile's key
they held: any served profile's ``API_SERVER_KEY`` could read — and stop /
steer / approve — any run on the gateway given its ``run_id``. The fix
stamps the creating request's profile into the run record at first write
and rejects foreign lookups with the same 404 shape as a run that does not
exist (403 would confirm the run id exists under another profile).

These tests mirror production wiring: a test middleware binds
``_api_request_profile`` from a header the way the real one binds it from
the URL prefix, and the per-profile secret lookup is stubbed so alpha and
beta each hold their own valid key — the attacker is an authenticated,
legitimate key holder of a *different* profile.
"""

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile


_PROFILE_KEYS = {
    "default": "sk-secret",
    "alpha": "a" * 32,
    "beta": "b" * 32,
}


@pytest.fixture
def adapter(monkeypatch):
    config = PlatformConfig(enabled=True, extra={"key": _PROFILE_KEYS["default"]})
    adapter = APIServerAdapter(config)

    def fake_get_secret(name, default=""):
        if name != "API_SERVER_KEY":
            return default
        return _PROFILE_KEYS.get(_api_request_profile.get() or "default", default)

    # The handlers import these lazily from their modules, so patching the
    # module attributes is what the call sites actually observe.
    monkeypatch.setattr("agent.secret_scope.get_secret", fake_get_secret)
    return adapter


def _auth(profile: str) -> dict:
    return {
        "Authorization": f"Bearer {_PROFILE_KEYS[profile]}",
        "X-Test-Profile": profile,
    }


@web.middleware
async def _profile_mw(request, handler):
    """Mirror the production middleware: bind the URL-selected profile."""
    token = _api_request_profile.set(request.headers.get("X-Test-Profile"))
    try:
        return await handler(request)
    finally:
        _api_request_profile.reset(token)


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[_profile_mw])
    app["api_server_adapter"] = adapter
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _stamp_run(adapter: APIServerAdapter, run_id: str, profile: str, status: str = "running"):
    """Create a registry record as if the named profile had started the run."""
    token = _api_request_profile.set(profile)
    try:
        adapter._set_run_status(run_id, status)
    finally:
        _api_request_profile.reset(token)


def test_run_records_stamp_profile_at_first_write_only(adapter):
    token = _api_request_profile.set("alpha")
    try:
        adapter._set_run_status("run_a", "queued")
    finally:
        _api_request_profile.reset(token)

    assert adapter._run_statuses["run_a"]["profile"] == "alpha"
    # A later update from a contextless worker (event callback, stop path)
    # must not re-stamp or clobber the owner.
    adapter._set_run_status("run_a", "running", last_event="run.started")
    assert adapter._run_statuses["run_a"]["profile"] == "alpha"


def test_visibility_gate_is_fail_closed(adapter):
    _stamp_run(adapter, "run_a", "alpha")

    token = _api_request_profile.set("alpha")
    try:
        assert adapter._run_visible_to_request("run_a") is True
    finally:
        _api_request_profile.reset(token)

    token = _api_request_profile.set("beta")
    try:
        assert adapter._run_visible_to_request("run_a") is False
    finally:
        _api_request_profile.reset(token)

    # No ContextVar = default listener; alpha's run is not visible there.
    assert adapter._run_visible_to_request("run_a") is False
    # Unknown ids pass through so callers return their own not-found shape.
    assert adapter._run_visible_to_request("run_nope") is True
    # A record without a stamp cannot occur since first-write stamping;
    # if it ever does, it is NOT visible (fail closed).
    adapter._run_statuses["run_ghost"] = {"run_id": "run_ghost", "status": "running"}
    assert adapter._run_visible_to_request("run_ghost") is False


@pytest.mark.asyncio
async def test_get_run_cross_profile_404_is_indistinguishable(adapter):
    app = _create_runs_app(adapter)
    _stamp_run(adapter, "run_a", "alpha")

    async with TestClient(TestServer(app)) as cli:
        owner = await cli.get("/v1/runs/run_a", headers=_auth("alpha"))
        assert owner.status == 200
        payload = await owner.json()
        # The ownership stamp is internal bookkeeping, not API surface.
        assert "profile" not in payload

        attacker = await cli.get("/v1/runs/run_a", headers=_auth("beta"))
        assert attacker.status == 404
        attacker_body = await attacker.json()

        missing = await cli.get("/v1/runs/run_missing", headers=_auth("beta"))
        missing_body = await missing.json()

    # Same error shape as a nonexistent run (no existence oracle for other
    # profiles' ids); the message names the queried id, exactly as the
    # not-found branch would for an id that does not exist for beta.
    assert attacker_body["error"]["code"] == missing_body["error"]["code"] == "run_not_found"
    assert set(attacker_body["error"]) == set(missing_body["error"])
    assert attacker_body["error"]["message"] == "Run not found: run_a"


@pytest.mark.asyncio
async def test_stop_run_cross_profile_is_blocked_and_run_survives(adapter):
    app = _create_runs_app(adapter)
    _stamp_run(adapter, "run_a", "alpha")
    agent = MagicMock()
    adapter._active_run_agents["run_a"] = agent
    adapter._active_run_tasks["run_a"] = MagicMock()

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/v1/runs/run_a/stop", headers=_auth("beta"))
        body = await resp.json()
        owner_view = await cli.get("/v1/runs/run_a", headers=_auth("alpha"))

    assert resp.status == 404
    assert body["error"]["code"] == "run_not_found"
    # The attack had no effect: no interrupt, no state transition, and the
    # owner still sees their run intact.
    agent.interrupt.assert_not_called()
    assert adapter._run_statuses["run_a"]["status"] == "running"
    assert owner_view.status == 200


@pytest.mark.asyncio
async def test_steer_and_approval_cross_profile_return_404(adapter):
    app = _create_runs_app(adapter)
    _stamp_run(adapter, "run_a", "alpha", status="waiting_for_approval")

    async with TestClient(TestServer(app)) as cli:
        steer = await cli.post(
            "/v1/runs/run_a/steer",
            json={"input": "inject guidance"},
            headers=_auth("beta"),
        )
        approval = await cli.post(
            "/v1/runs/run_a/approval",
            json={"choice": "approve"},
            headers=_auth("beta"),
        )

    assert steer.status == 404
    assert approval.status == 404


@pytest.mark.asyncio
async def test_single_profile_default_flow_is_unchanged(adapter):
    """No multiplex: runs created and read through the default listener
    (no profile prefix) keep working exactly as before."""
    app = _create_runs_app(adapter)
    adapter._set_run_status("run_d", "running")  # no ContextVar → default
    adapter._active_run_agents["run_d"] = MagicMock()

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(
            "/v1/runs/run_d", headers=_auth("default"))
        assert resp.status == 200
        payload = await resp.json()
        assert payload["status"] == "running"
        assert "profile" not in payload

        stop = await cli.post(
            "/v1/runs/run_d/stop", headers=_auth("default"))
        assert stop.status == 200
