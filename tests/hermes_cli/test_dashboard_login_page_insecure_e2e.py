"""E2E + exhaustive regression coverage for issue #87788 / PR #87814.

The zero-providers "Sign-in unavailable" page served at ``GET /login``
used to tell operators to restart the dashboard with ``--insecure`` to
bypass the auth gate -- a no-op since the June 2026 hardening
(``hermes_cli/web_server.py``'s ``--insecure no longer bypasses
dashboard authentication`` warning). ``tests/hermes_cli/
test_dashboard_auth_401_reauth.py`` already unit-tests
``render_login_html()`` directly; this file drives the *real* ASGI app
through ``TestClient`` so a future change to routing, middleware, or
the auth-gate wiring can't silently reintroduce the dead advice or
break the populated-providers branch this fix must not touch.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import DashboardAuthProvider, Session
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

pytestmark = pytest.mark.xdist_group(name="dashboard_auth_app_state")


class _StubPasswordProvider(DashboardAuthProvider):
    """Minimal supports_password=True provider — enough to populate the
    login page's provider list. Login flow itself is out of scope here;
    ``test_dashboard_auth_password_login.py`` already covers that."""

    name = "teststub-pw"
    display_name = "Stub Password"
    supports_password = True

    def start_login(self, *, redirect_uri: str):
        raise NotImplementedError

    def complete_login(self, **kwargs):
        raise NotImplementedError

    def complete_password_login(self, *, username: str, password: str) -> Session:
        raise NotImplementedError

    def verify_session(self, *, access_token: str):
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


@pytest.fixture
def bound_client():
    """Real ASGI app bound to a non-loopback host, mirroring a Fly deploy
    where the auth gate always engages."""
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def loopback_client():
    """Real ASGI app bound loopback-style, where the auth gate never
    engages. ``GET /login`` must still render safely with zero providers
    (an operator can reach it directly even though nothing redirects
    there on loopback)."""
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8420
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8420")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


# ---------------------------------------------------------------------------
# Zero providers: the exact regression from #87788.
# ---------------------------------------------------------------------------


class TestZeroProvidersEmptyStateE2E:
    def test_get_login_200_and_no_dead_insecure_advice(self, bound_client):
        resp = bound_client.get("/login")
        assert resp.status_code == 200
        body = resp.text
        assert "Sign-in unavailable" in body
        assert "restart with" not in body
        assert "to bypass the" not in body

    def test_get_login_points_at_both_working_remedies(self, bound_client):
        body = bound_client.get("/login").text
        assert "hermes dashboard register" in body
        assert "dashboard.basic_auth" in body
        assert "127.0.0.1" in body

    def test_get_login_cache_control_no_store(self, bound_client):
        resp = bound_client.get("/login")
        cache_control = resp.headers.get("cache-control", "")
        assert "no-store" in cache_control

    def test_get_login_content_type_html(self, bound_client):
        resp = bound_client.get("/login")
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            '"><img src=x onerror=alert(1)>',
            "javascript:alert(1)",
            "../../etc/passwd",
            "%00%0d%0aSet-Cookie:%20evil=1",
            "hermes-canary-marker-4f2a9c",
        ],
    )
    def test_empty_state_ignores_next_param_injection(self, bound_client, payload):
        # _EMPTY_HTML is a static string returned before next_path is ever
        # touched (see render_login_html) — a malicious ``next=`` must not
        # get reflected into the zero-providers branch at all.
        resp = bound_client.get("/login", params={"next": payload})
        assert resp.status_code == 200
        assert payload not in resp.text
        assert "Sign-in unavailable" in resp.text

    def test_repeated_requests_are_byte_identical(self, bound_client):
        # Guards against any future refactor that makes _EMPTY_HTML
        # mutable / stateful (e.g. accidentally memoizing per-request
        # data into a module-level string).
        first = bound_client.get("/login").text
        for _ in range(25):
            assert bound_client.get("/login").text == first

    def test_loopback_zero_providers_also_safe(self, loopback_client):
        resp = loopback_client.get("/login")
        assert resp.status_code == 200
        assert "Sign-in unavailable" in resp.text
        assert "restart with" not in resp.text


# ---------------------------------------------------------------------------
# Regression guard: populated-providers branch must be untouched by this fix.
# ---------------------------------------------------------------------------


class TestPopulatedProvidersUnaffected:
    def test_oauth_provider_renders_form_not_empty_state(self, bound_client):
        register_provider(StubAuthProvider())
        body = bound_client.get("/login").text
        assert "Sign-in unavailable" not in body
        assert "--insecure" not in body

    def test_password_provider_renders_form_not_empty_state(self, bound_client):
        register_provider(_StubPasswordProvider())
        body = bound_client.get("/login").text
        assert "Sign-in unavailable" not in body
        assert "--insecure" not in body

    def test_mixed_oauth_and_password_providers(self, bound_client):
        register_provider(StubAuthProvider())
        register_provider(_StubPasswordProvider())
        resp = bound_client.get("/login")
        assert resp.status_code == 200
        body = resp.text
        assert "Sign-in unavailable" not in body
        assert "--insecure" not in body


# ---------------------------------------------------------------------------
# Cross-surface consistency: the HTML fix must mirror the CLI fail-closed
# hint (web_server.py) so operators aren't given two different stories.
# ---------------------------------------------------------------------------


class TestFixHintWordingConsistency:
    def test_html_and_cli_hint_share_the_same_two_remedies(self):
        import inspect

        html_src = inspect.getsource(
            __import__(
                "hermes_cli.dashboard_auth.login_page", fromlist=["_EMPTY_HTML"]
            )
        )
        cli_src = inspect.getsource(web_server)

        for phrase in ("hermes dashboard register", "dashboard.basic_auth"):
            assert phrase in html_src, f"{phrase!r} missing from login_page.py"
            assert phrase in cli_src, f"{phrase!r} missing from web_server.py"

    def test_neither_surface_recommends_insecure_as_a_fix(self):
        import inspect

        html_src = inspect.getsource(
            __import__(
                "hermes_cli.dashboard_auth.login_page", fromlist=["_EMPTY_HTML"]
            )
        )
        assert "restart with" not in html_src
        assert "to bypass the" not in html_src
