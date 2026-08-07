"""Seam-identity + pure-function tests for the s3-w1b extractions (R4-C1, R4-C4).

``hermes_cli/web_routers/providers_custom_endpoints.py`` and
``hermes_cli/web_routers/telegram_onboarding.py`` hold two clusters moved out
of ``hermes_cli/web_server.py`` verbatim (god-file shard s3, epic #78791):

* custom endpoints — OpenAI-compatible provider CRUD, activation, live
  ``/models`` probing;
* telegram onboarding — QR pairing lifecycle (start/poll/apply/cancel).

The seam-identity tests pin the regression this extraction must prevent:
``web_server`` must resolve every moved name to the *same object* the router
module defines (legacy re-exports), and the routers must be mounted on the app
so the HTTP routes keep working.  The pure-function tests exercise the moved
helpers directly.
"""

from fastapi.testclient import TestClient

from hermes_cli import web_server as ws
from hermes_cli.web_routers import providers_custom_endpoints as ce
from hermes_cli.web_routers import telegram_onboarding as tg

CE_MOVED_NAMES = (
    "_api_key_display",
    "_config_api_key_is_env_ref",
    "_custom_endpoint_id",
    "_custom_endpoint_response",
    "_detach_main_model_from_provider",
    "_models_from_custom_endpoint_entry",
    "_parse_model_ids",
    "_write_custom_endpoint",
    "activate_custom_endpoint",
    "delete_custom_endpoint",
    "list_custom_endpoints",
    "upsert_custom_endpoint",
    "validate_custom_endpoint",
)

TG_MOVED_NAMES = (
    "_TELEGRAM_ONBOARDING_DEFAULT_URL",
    "_TELEGRAM_ONBOARDING_USER_AGENT",
    "_TelegramOnboardingPairing",
    "_normalize_telegram_user_id",
    "_parse_expiry_ts",
    "_prune_telegram_onboarding_pairings",
    "_restart_gateway_after_telegram_onboarding",
    "_telegram_onboarding_base_url",
    "_telegram_onboarding_error_message",
    "_telegram_onboarding_lock",
    "_telegram_onboarding_pairings",
    "_telegram_onboarding_request",
    "apply_telegram_onboarding",
    "cancel_telegram_onboarding",
    "get_telegram_onboarding_status",
    "start_telegram_onboarding",
)


def test_custom_endpoint_names_are_seam_identical():
    for name in CE_MOVED_NAMES:
        assert getattr(ws, name, None) is getattr(ce, name, None), name


def test_telegram_onboarding_names_are_seam_identical():
    for name in TG_MOVED_NAMES:
        assert getattr(ws, name, None) is getattr(tg, name, None), name


def test_custom_endpoint_routes_registered():
    paths = {rt.path for rt in ws.app.routes if "/api/providers/custom-endpoints" in getattr(rt, "path", "")}
    assert paths == {
        "/api/providers/custom-endpoints",
        "/api/providers/custom-endpoints/{endpoint_id}/activate",
        "/api/providers/custom-endpoints/{endpoint_id}",
        "/api/providers/custom-endpoints/validate",
    }


def test_telegram_onboarding_routes_registered():
    paths = {rt.path for rt in ws.app.routes if "/api/messaging/telegram/onboarding" in getattr(rt, "path", "")}
    assert paths == {
        "/api/messaging/telegram/onboarding/start",
        "/api/messaging/telegram/onboarding/{pairing_id}",
        "/api/messaging/telegram/onboarding/{pairing_id}/apply",
    }


def test_http_routes_still_served():
    """The extracted routers answer through the mounted app."""
    prev_auth = getattr(ws.app.state, "auth_required", None)
    prev_host = getattr(ws.app.state, "bound_host", None)
    ws.app.state.auth_required = False
    ws.app.state.bound_host = None
    try:
        client = TestClient(ws.app)
        client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
        resp = client.get("/api/providers/custom-endpoints")
        assert resp.status_code == 200
        assert "endpoints" in resp.json()
    finally:
        if prev_auth is None:
            delattr(ws.app.state, "auth_required")
        else:
            ws.app.state.auth_required = prev_auth
        if prev_host is None:
            if hasattr(ws.app.state, "bound_host"):
                delattr(ws.app.state, "bound_host")
        else:
            ws.app.state.bound_host = prev_host


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.is_success = ok

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_parse_model_ids_tolerant_of_common_shapes():
    from hermes_cli.web_server import _parse_model_ids

    assert _parse_model_ids(_FakeResp({"data": [{"id": "m1"}, {"id": "m2"}]})) == ["m1", "m2"]
    assert _parse_model_ids(_FakeResp({"data": ["m1", "m2"]})) == ["m1", "m2"]
    assert _parse_model_ids(_FakeResp([{"id": "x"}])) == ["x"]
    assert _parse_model_ids(_FakeResp({"data": []}, ok=False)) == []
    assert _parse_model_ids(_FakeResp({"nope": 1})) == []
    assert _parse_model_ids(_FakeResp(ValueError("bad json"))) == []


def test_custom_endpoint_id_slugifies():
    from hermes_cli.web_server import _custom_endpoint_id

    assert _custom_endpoint_id("My Endpoint!") == "my-endpoint"
    assert _custom_endpoint_id("  ") == "custom"
    assert _custom_endpoint_id("acme") == "acme"


def test_models_from_custom_endpoint_entry_dedupes():
    from hermes_cli.web_server import _models_from_custom_endpoint_entry

    entry = {"models": {"a": {}, "b": {}}, "model": "a"}
    assert _models_from_custom_endpoint_entry(entry) == ["a", "b"]
    assert _models_from_custom_endpoint_entry({"models": ["x", "x"]}) == ["x"]
    assert _models_from_custom_endpoint_entry({}) == []


def test_telegram_normalize_user_id():
    from hermes_cli.web_server import _normalize_telegram_user_id

    assert _normalize_telegram_user_id("123456789") == "123456789"
    assert _normalize_telegram_user_id("  42  ") == "42"
    assert _normalize_telegram_user_id("abc") is None
    assert _normalize_telegram_user_id(None) is None


def test_telegram_parse_expiry_ts():
    from hermes_cli.web_server import _parse_expiry_ts

    ts = _parse_expiry_ts("2027-05-18T00:00:00.000Z")
    assert ts > 1_700_000_000
    assert _parse_expiry_ts("garbage") > 0  # fallback: now + 600s


def test_telegram_onboarding_error_message_maps_codes():
    from hermes_cli.web_server import _telegram_onboarding_error_message

    assert "not found" in _telegram_onboarding_error_message("not_found", "fb").lower()
    assert _telegram_onboarding_error_message("unknown_code", "fb") == "fb"


def test_detach_main_model_only_touches_matching_provider():
    from hermes_cli.web_server import _detach_main_model_from_provider

    cfg = {"model": {"provider": "acme", "base_url": "u", "api_key": "k"}}
    _detach_main_model_from_provider(cfg, "acme")
    assert cfg["model"] == {}

    cfg2 = {"model": {"provider": "other", "base_url": "u"}}
    _detach_main_model_from_provider(cfg2, "acme")
    assert cfg2["model"]["provider"] == "other"
