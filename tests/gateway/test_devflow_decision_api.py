"""Trusted HTTP adapter tests for DevFlow grants and DDP decisions."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from devflow_delegation.decision_service import (
    DdpDecisionConflict,
    DdpDecisionExpired,
    DdpDecisionTelemetryError,
    DdpDecisionUnauthorized,
    StagedDdpDecision,
)
from gateway.config import PlatformConfig
from gateway.devflow_auth import DevflowLoginGrantStore
from gateway.platforms.api_server import APIServerAdapter, cors_middleware, security_headers_middleware
from gateway.platforms.telegram_miniapp_auth import MiniAppIdentity


class FakeDecisionService:
    def __init__(self) -> None:
        self.stage_calls: list[dict[str, str]] = []
        self.confirm_calls: list[dict[str, object]] = []
        self.staged = StagedDdpDecision(
            request_id="request-1",
            decision="approve",
            target_state="PLANNED",
            immutable_summary="request-1: fixture -> PLANNED",
            confirmation_token="internal-confirmation-secret",
            expires_at_monotonic=500.0,
        )

    def stage(self, **kwargs):
        self.stage_calls.append(kwargs)
        return self.staged

    def pending(self, token: str):
        return self.staged if token == self.staged.confirmation_token else None

    def confirm(self, **kwargs):
        self.confirm_calls.append(kwargs)
        return "committed"


@dataclass
class Runner:
    actor: str | None = "telegram:admin-42"

    def _ddp_actor_for_source(self, _source):
        return self.actor


def _adapter(*, runner: Runner | None = None) -> tuple[APIServerAdapter, FakeDecisionService]:
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "server-key", "cors_origins": ["http://localhost:3040"]})
    )
    adapter.gateway_runner = runner or Runner()
    adapter._devflow_grant_store = DevflowLoginGrantStore(
        secret=b"pepper",
        token_factory=iter(("browser-login-grant", "opaque-operator-subject")).__next__,
    )
    service = FakeDecisionService()
    adapter._devflow_decision_service = service
    return adapter, service


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware, security_headers_middleware])
    app["api_server_adapter"] = adapter
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    return app


def _headers(*, subject: str | None = None, origin: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer server-key",
        "X-Hermes-Admin-Platform": "telegram",
        "X-Hermes-Admin-Id": "admin-42",
    }
    if subject:
        headers["X-Devflow-Operator-Subject"] = subject
    if origin:
        headers["Origin"] = origin
    return headers


@pytest.mark.asyncio
async def test_grant_mint_requires_signed_explicit_admin_then_redeems_opaque_subject(monkeypatch) -> None:
    adapter, _ = _adapter()
    monkeypatch.setattr(
        "gateway.platforms.api_server.validate_telegram_init_data",
        lambda _data: MiniAppIdentity(
            user_id="admin-42",
            first_name=None,
            username=None,
            raw_init_data="signed-init-data",
        ),
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        denied = await client.post("/api/devflow/auth/grants", json={"audience": "devflow-local"})
        assert denied.status == 401

        forged_headers = _headers()
        forged_headers.pop("Authorization")
        forged = await client.post(
            "/api/devflow/auth/grants",
            headers=forged_headers,
            json={"audience": "devflow-local"},
        )
        assert forged.status == 401

        minted = await client.post(
            "/api/devflow/auth/grants",
            headers={"X-Telegram-Init-Data": "signed-init-data"},
            json={"audience": "devflow-local"},
        )
        assert minted.status == 201
        assert await minted.json() == {"grant": "browser-login-grant", "expires_in": 60}

        redeemed = await client.post(
            "/api/devflow/auth/grants/redeem",
            headers=_headers(),
            json={"audience": "devflow-local", "grant": "browser-login-grant"},
        )
        assert redeemed.status == 200
        assert await redeemed.json() == {"subject": "opaque-operator-subject"}
        assert "admin-42" not in repr(await redeemed.json())

    no_admin, _ = _adapter(runner=Runner(actor=None))
    async with TestClient(TestServer(_app(no_admin))) as client:
        response = await client.post(
            "/api/devflow/auth/grants",
            headers={"X-Telegram-Init-Data": "signed-init-data"},
            json={"audience": "devflow-local"},
        )
        assert response.status == 403
        assert await response.json() == {"error": "request unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["actor", "decided_by", "confirmation_token"])
async def test_decision_stage_rejects_identity_and_confirmation_fields(forbidden: str) -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject
    body = {"request_id": "request-1", "decision": "approve", "rationale": "reviewed", forbidden: "forged"}

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json=body,
        )
        assert response.status == 400
        assert await response.json() == {"error": "invalid request"}
        assert service.stage_calls == []


@pytest.mark.asyncio
async def test_trusted_stage_derives_actor_and_internal_token_never_crosses_browser_cors_or_logs(caplog) -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {
        "request_id": "request-1",
        "decision": "approve",
        "target_state": "PLANNED",
        "immutable_summary": "request-1: fixture -> PLANNED",
        "staged_token": "internal-confirmation-secret",
    }
    assert service.stage_calls == [
        {"request_id": "request-1", "decision": "approve", "actor": "telegram:admin-42", "rationale": "reviewed"}
    ]
    exposed = repr(dict(response.headers)) + caplog.text
    assert "internal-confirmation-secret" not in exposed
    assert "X-Devflow-Confirmation" not in exposed

    async with TestClient(TestServer(_app(adapter))) as client:
        browser_call = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject, origin="http://localhost:3040"),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        assert browser_call.status == 403
        assert "staged_token" not in await browser_call.text()

        preflight = await client.options(
            "/api/devflow/decisions/stage",
            headers={
                "Origin": "http://localhost:3040",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status == 403
        assert "confirmation" not in preflight.headers.get(
            "Access-Control-Allow-Headers", ""
        ).lower()


@pytest.mark.asyncio
async def test_confirm_uses_server_side_staged_decision_and_rejects_browser_confirmation_token() -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject

    async with TestClient(TestServer(_app(adapter))) as client:
        staged = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        assert staged.status == 200

        forged = await client.post(
            "/api/devflow/decisions/confirm",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "confirmation_token": "forged"},
        )
        assert forged.status == 400

        confirmed = await client.post(
            "/api/devflow/decisions/confirm",
            headers=_headers(subject=subject),
            json={
                "request_id": "request-1",
                "decision": "approve",
                "staged_token": "internal-confirmation-secret",
            },
        )
        assert confirmed.status == 200
        assert await confirmed.json() == {"result": "committed", "request_id": "request-1", "state": "PLANNED"}

    assert service.confirm_calls == [{"staged": service.staged, "actor": "telegram:admin-42"}]


@pytest.mark.asyncio
async def test_post_commit_telemetry_failure_reports_committed_degraded() -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject

    def committed_then_telemetry_failed(**kwargs):
        service.confirm_calls.append(kwargs)
        raise DdpDecisionTelemetryError(
            "durable decision committed but telemetry delivery failed"
        )

    service.confirm = committed_then_telemetry_failed
    async with TestClient(TestServer(_app(adapter))) as client:
        staged = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        token = (await staged.json())["staged_token"]
        response = await client.post(
            "/api/devflow/decisions/confirm",
            headers=_headers(subject=subject),
            json={
                "request_id": "request-1",
                "decision": "approve",
                "staged_token": token,
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {
        "result": "committed_telemetry_degraded",
        "request_id": "request-1",
        "state": "PLANNED",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (DdpDecisionUnauthorized("not authorized"), 403),
        (DdpDecisionConflict("request state changed"), 409),
        (RuntimeError("ledger unavailable"), 503),
    ],
)
async def test_stage_maps_typed_domain_and_outage_failures(
    error: Exception,
    expected_status: int,
) -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject

    def fail_stage(**_kwargs):
        raise error

    service.stage = fail_stage
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        payload = await response.json()

    assert response.status == expected_status
    assert payload == {"error": "request unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (DdpDecisionUnauthorized("stale confirmation"), 409),
        (DdpDecisionExpired("confirmation expired"), 409),
        (DdpDecisionConflict("request state changed"), 409),
        (RuntimeError("ledger unavailable"), 503),
    ],
)
async def test_confirm_maps_typed_domain_and_outage_failures(
    error: Exception,
    expected_status: int,
) -> None:
    adapter, service = _adapter()
    subject = adapter._devflow_grant_store.redeem(
        grant=adapter._devflow_grant_store.mint(
            authenticated_actor="telegram:admin-42", audience="devflow-local"
        ),
        audience="devflow-local",
    ).subject

    def fail_confirm(**_kwargs):
        raise error

    service.confirm = fail_confirm
    async with TestClient(TestServer(_app(adapter))) as client:
        staged = await client.post(
            "/api/devflow/decisions/stage",
            headers=_headers(subject=subject),
            json={"request_id": "request-1", "decision": "approve", "rationale": "reviewed"},
        )
        token = (await staged.json())["staged_token"]
        response = await client.post(
            "/api/devflow/decisions/confirm",
            headers=_headers(subject=subject),
            json={
                "request_id": "request-1",
                "decision": "approve",
                "staged_token": token,
            },
        )
        payload = await response.json()

    assert response.status == expected_status
    assert payload == {"error": "request unavailable"}
