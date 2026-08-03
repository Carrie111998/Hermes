"""Tests for the Chronos cron-fire webhook (POST /api/cron/fire) — Phase 4E.2.

The webhook authenticates a NAS-minted JWT via the pluggable fire-verifier
(NOT API_SERVER_KEY), then runs the job via the resolved provider's fire_due in
the background, returning 202. These tests monkeypatch the verifier and
resolve_cron_scheduler — the verifier itself is tested with real crypto in
test_chronos_verify.py.
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

_MOD = "gateway.platforms.api_server"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-secret"}))


class _Request:
    def __init__(self, body, *, token="good", json_impl=None):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body
        self._json_impl = json_impl
        self.transport = None
        self.remote = ""
        self.method = "POST"
        self.path_qs = "/api/cron/fire"

    async def json(self):
        if self._json_impl is not None:
            return await self._json_impl()
        return self._body


async def _post(adapter, body, *, token="good", json_impl=None):
    return await adapter._handle_cron_fire(
        _Request(body, token=token, json_impl=json_impl),
    )


def _response_json(response):
    return json.loads(response.text)


@pytest.fixture(autouse=True)
def _stub_aiohttp_response(monkeypatch):
    """Exercise the handler without requiring the optional aiohttp extra."""
    class Response:
        def __init__(self, payload, status=200):
            self.text = json.dumps(payload)
            self.status = status

    monkeypatch.setattr(
        f"{_MOD}.web",
        SimpleNamespace(
            json_response=lambda payload, status=200, **_kwargs: Response(payload, status),
        ),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def adapter():
    return _make_adapter()


class _SpyProvider:
    """Records fire_due calls; stands in for the resolved provider."""

    def __init__(self):
        self.fired = []

    def fire_due(self, job_id, *, nominal_fire_at=None, adapters=None, loop=None):
        self.fired.append((job_id, nominal_fire_at))
        return True


@pytest.mark.anyio
async def test_valid_token_accepts_and_fires(adapter, monkeypatch):
    """Valid NAS-JWT + {job_id} → 202 and fire_due invoked with that id."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    # verifier returns claims (valid token)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {
            "purpose": "cron_fire", "aud": "agent:x", "job_id": "abc123",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )

    resp = await _post(
        adapter, {"job_id": "abc123", "fire_at": "2026-08-01T09:10:00+00:00"},
    )
    assert resp.status == 202
    assert _response_json(resp)["job_id"] == "abc123"

    # fire runs in a background thread/task — give it a beat to land.
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == [("abc123", "2026-08-01T09:10:00+00:00")]


@pytest.mark.anyio
@pytest.mark.parametrize("fire_at", [
    "2026-08-01T09:10:00Z",
    "2026-08-01T10:10:00+01:00",
    " 2026-08-01T09:10:00+00:00 ",
    "2026-08-01T09:10:00.000000+00:00",
])
async def test_noncanonical_fire_body_is_rejected(adapter, monkeypatch, fire_at):
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **_kw: {
            "purpose": "cron_fire", "aud": "agent:x", "job_id": "abc123",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )

    resp = await _post(adapter, {"job_id": "abc123", "fire_at": fire_at})
    assert resp.status == 400
    assert spy.fired == []


@pytest.mark.anyio
@pytest.mark.parametrize("claims", [
    {"purpose": "cron_fire", "job_id": "foreign", "fire_at": "2026-08-01T09:10:00+00:00"},
    {"purpose": "cron_fire", "job_id": "abc123", "fire_at": "2026-08-01T09:11:00+00:00"},
    {"purpose": "cron_fire"},
])
async def test_signed_claims_must_match_exact_job_and_nominal_fire(
    adapter, monkeypatch, claims,
):
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **_kw: claims),
    )

    async def request_json():
        return {"job_id": "abc123", "fire_at": "2026-08-01T09:10:00+00:00"}

    request = SimpleNamespace(
        headers={"Authorization": "Bearer good"},
        json=request_json,
        transport=None,
        remote="",
        method="POST",
        path_qs="/api/cron/fire",
    )
    resp = await adapter._handle_cron_fire(request)
    assert resp.status == 401
    assert spy.fired == []


@pytest.mark.anyio
async def test_invalid_token_401_and_no_fire(adapter, monkeypatch):
    """Bad/forged token → 401, fire_due NOT invoked."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: None),  # verification fails
    )

    resp = await _post(adapter, {"job_id": "abc123"}, token="forged")
    assert resp.status == 401

    await asyncio.sleep(0.05)
    assert spy.fired == []


@pytest.mark.anyio
async def test_missing_token_401(adapter, monkeypatch):
    """No Authorization header → verifier gets empty token → 401."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    # Real verifier: empty token returns None.
    resp = await _post(adapter, {"job_id": "abc123"}, token="")
    assert resp.status == 401
    assert spy.fired == []


@pytest.mark.anyio
async def test_valid_token_refuses_during_gateway_drain(adapter, monkeypatch):
    spy = _SpyProvider()
    runner = SimpleNamespace(_draining=False, _external_drain_active=True)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {
            "purpose": "cron_fire", "job_id": "abc123",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )

    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        response = await _post(adapter, {"job_id": "abc123"})
        payload = _response_json(response)

    assert response.status == 503
    assert payload["error"]["code"] == "gateway_draining"
    assert spy.fired == []


