from agent import usage_contract
from tui_gateway import server


def test_usage_accounts_rpc_exposes_v1_contract_without_session(monkeypatch):
    payload = {
        "contract": {"name": "usage.accounts", "version": 1},
        "capabilities": {
            "provider_usage": {"per_account": True, "providers": []},
            "credential_pool_health": True,
            "local_session_analytics": True,
        },
        "generated_at": "2026-08-10T00:00:00Z",
        "providers": [],
        "local": {"status": "unavailable"},
    }
    monkeypatch.setattr(usage_contract, "build_usage_contract", lambda **kwargs: payload)

    response = server._methods["usage.accounts"]("usage-1", {"refresh": False})

    assert response == {"jsonrpc": "2.0", "id": "usage-1", "result": payload}
    assert "usage.accounts" in server._LONG_HANDLERS
