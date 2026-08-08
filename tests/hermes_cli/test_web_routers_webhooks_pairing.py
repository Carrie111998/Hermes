"""Regression tests for the extracted webhooks/pairing router modules (s4 w1b).

Covers the logic moved out of ``hermes_cli/web_server.py`` into
``hermes_cli/web_routers/webhooks.py`` (c7) and
``hermes_cli/web_routers/pairing.py`` (c6): webhook subscription
summarisation, webhook name/enable validation semantics, pairing store
resolution, and route registration on the new routers.

Sandbox mode: when the patch is not yet applied to the working tree
(``S4_W1B_SANDBOX_NEW`` env var points at the patch's ``new/`` tree), the
modules are loaded from that tree by file path so the tests exercise the
exact extracted code.  In the applied repo the normal import path is used.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SANDBOX_NEW = os.environ.get("S4_W1B_SANDBOX_NEW")


def _load_router(name: str):
    """Import ``hermes_cli.web_routers.<name>``, falling back to the sandbox
    ``new/`` tree when the module does not exist in the working tree yet."""
    modname = f"hermes_cli.web_routers.{name}"
    try:
        return importlib.import_module(modname)
    except ImportError:
        if not _SANDBOX_NEW:
            raise
    path = Path(_SANDBOX_NEW) / "hermes_cli" / "web_routers" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(modname, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


webhooks = _load_router("webhooks")
pairing = _load_router("pairing")


# ---------------------------------------------------------------------------
# c7 webhooks helpers
# ---------------------------------------------------------------------------


def test_webhook_route_summary_shapes_payload():
    summary = webhooks._webhook_route_summary(
        "todoist",
        {
            "description": "Todoist filter",
            "events": ["message"],
            "deliver": "telegram",
            "deliver_only": True,
            "prompt": "summarise",
            "script": "filter.py",
            "skills": ["a", "b"],
            "created_at": "2026-01-01T00:00:00Z",
            "secret": "s3cr3t",
        },
        "https://example.com",
    )
    assert summary["name"] == "todoist"
    assert summary["description"] == "Todoist filter"
    assert summary["events"] == ["message"]
    assert summary["deliver"] == "telegram"
    assert summary["deliver_only"] is True
    assert summary["prompt"] == "summarise"
    assert summary["script"] == "filter.py"
    assert summary["skills"] == ["a", "b"]
    assert summary["url"] == "https://example.com/webhooks/todoist"
    # Secret is masked on read; only the presence flag is surfaced.
    assert summary["secret_set"] is True
    assert "secret" not in summary


def test_webhook_route_summary_defaults():
    summary = webhooks._webhook_route_summary("bare", {}, "http://localhost")
    assert summary["deliver"] == "log"
    assert summary["deliver_only"] is False
    assert summary["events"] == []
    assert summary["secret_set"] is False
    assert summary["enabled"] is True


def test_webhook_route_summary_enabled_false_only_when_explicit():
    assert webhooks._webhook_route_summary("a", {}, "u")["enabled"] is True
    assert webhooks._webhook_route_summary("a", {"enabled": True}, "u")["enabled"] is True
    assert webhooks._webhook_route_summary("a", {"enabled": False}, "u")["enabled"] is False


def test_create_webhook_rejects_unknown_event_name(monkeypatch):
    import hermes_cli.webhook as wh

    # Bypass the platform-enable gate: this test targets the name validation.
    monkeypatch.setattr(wh, "_is_webhook_enabled", lambda: True)

    class _Body:
        name = "bad name!"
        deliver = "log"
        deliver_only = False
        deliver_chat_id = None
        description = None
        events = []
        secret = None
        prompt = None
        skills = []
        script = None

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.create_webhook(_Body()))
    assert exc.value.status_code == 400
    assert "Invalid name" in exc.value.detail


def test_create_webhook_rejects_deliver_only_with_log_delivery(monkeypatch):
    import hermes_cli.webhook as wh
    from fastapi import HTTPException

    monkeypatch.setattr(wh, "_is_webhook_enabled", lambda: True)

    class _Body:
        name = "okname"
        deliver = "log"
        deliver_only = True
        deliver_chat_id = None
        description = None
        events = ["message"]
        secret = None
        prompt = None
        skills = []
        script = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.create_webhook(_Body()))
    assert exc.value.status_code == 400
    assert "Direct delivery requires a real target" in exc.value.detail


def test_delete_webhook_404_for_missing(monkeypatch):
    import hermes_cli.webhook as wh
    from fastapi import HTTPException

    monkeypatch.setattr(wh, "_load_subscriptions", lambda: {})
    monkeypatch.setattr(wh, "_save_subscriptions", lambda s: None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.delete_webhook("nope"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# c6 pairing helpers
# ---------------------------------------------------------------------------


def test_pairing_store_defaults_to_global_store(monkeypatch, tmp_path):
    """Unspecified/current profile resolves to the dashboard's own store."""
    import gateway.pairing as gp

    # Isolate the store dir so no real ~/.hermes state is touched.
    monkeypatch.setattr(gp, "PAIRING_DIR", tmp_path)
    store = pairing._pairing_store(None)
    assert store._dir == gp.PairingStore()._dir == tmp_path
    store2 = pairing._pairing_store("current")
    assert store2._dir == tmp_path


