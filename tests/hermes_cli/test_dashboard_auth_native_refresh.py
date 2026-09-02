"""auth_native_refresh() must dual-log both failure modes (#98338).

Before the fix, provider-unreachable (503) refresh failures reached only
agent.log (the 503 raise pre-empted the audit_log), while all-rejected (401)
failures reached only the audit log (no agent.log WARNING). The two failure
modes were split across two logs with zero overlap, so 66% of a refresh storm
was invisible in the primary log. Both paths must now hit both logs.
"""
import unittest.mock as mock

import pytest
from fastapi import HTTPException

import hermes_cli.dashboard_auth as da
from hermes_cli.dashboard_auth import routes
from hermes_cli.dashboard_auth.audit import AuditEvent
from hermes_cli.dashboard_auth.base import ProviderError, RefreshExpiredError


class _Provider:
    def __init__(self, name, exc):
        self.name = name
        self.supports_session = True
        self._exc = exc

    def refresh_session(self, refresh_token=None):
        raise self._exc


@pytest.fixture
def logs(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "audit_log", lambda event, **kw: calls.append((event, kw)))
    log = mock.MagicMock()
    monkeypatch.setattr(routes, "_log", log)
    monkeypatch.setattr(routes, "_client_ip", lambda request: "1.2.3.4")
    return calls, log


def _reasons(calls):
    return [(event, kw.get("reason")) for event, kw in calls]


@pytest.mark.asyncio
async def test_provider_unreachable_is_audit_logged_before_503(logs, monkeypatch):
    calls, log = logs
    monkeypatch.setattr(da, "list_session_providers", lambda: [_Provider("nous", ProviderError("idp down"))])

    with pytest.raises(HTTPException) as ei:
        await routes.auth_native_refresh(object(), routes._NativeRefreshBody(refresh_token="rt"))

    assert ei.value.status_code == 503
    # audit trail now records the unreachable failure (was invisible before)
    assert (AuditEvent.REFRESH_FAILURE, "provider_unreachable") in _reasons(calls)
    # agent.log still carries the per-provider WARNING
    assert log.warning.called


@pytest.mark.asyncio
async def test_all_rejected_warns_in_agent_log_and_audits(logs, monkeypatch):
    calls, log = logs
    monkeypatch.setattr(da, "list_session_providers", lambda: [_Provider("nous", RefreshExpiredError("expired"))])

    resp = await routes.auth_native_refresh(object(), routes._NativeRefreshBody(refresh_token="rt"))

    assert resp.status_code == 401
    assert (AuditEvent.REFRESH_FAILURE, "all_providers_rejected_rt") in _reasons(calls)
    # now also surfaced in agent.log (was audit-only before)
    assert log.warning.called
