"""Tests for the Chronos cron-fire webhook (POST /api/cron/fire) — Phase 4E.2.

The webhook authenticates a NAS-minted JWT via the pluggable fire-verifier
(NOT API_SERVER_KEY), then runs the job via the resolved provider's fire_due in
the background, returning 202. These tests monkeypatch the verifier and
resolve_cron_scheduler — the verifier itself is tested with real crypto in
test_chronos_verify.py.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware

_MOD = "gateway.platforms.api_server"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-secret"}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/cron/fire", adapter._handle_cron_fire)
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


class _SpyProvider:
    """Records fire_due calls; stands in for the resolved provider."""

    def __init__(self):
        self.fired = []

    def fire_due(self, job_id, *, adapters=None, loop=None):
        self.fired.append(job_id)
        return True


def _install_provider(monkeypatch, provider):
    @contextmanager
    def borrow(**_kwargs):
        yield provider

    monkeypatch.setattr(
        "cron.scheduler_runtime.borrow_scheduler_provider", borrow
    )
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [{"id": "abc123"}, {"id": "j1"}])


@pytest.mark.asyncio
async def test_fire_admission_does_not_deadlock_one_worker_executor(
    adapter, monkeypatch
):
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **_kw: {"purpose": "cron_fire"}),
    )

    class Request:
        headers = {"Authorization": "Bearer good"}

        async def json(self):
            return {"job_id": "j1"}

    loop = asyncio.get_running_loop()
    old_executor = getattr(loop, "_default_executor", None)
    one_worker = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(one_worker)
    try:
        response = await asyncio.wait_for(adapter._handle_cron_fire(Request()), 1)
        assert response.status == 202
    finally:
        loop.set_default_executor(old_executor or ThreadPoolExecutor())
        one_worker.shutdown(wait=True)
    assert spy.fired == ["j1"]


@pytest.mark.asyncio
async def test_multiplex_fire_resolves_owner_before_profile_auth(
    adapter, tmp_path, monkeypatch
):
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    default = tmp_path / "default"
    worker = tmp_path / "worker"
    for home in (default, worker):
        (home / "cron").mkdir(parents=True)
    providers = {"default": _SpyProvider(), "worker": _SpyProvider()}
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True)
    )

    @contextmanager
    def profile_scope(profile):
        home = worker if profile == "worker" else default
        token = set_hermes_home_override(home)
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    @contextmanager
    def borrow(*, hermes_home):
        name = "worker" if Path(hermes_home) == worker.resolve() else "default"
        yield providers[name]

    monkeypatch.setattr(adapter, "_profile_scope", profile_scope)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", default), ("worker", worker)],
    )
    monkeypatch.setattr(
        "cron.jobs.load_jobs",
        lambda: [{"id": "worker-job"}] if get_hermes_home() == worker.resolve() else [],
    )
    monkeypatch.setattr(
        "cron.scheduler_runtime.borrow_scheduler_provider", borrow
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "cron": {
                "chronos": {
                    "expected_audience": (
                        "worker-audience"
                        if get_hermes_home() == worker.resolve()
                        else "default-audience"
                    )
                }
            }
        },
    )
    verified = []

    def verifier(**kwargs):
        verified.append((get_hermes_home(), kwargs["token"], kwargs["expected_audience"]))
        return {"purpose": "cron_fire"} if kwargs["token"] == "worker-token" else None

    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: verifier,
    )

    class Request:
        headers = {"Authorization": "Bearer worker-token"}

        async def json(self):
            return {"job_id": "worker-job"}

    response = await adapter._handle_cron_fire(Request())
    assert response.status == 202
    for _ in range(50):
        if providers["worker"].fired:
            break
        await asyncio.sleep(0.01)
    assert providers["default"].fired == []
    assert providers["worker"].fired == ["worker-job"]
    assert verified == [(worker.resolve(), "worker-token", "worker-audience")]


@pytest.mark.asyncio
async def test_valid_token_accepts_and_fires(adapter, monkeypatch):
    """Valid NAS-JWT + {job_id} → 202 and fire_due invoked with that id."""
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    # verifier returns claims (valid token)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire", "aud": "agent:x"}),
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer good"},
                              json={"job_id": "abc123"})
        assert resp.status == 202
        data = await resp.json()
        assert data["job_id"] == "abc123"

    # fire runs in a background thread/task — give it a beat to land.
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == ["abc123"]


@pytest.mark.asyncio
async def test_invalid_token_401_and_no_fire(adapter, monkeypatch):
    """Bad/forged token → 401, fire_due NOT invoked."""
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: None),  # verification fails
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer forged"},
                              json={"job_id": "abc123"})
        assert resp.status == 401

    await asyncio.sleep(0.05)
    assert spy.fired == []


@pytest.mark.asyncio
async def test_missing_token_401(adapter, monkeypatch):
    """No Authorization header → verifier gets empty token → 401."""
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    # Real verifier: empty token returns None.
    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire", json={"job_id": "abc123"})
        assert resp.status == 401
    assert spy.fired == []


@pytest.mark.asyncio
async def test_valid_token_refuses_during_gateway_drain(adapter, monkeypatch):
    spy = _SpyProvider()
    runner = SimpleNamespace(_draining=False, _external_drain_active=True)
    _install_provider(monkeypatch, spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )

    app = _create_app(adapter)
    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(app)) as cli:
            response = await cli.post(
                "/api/cron/fire",
                headers={"Authorization": "Bearer good"},
                json={"job_id": "abc123"},
            )
            payload = await response.json()

    assert response.status == 503
    assert payload["error"]["code"] == "gateway_draining"
    assert spy.fired == []


@pytest.mark.asyncio
async def test_valid_fire_reservation_blocks_drain_before_body_and_task(adapter, monkeypatch):
    runner = SimpleNamespace(_draining=False, _external_drain_active=False)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    fired = threading.Event()
    release_fire = threading.Event()

    class BlockingProvider:
        def fire_due(self, job_id, *, adapters=None, loop=None):
            fired.set()
            release_fire.wait(timeout=2)
            return True

    original_json = web.Request.json

    async def delayed_json(request):
        body_started.set()
        await release_body.wait()
        return await original_json(request)

    _install_provider(monkeypatch, BlockingProvider())
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    app = _create_app(adapter)
    with patch("gateway.run._gateway_runner_ref", lambda: runner), patch.object(
        web.Request, "json", delayed_json
    ):
        async with TestClient(TestServer(app)) as cli:
            request_task = asyncio.create_task(
                cli.post(
                    "/api/cron/fire",
                    headers={"Authorization": "Bearer good"},
                    json={"job_id": "abc123"},
                )
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


@pytest.mark.asyncio
async def test_cancelled_fire_task_keeps_provider_admission_until_worker_returns(
    adapter, tmp_path, monkeypatch
):
    from cron.scheduler_lease import SchedulerOwnershipLease
    from cron.scheduler_runtime import OwnedSchedulerRuntime, SchedulerOwnershipPolicy
    from tests.cron.test_scheduler_runtime import _Provider, _run, _wait

    policy = SchedulerOwnershipPolicy("gateway", "external")
    worker_entered = threading.Event()
    release_worker = threading.Event()

    class BlockingProvider(_Provider):
        def __init__(self):
            super().__init__(returns=True)

        def fire_due(self, _job_id, *, adapters=None, loop=None):
            worker_entered.set()
            release_worker.wait()
            return True

    provider = BlockingProvider()
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **_kw: {"purpose": "cron_fire"}),
    )
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [{"id": "j1"}])
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, runtime_thread = _run(runtime)
    _wait(provider.started.is_set)

    class Request:
        headers = {"Authorization": "Bearer good"}

        async def json(self):
            return {"job_id": "j1"}

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(tmp_path)
    try:
        response = await adapter._handle_cron_fire(Request())
    finally:
        reset_hermes_home_override(home_token)
    assert response.status == 202
    assert worker_entered.wait(1)
    fire_task = next(task for task in adapter._background_tasks if not task.done())
    fire_task.cancel()
    await asyncio.sleep(0)

    stop.set()
    time.sleep(0.05)
    assert runtime_thread.is_alive()
    assert (
        SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        is None
    )

    release_worker.set()
    runtime_thread.join(2)
    takeover = SchedulerOwnershipLease.try_acquire(
        hermes_home=tmp_path, owner="desktop", provider="builtin"
    )
    assert takeover is not None
    takeover.release()


@pytest.mark.asyncio
async def test_missing_job_id_400(adapter, monkeypatch):
    """Valid token but no job_id → 400, no fire."""
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer good"},
                              json={})
        assert resp.status == 400
    assert spy.fired == []


@pytest.mark.asyncio
async def test_fire_does_not_require_api_server_key(adapter, monkeypatch):
    """The fire endpoint must NOT gate on API_SERVER_KEY — auth is the NAS-JWT.
    A request with NO API key header but a valid fire token still succeeds."""
    spy = _SpyProvider()
    _install_provider(monkeypatch, spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [{"id": "j9"}])

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        # Bearer is the FIRE token, not the API_SERVER_KEY "sk-secret".
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer nas-jwt"},
                              json={"job_id": "j9"})
        assert resp.status == 202
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == ["j9"]