def test_pairing_store_rejects_unknown_profile(monkeypatch):
    from fastapi import HTTPException

    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)

    with pytest.raises(HTTPException) as exc:
        pairing._pairing_store("ghost-profile-xyz")
    assert exc.value.status_code == 404


def test_list_pairing_shape(monkeypatch, tmp_path):
    import gateway.pairing as gp

    monkeypatch.setattr(gp, "PAIRING_DIR", tmp_path)
    result = asyncio.run(pairing.list_pairing())
    assert set(result) == {"pending", "approved"}
    assert result["pending"] == []
    assert result["approved"] == []


def test_approve_pairing_by_code_roundtrip(monkeypatch, tmp_path):
    import gateway.pairing as gp

    monkeypatch.setattr(gp, "PAIRING_DIR", tmp_path)
    store = gp.PairingStore()
    bot_code = store.generate_code("telegram", "user9", "Ursula")

    class _Body:
        profile = None
        platform = "telegram"
        request_id = None
        code = bot_code

    result = asyncio.run(pairing.approve_pairing(_Body()))
    assert result["ok"] is True
    assert result["user"]["user_id"] == "user9"
    assert gp.PairingStore().is_approved("telegram", "user9") is True


def test_clear_pending_pairing(monkeypatch, tmp_path):
    import gateway.pairing as gp

    monkeypatch.setattr(gp, "PAIRING_DIR", tmp_path)
    gp.PairingStore().generate_code("telegram", "userA", "Alice")
    gp.PairingStore().generate_code("discord", "userB", "Bob")

    result = asyncio.run(pairing.clear_pending_pairing())
    assert result == {"ok": True, "cleared": 2}
    assert gp.PairingStore().list_pending() == []


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_webhooks_router_registers_all_five_routes():
    paths = {(r.path, tuple(sorted(r.methods))) for r in webhooks.router.routes}
    assert ("/api/webhooks", ("GET",)) in paths
    assert ("/api/webhooks", ("POST",)) in paths
    assert ("/api/webhooks/enable", ("POST",)) in paths
    assert ("/api/webhooks/{name}", ("DELETE",)) in paths
    assert ("/api/webhooks/{name}/enabled", ("PUT",)) in paths


def test_pairing_router_registers_all_four_routes():
    paths = {(r.path, tuple(sorted(r.methods))) for r in pairing.router.routes}
    assert ("/api/pairing", ("GET",)) in paths
    assert ("/api/pairing/approve", ("POST",)) in paths
    assert ("/api/pairing/revoke", ("POST",)) in paths
    assert ("/api/pairing/clear-pending", ("POST",)) in paths