@pytest.mark.anyio
async def test_valid_fire_reservation_blocks_drain_before_body_and_task(adapter, monkeypatch):
    runner = SimpleNamespace(_draining=False, _external_drain_active=False)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    fired = threading.Event()
    release_fire = threading.Event()

    class BlockingProvider:
        def fire_due(self, job_id, *, nominal_fire_at=None, adapters=None, loop=None):
            fired.set()
            release_fire.wait(timeout=2)
            return True

    async def delayed_json():
        body_started.set()
        await release_body.wait()
        return {"job_id": "abc123", "fire_at": "2026-08-01T09:10:00+00:00"}

    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", BlockingProvider)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {
            "purpose": "cron_fire", "job_id": "abc123",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )
    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        request_task = asyncio.create_task(
            _post(adapter, None, json_impl=delayed_json),
        )
        await body_started.wait()
        assert adapter.active_agent_work_count() == 1

        release_body.set()
        response = await request_task
        assert response.status == 202
        await asyncio.to_thread(fired.wait, 2)
        assert adapter.active_agent_work_count() == 1
        release_fire.set()
        for _ in range(50):
            if adapter.active_agent_work_count() == 0:
                break
            await asyncio.sleep(0.01)

    assert adapter.active_agent_work_count() == 0


@pytest.mark.anyio
async def test_missing_job_id_400(adapter, monkeypatch):
    """Valid token but no job_id → 400, no fire."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {
            "purpose": "cron_fire", "job_id": "j9",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )

    resp = await _post(adapter, {})
    assert resp.status == 400
    assert spy.fired == []


@pytest.mark.anyio
async def test_missing_fire_at_400(adapter, monkeypatch):
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )

    resp = await _post(adapter, {"job_id": "abc123"})
    assert resp.status == 400
    assert spy.fired == []


@pytest.mark.anyio
async def test_fire_does_not_require_api_server_key(adapter, monkeypatch):
    """The fire endpoint must NOT gate on API_SERVER_KEY — auth is the NAS-JWT.
    A request with NO API key header but a valid fire token still succeeds."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {
            "purpose": "cron_fire", "job_id": "j9",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }),
    )

    # Bearer is the FIRE token, not the API_SERVER_KEY "sk-secret".
    resp = await _post(
        adapter,
        {"job_id": "j9", "fire_at": "2026-08-01T09:10:00+00:00"},
        token="nas-jwt",
    )
    assert resp.status == 202
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == [("j9", "2026-08-01T09:10:00+00:00")]


@pytest.mark.anyio
async def test_sync_verifier_runs_off_the_event_loop(adapter, monkeypatch):
    """The verifier resolves the signing key from a JWKS URL — a synchronous
    HTTP GET on a cache miss. It must run via asyncio.to_thread, NOT inline on
    the event loop, or a slow/rate-limited portal stalls every other adapter
    sharing the loop. Proof: the sync verifier executes on a worker thread, not
    the loop thread.
    """
    loop_thread_id = threading.get_ident()
    seen = {}

    def blocking_verifier(**kw):
        seen["thread_id"] = threading.get_ident()
        return {
            "purpose": "cron_fire", "job_id": "off-loop",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }

    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: blocking_verifier,
    )

    resp = await _post(
        adapter,
        {"job_id": "off-loop", "fire_at": "2026-08-01T09:10:00+00:00"},
    )
    assert resp.status == 202

    # If the verifier had run inline on the loop, its thread id would equal the
    # loop thread's; to_thread puts it on a distinct worker thread.
    assert seen["thread_id"] != loop_thread_id


@pytest.mark.anyio
async def test_crashing_verifier_fails_closed_401(adapter, monkeypatch):
    """A verifier that raises must be treated as a rejection (401), never admit
    the fire, and never surface as a 500 — this is the only inbound that can
    trigger remote job execution, so it fails closed.
    """
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)

    def exploding_verifier(**kw):
        raise RuntimeError("JWKS endpoint unreachable")

    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: exploding_verifier,
    )

    resp = await _post(adapter, {"job_id": "abc123"}, token="boom")
    assert resp.status == 401

    await asyncio.sleep(0.05)
    assert spy.fired == []


@pytest.mark.anyio
async def test_async_verifier_is_awaited(adapter, monkeypatch):
    """A coroutine verifier (a future async escape-hatch) is awaited directly
    rather than dispatched to a thread — a valid async verify still fires.
    """
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)

    async def async_verifier(**kw):
        return {
            "purpose": "cron_fire", "aud": "agent:x", "job_id": "async-ok",
            "fire_at": "2026-08-01T09:10:00+00:00",
        }

    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: async_verifier,
    )

    resp = await _post(
        adapter,
        {"job_id": "async-ok", "fire_at": "2026-08-01T09:10:00+00:00"},
    )
    assert resp.status == 202

    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == [("async-ok", "2026-08-01T09:10:00+00:00")]
