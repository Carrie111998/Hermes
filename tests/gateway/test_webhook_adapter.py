"""Unit tests for the generic webhook platform adapter.

Covers:
- HMAC signature validation (GitHub, GitLab, generic)
- Prompt rendering with dot-notation template variables
- Event type filtering
- HTTP handler behaviour (404, 202, health)
- Durable replay admission and duplicate delivery identities
- Rate limiting (fixed-window, per route)
- Body size limits
- INSECURE_NO_AUTH bypass
- Session isolation for concurrent webhooks
- Durable final-delivery settlement
- connect / disconnect lifecycle
"""

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    _INSECURE_NO_AUTH,
    check_webhook_requirements,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    routes=None,
    secret="",
    rate_limit=30,
    max_body_bytes=1_048_576,
    host="127.0.0.1",
    port=0,  # let OS pick a free port in tests
):
    """Build a PlatformConfig suitable for WebhookAdapter."""
    extra = {
        "host": host,
        "port": port,
        "routes": routes or {},
        "rate_limit": rate_limit,
        "max_body_bytes": max_body_bytes,
    }
    if secret:
        extra["secret"] = secret
    return PlatformConfig(enabled=True, extra=extra)


def _make_adapter(routes=None, **kwargs):
    """Create a WebhookAdapter with sensible defaults for testing."""
    config = _make_config(routes=routes, **kwargs)
    return WebhookAdapter(config)


def _write_deny_all_toolset_authority(home):
    """Create the profile config that production startup requires for grants."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  webhook: []\n",
        encoding="utf-8",
    )


def _create_app(adapter: WebhookAdapter) -> web.Application:
    """Build the aiohttp Application from the adapter (without starting a full server)."""
    # Mirror connect(): client_max_size enforces the cap on chunked bodies.
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _mock_request(headers=None, body=b"", content_length=None, match_info=None):
    """Build a lightweight mock aiohttp request for non-HTTP tests."""
    req = MagicMock()
    req.headers = headers or {}
    req.content_length = content_length if content_length is not None else len(body)
    req.match_info = match_info or {}
    req.method = "POST"

    async def _read():
        return body

    req.read = _read
    return req


def _github_signature(body: bytes, secret: str) -> str:
    """Compute X-Hub-Signature-256 for *body* using *secret*."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _github_pull_request_body(number: int = 42) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "number": number,
            "pull_request": {
                "id": 701,
                "number": number,
                "state": "open",
                "title": "Authenticated PR",
            },
            "repository": {"id": 801, "full_name": "org/repo"},
            "sender": {"id": 901, "login": "octocat"},
        },
        separators=(",", ":"),
    ).encode()


def _generic_signature(body: bytes, secret: str) -> str:
    """Compute X-Webhook-Signature (plain HMAC-SHA256 hex) for *body*."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _generic_v2_signature(body: bytes, secret: str, timestamp: str) -> str:
    """Compute X-Webhook-Signature-V2 (HMAC-SHA256 of "<timestamp>.<body>")."""
    signed_content = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()


def _svix_signature(body: bytes, secret: str, msg_id: str, timestamp: str) -> str:
    """Compute a Svix v1 signature header for *body* using *secret*."""
    key = (
        base64.b64decode(secret.removeprefix("whsec_"))
        if secret.startswith("whsec_")
        else secret.encode()
    )
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


# ===================================================================
# Signature validation
# ===================================================================


class TestValidateSignature:
    """Tests for WebhookAdapter._validate_signature."""

    def test_validate_no_signature_with_secret_rejects(self):
        """Secret configured but no recognised signature header → reject."""
        adapter = _make_adapter()
        req = _mock_request(headers={})  # no sig headers at all
        assert (
            adapter._validate_signature(req, b"{}", "my-secret", "generic_v2") is False
        )

    def test_non_ascii_signature_headers_reject_without_raising(self):
        """The signature headers are attacker-controlled on a public, unauth
        endpoint. A non-ASCII byte in one must be rejected (False), not crash
        the handler: hmac.compare_digest raises TypeError on a non-ASCII str."""
        adapter = _make_adapter()
        body = b'{"action": "opened"}'
        secret = "webhook-secret-42"
        hostile = "ské-not-a-valid-signature"
        for header, mode in (
            ("X-Hub-Signature-256", "github"),
            ("X-Gitlab-Token", "gitlab"),
            ("X-Webhook-Signature", "generic_v1"),
            ("linear-signature", "linear"),
        ):
            req = _mock_request(headers={header: hostile})
            # Must return False, never raise.
            assert adapter._validate_signature(req, body, secret, mode) is False

    def test_linear_signature_valid_accepts(self):
        """Linear signs the raw body (hex HMAC-SHA256) in linear-signature."""
        adapter = _make_adapter()
        body = b'{"type": "Issue", "data": {"id": "abc"}}'
        secret = "linear-webhook-key"
        sig = _generic_signature(body, secret)  # same math as linear-signature

        req = _mock_request(headers={"linear-signature": sig})

        assert adapter._validate_signature(req, body, secret, "linear") is True

    def test_linear_signature_mismatch_rejects(self):
        """A well-formed linear-signature computed with the wrong key fails closed."""
        adapter = _make_adapter()
        body = b'{"type": "Issue"}'
        sig = _generic_signature(body, "attacker-controlled-key")

        req = _mock_request(headers={"linear-signature": sig})

        assert adapter._validate_signature(req, body, "real-secret", "linear") is False

    def test_non_ascii_svix_signature_rejected(self):
        """The Svix branch also runs its `v1,<sig>` comparison through the
        hardened helper: a valid svix-id + fresh timestamp reaches the compare,
        and a non-ASCII signature must reject rather than raise."""
        adapter = _make_adapter()
        req = _mock_request(
            headers={
                "svix-id": "msg_2xabc",
                "svix-timestamp": str(int(time.time())),  # inside the replay window
                "svix-signature": "v1,ské-not-a-valid-base64-sig",
            }
        )
        assert (
            adapter._validate_signature(req, b'{"x":1}', "shh-secret", "svix") is False
        )

    def test_non_ascii_secret_still_validates_a_matching_token(self):
        """A non-ASCII configured secret must still match its exact GitLab
        token value byte for byte (bytes comparison keeps this working)."""
        adapter = _make_adapter()
        secret = "gl-tökén-välue"
        req = _mock_request(headers={"X-Gitlab-Token": secret})
        assert adapter._validate_signature(req, b"{}", secret, "gitlab") is True

    def test_validate_generic_v2_wrong_timestamp_rejects(self):
        """The timestamp is cryptographically bound into the V2 signature —
        this is the actual fix for the V1 replay hole. An attacker who only
        has a captured (body, signature) pair for V1 (no timestamp binding)
        cannot forge a valid V2 signature for a fresh timestamp without the
        secret, unlike V1 where the signature covers the body alone and a
        forged/fresh timestamp would otherwise sail through unverified."""
        adapter = _make_adapter()
        body = b'{"event": "push"}'
        secret = "generic-secret"
        real_timestamp = str(int(time.time()))
        sig = _generic_v2_signature(body, secret, real_timestamp)
        forged_timestamp = str(int(time.time()) + 1)
        req = _mock_request(
            headers={
                "X-Webhook-Signature-V2": sig,
                "X-Webhook-Timestamp": forged_timestamp,
            }
        )
        assert adapter._validate_signature(req, body, secret, "generic_v2") is False

    def test_validate_generic_v2_stripped_timestamp_does_not_downgrade_to_v1(self):
        """Regression test for a downgrade attack found in review: a sender
        migrating to V2 typically sends BOTH the V1 and V2 signatures
        together (for compatibility while both ends update). If an
        attacker captures one such mixed request and replays it with the
        X-Webhook-Timestamp header stripped, the presence of
        X-Webhook-Signature-V2 must still commit to V2 validation and
        reject — it must NOT silently fall through to validating the
        still-present, still-unprotected V1 signature instead. Falling
        through would let an attacker downgrade a V2-protected request
        back into the signed-freshness gap V2 exists to close, just by
        deleting one header from a captured request."""
        adapter = _make_adapter()
        body = b'{"event": "push"}'
        secret = "generic-secret"
        timestamp = str(int(time.time()))
        v2_sig = _generic_v2_signature(body, secret, timestamp)
        v1_sig = _generic_signature(body, secret)
        # Simulates a captured mixed V1+V2 request replayed with the
        # timestamp header stripped — V1 signature is still valid on its
        # own, but must not be reachable via this path.
        req = _mock_request(
            headers={
                "X-Webhook-Signature-V2": v2_sig,
                "X-Webhook-Signature": v1_sig,
                # X-Webhook-Timestamp deliberately omitted.
            }
        )
        assert adapter._validate_signature(req, body, secret, "generic_v2") is False

    def test_v1_mac_revalidates_later_but_durable_body_fence_is_separate(self):
        """V1 authentication has no clock binding of its own.

        This verifier-only test deliberately does not exercise HTTP admission:
        the durable ledger separately prevents an identical authenticated body
        from executing twice. V2 additionally rejects a captured request whose
        first presentation occurs outside its signed freshness window.
        """
        adapter = _make_adapter()
        body = b'{"event": "push"}'
        secret = "generic-secret"
        sig = _generic_signature(body, secret)
        original_request = _mock_request(headers={"X-Webhook-Signature": sig})
        assert (
            adapter._validate_signature(original_request, body, secret, "generic_v1")
            is True
        )
        # "Time passes" — nothing about a V1 signature depends on time, so
        # a captured pair replayed much later still validates.
        replayed_request = _mock_request(headers={"X-Webhook-Signature": sig})
        assert (
            adapter._validate_signature(replayed_request, body, secret, "generic_v1")
            is True
        )

    def test_validate_svix_signature_raw_secret_valid(self):
        """Raw shared secrets are accepted for Svix-style senders without whsec_ secrets."""
        adapter = _make_adapter()
        body = b'{"event_type":"message.received"}'
        secret = "raw-agentmail-secret"
        msg_id = "msg_123"
        timestamp = str(int(time.time()))
        sig = _svix_signature(body, secret, msg_id, timestamp)
        req = _mock_request(
            headers={
                "svix-id": msg_id,
                "svix-timestamp": timestamp,
                "svix-signature": sig,
            }
        )
        assert adapter._validate_signature(req, body, secret, "svix") is True


# ===================================================================
# Prompt rendering
# ===================================================================


class TestRenderPrompt:
    """Tests for WebhookAdapter._render_prompt."""

    def test_render_prompt_dot_notation(self):
        """Dot-notation {pull_request.title} resolves nested keys."""
        adapter = _make_adapter()
        payload = {"pull_request": {"title": "Fix bug", "number": 42}}
        result = adapter._render_prompt(
            "PR #{pull_request.number}: {pull_request.title}",
            payload,
            "pull_request",
            "github",
        )
        assert result == "PR #42: Fix bug"


# ===================================================================
# Delivery extra rendering
# ===================================================================


class TestRenderDeliveryExtra:
    def test_render_delivery_extra_templates(self):
        """String values in deliver_extra are rendered with payload data."""
        adapter = _make_adapter()
        extra = {
            "repo": "{repository.full_name}",
            "pr_number": "{number}",
            "static": 42,
        }
        payload = {"repository": {"full_name": "org/repo"}, "number": 7}
        result = adapter._render_delivery_extra(extra, payload)
        assert result["repo"] == "org/repo"
        assert result["pr_number"] == "7"
        assert result["static"] == 42  # non-string left as-is


# ===================================================================
# Event filtering
# ===================================================================


class TestEventFilter:
    """Tests for event type filtering in _handle_webhook."""

    @pytest.mark.asyncio
    async def test_event_filter_accepts_matching(self):
        """Matching event type passes through."""
        routes = {
            "gh": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "events": ["pull_request"],
                "prompt": "PR: {action}",
            }
        }
        adapter = _make_adapter(routes=routes)
        # Stub handle_message to avoid running the agent
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/gh",
                json={"action": "opened"},
                headers={"X-GitHub-Event": "pull_request"},
            )
            assert resp.status == 202


# ===================================================================
# Payload filters
# ===================================================================


class TestPayloadFilters:
    """Tests for route-level payload filters in _handle_webhook."""

    @pytest.mark.asyncio
    async def test_filter_accepts_nested_any_and_in_file(self, tmp_path, monkeypatch):
        """Nested any groups can match dynamic watchlists under HERMES_HOME."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        watchlist = tmp_path / "data" / "watchlist.json"
        watchlist.parent.mkdir()
        watchlist.write_text(json.dumps(["chat-1", "chat-2"]), encoding="utf-8")
        routes = {
            "waha": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "filters": [
                    {"field": "payload.fromMe", "equals": False},
                    {
                        "any": [
                            {
                                "field": "payload.chatId",
                                "in_file": "~/.hermes/data/watchlist.json",
                            },
                            {
                                "field": "payload.id.remote",
                                "in_file": "~/.hermes/data/watchlist.json",
                            },
                        ]
                    },
                ],
                "prompt": "Message from {payload.chatId}: {payload.body}",
            }
        }
        adapter = _make_adapter(routes=routes)
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/waha",
                json={
                    "payload": {
                        "fromMe": False,
                        "chatId": "chat-2",
                        "body": "hello",
                    }
                },
                headers={"X-GitHub-Delivery": "filter-match-1"},
            )
            assert resp.status == 202

        await asyncio.sleep(0.05)
        assert len(captured) == 1
        assert captured[0].text == "Message from chat-2: hello"

    @pytest.mark.asyncio
    async def test_script_transforms_payload_before_prompt_rendering(
        self, tmp_path, monkeypatch
    ):
        """A script can replace the payload used by prompt templates."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        script = scripts / "todoist_filter.py"
        script.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "payload['body'] = payload['task']['content'].upper()\n"
            "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        routes = {
            "todoist": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "script": "todoist_filter.py",
                "prompt": "Task: {body}",
            }
        }
        adapter = _make_adapter(routes=routes)
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/todoist",
                json={"task": {"content": "pay bills"}},
                headers={"X-GitHub-Delivery": "script-transform-1"},
            )
            assert resp.status == 202

        await asyncio.sleep(0.05)
        assert captured[0].text == "Task: PAY BILLS"
        assert captured[0].raw_message["body"] == "PAY BILLS"


# ===================================================================
# HTTP handling
# ===================================================================


class TestHTTPHandling:
    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self):
        """POST to an unknown route returns 404."""
        adapter = _make_adapter(
            routes={
                "real": {
                    "secret": _INSECURE_NO_AUTH,
                    "provider": "generic",
                    "prompt": "x",
                }
            }
        )
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/nonexistent", json={"a": 1})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_route_without_secret_is_route_misconfiguration(self):
        """A missing HMAC secret fails at route publication, before authentication."""
        routes = {"test": {"provider": "generic", "prompt": "hi"}}
        adapter = _make_adapter(routes=routes, secret="")
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/test", json={"data": "value"})
            assert resp.status == 500
            data = await resp.json()
            assert data == {
                "status": "failed",
                "error": "Webhook route is misconfigured",
            }

        adapter.handle_message.assert_not_called()


# ===================================================================
# Durable replay admission
# ===================================================================


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_in_flight_delivery_id_returns_202(self):
        """A retry cannot turn an in-flight operation into duplicate success."""
        routes = {
            "idem": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "test",
            }
        }
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {"X-GitHub-Delivery": "delivery-123"}
            resp1 = await cli.post("/webhooks/idem", json={"a": 1}, headers=headers)
            assert resp1.status == 202

            resp2 = await cli.post("/webhooks/idem", json={"a": 1}, headers=headers)
            assert resp2.status == 202
            data = await resp2.json()
            assert data["status"] == "in_progress"


# ===================================================================
# Rate limiting
# ===================================================================


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_rejects_excess(self):
        """Exceeding the rate limit returns 429."""
        routes = {
            "limited": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "test",
            }
        }
        adapter = _make_adapter(routes=routes, rate_limit=2)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # Two requests within limit
            for i in range(2):
                resp = await cli.post(
                    "/webhooks/limited",
                    json={"n": i},
                    headers={"X-GitHub-Delivery": f"d-{i}"},
                )
                assert resp.status == 202, f"Request {i} should be accepted"

            # Third request should be rate-limited
            resp = await cli.post(
                "/webhooks/limited",
                json={"n": 99},
                headers={"X-GitHub-Delivery": "d-99"},
            )
            assert resp.status == 429


# ===================================================================
# Body size limit
# ===================================================================


class TestBodySize:
    @pytest.mark.asyncio
    async def test_oversized_payload_rejected(self):
        """Content-Length > max_body_bytes returns 413."""
        routes = {
            "big": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "test",
            }
        }
        adapter = _make_adapter(routes=routes, max_body_bytes=100)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            large_payload = {"data": "x" * 200}
            resp = await cli.post(
                "/webhooks/big",
                json=large_payload,
                headers={"Content-Length": "999999"},
            )
            assert resp.status == 413


# ===================================================================
# INSECURE_NO_AUTH
# ===================================================================


class TestInsecureNoAuth:
    @pytest.mark.asyncio
    async def test_insecure_no_auth_skips_validation(self):
        """Setting secret to _INSECURE_NO_AUTH bypasses signature check."""
        routes = {
            "open": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "hello",
            }
        }
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # No signature header at all — should still be accepted
            resp = await cli.post("/webhooks/open", json={"test": True})
            assert resp.status == 202


# ===================================================================
# Session isolation
# ===================================================================


class TestSessionIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_webhooks_get_independent_sessions(self):
        """Two events on the same route produce different session keys."""
        routes = {
            "ci": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "build",
            }
        }
        adapter = _make_adapter(routes=routes)

        captured_events = []

        async def _capture(event):
            captured_events.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp1 = await cli.post(
                "/webhooks/ci",
                json={"ref": "main"},
                headers={"X-GitHub-Delivery": "aaa-111"},
            )
            assert resp1.status == 202

            resp2 = await cli.post(
                "/webhooks/ci",
                json={"ref": "dev"},
                headers={"X-GitHub-Delivery": "bbb-222"},
            )
            assert resp2.status == 202

        # Wait for the async tasks to be created
        await asyncio.sleep(0.05)

        assert len(captured_events) == 2
        ids = {ev.source.chat_id for ev in captured_events}
        assert len(ids) == 2, "Each delivery must have a unique session chat_id"


# ===================================================================
# Silence-marker suppression
# ===================================================================


class TestWebhookSilenceSuppression:
    """A webhook route that answers ``[SILENT]`` must deliver nothing.

    Webhook routes are autonomous lanes with nobody waiting on the other end,
    so a subscription prompt tells the agent to reply ``[SILENT]`` on a tick
    that produced no story.  Models routinely append a sentence saying WHY they
    stayed quiet, and the live gateway's exact-whole-response rule then treats
    that as a real report — which is how a Helper support lane ended up
    repeatedly messaging its owner to say it had nothing to say.
    """

    async def _adapter_with_mock_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_deny_all_toolset_authority(tmp_path)
        adapter = _make_adapter(
            routes={
                "helper-events": {
                    "secret": _INSECURE_NO_AUTH,
                    "provider": "github",
                    "prompt": "Tick: {message}",
                    "deliver": "telegram",
                    "deliver_extra": {"chat_id": "-100123"},
                }
            }
        )
        mock_target = AsyncMock()
        mock_target.send = AsyncMock(return_value=SendResult(success=True))
        mock_target.config = PlatformConfig(enabled=True)
        mock_runner = MagicMock()
        mock_runner.adapters = {
            Platform.WEBHOOK: adapter,
            Platform("telegram"): mock_target,
        }
        mock_runner.config.get_home_channel.return_value = None
        mock_runner._authorization_adapter = None
        adapter.gateway_runner = mock_runner
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            response = await cli.post(
                "/webhooks/helper-events",
                json={"message": "nothing new"},
                headers={"X-GitHub-Delivery": "silence-1"},
            )
            assert response.status == 202
        await asyncio.sleep(0.01)
        event = adapter.handle_message.await_args.args[0]
        await adapter.on_processing_start(event)
        return adapter, mock_target, event.source.chat_id

    @pytest.mark.asyncio
    async def test_bare_marker_is_not_delivered(self, tmp_path, monkeypatch):
        adapter, target, chat_id = await self._adapter_with_mock_target(
            tmp_path,
            monkeypatch,
        )

        result = await adapter.send(chat_id, "[SILENT]", metadata={"notify": True})

        assert result.success is True
        target.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marker_followed_by_prose_is_not_delivered(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The regression this suppression exists for.

        The agent explains its own silence on the lines after the marker.  The
        strict interactive rule reads that as substantive prose and delivers the
        whole thing, marker included.
        """
        adapter, target, chat_id = await self._adapter_with_mock_target(
            tmp_path,
            monkeypatch,
        )

        result = await adapter.send(
            chat_id,
            "[SILENT]\n\nThe new inbound was the same email quoted back a second "
            "time, on a ticket we already answered. Nothing new to reply to, so I "
            "closed it; it reopens by itself if they write back.",
            metadata={"notify": True},
        )

        assert result.success is True
        target.send.assert_not_awaited()


# ===================================================================
# Delivery info cleanup
# ===================================================================


class TestDeliveryCleanup:
    @pytest.mark.asyncio
    async def test_missing_delivery_authority_never_falls_back_to_log_success(self):
        adapter = _make_adapter()
        result = await adapter.send(
            "webhook:default:events:github:missing",
            "Final agent response",
        )
        assert result.success is False
        assert result.error == "Missing admitted webhook delivery authority"

    @pytest.mark.asyncio
    async def test_interim_send_cannot_consume_final_delivery_authority(self):
        """Only an explicitly marked final response may stage the exact effect."""
        adapter = _make_adapter(
            routes={
                "test": {
                    "secret": _INSECURE_NO_AUTH,
                    "provider": "github",
                    "prompt": "{message}",
                    "deliver": "log",
                }
            }
        )
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_create_app(adapter))) as cli:
            response = await cli.post(
                "/webhooks/test",
                json={"message": "run"},
                headers={"X-GitHub-Delivery": "delivery-authority-1"},
            )
            assert response.status == 202
        await asyncio.sleep(0.01)
        event = adapter.handle_message.await_args.args[0]
        chat_id = event.source.chat_id

        admitted = adapter._operation_ledger.lookup_session(chat_id)
        assert admitted is not None
        assert admitted.delivery is None

        # An interim status is acknowledged but cannot stage or consume the target.
        result1 = await adapter.send(
            chat_id,
            "Status: switching to fallback",
            metadata={"notify": True, "_interim_send": True},
        )
        assert result1.success is True
        after_interim = adapter._operation_ledger.lookup_session(chat_id)
        assert after_interim is not None
        assert after_interim.delivery is None

        # The marked final response is durably staged and settled exactly once.
        await adapter.on_processing_start(event)
        result2 = await adapter.send(
            chat_id,
            "Final agent response",
            metadata={"notify": True},
        )
        assert result2.success is True
        settled = adapter._operation_ledger.lookup_session(chat_id)
        assert settled is not None
        assert settled.delivery is not None
        assert settled.delivery.content == "Final agent response"

        # An exact repeat reads the cached settlement; contradictory content fails.
        repeat = await adapter.send(
            chat_id,
            "Final agent response",
            metadata={"notify": True},
        )
        assert repeat.success is True
        conflict = await adapter.send(
            chat_id,
            "Different final response",
            metadata={"notify": True},
        )
        assert conflict.success is False


# ===================================================================
# check_webhook_requirements
# ===================================================================


class TestCheckRequirements:
    @patch("gateway.platforms.webhook.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self):
        assert check_webhook_requirements() is False


# ===================================================================
# __raw__ template token
# ===================================================================


class TestRawTemplateToken:
    """Tests for the {__raw__} special token in _render_prompt."""

    def test_raw_mixed_with_other_variables(self):
        """{__raw__} can be mixed with regular template variables."""
        adapter = _make_adapter()
        payload = {"action": "closed", "number": 7}
        result = adapter._render_prompt(
            "Action={action} Raw={__raw__}", payload, "push", "test"
        )
        assert result.startswith("Action=closed Raw=")
        envelope = json.loads(result.split(" Raw=", 1)[1])
        assert envelope["truncated"] is False
        assert '"action": "closed"' in envelope["payload"]
        assert '"number": 7' in envelope["payload"]


# ===================================================================
# Cross-platform delivery thread_id passthrough
# ===================================================================


class TestDeliverCrossPlatformThreadId:
    """Tests for thread_id passthrough through the durable target gate."""

    def _setup_adapter_with_mock_target(self, tmp_path, monkeypatch):
        """Set up a webhook adapter with a mocked gateway_runner and target adapter."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_deny_all_toolset_authority(tmp_path)
        adapter = _make_adapter()
        mock_target = AsyncMock()
        mock_target.send = AsyncMock(return_value=SendResult(success=True))
        mock_target.config = PlatformConfig(enabled=True)

        mock_runner = MagicMock()
        mock_runner.adapters = {
            Platform.WEBHOOK: adapter,
            Platform("telegram"): mock_target,
        }
        mock_runner.config.get_home_channel.return_value = None
        mock_runner._authorization_adapter = None

        adapter.gateway_runner = mock_runner
        return adapter, mock_target

    @pytest.mark.asyncio
    async def test_thread_id_passed_as_metadata(self, tmp_path, monkeypatch):
        """thread_id from deliver_extra is passed as metadata to adapter.send()."""
        adapter, mock_target = self._setup_adapter_with_mock_target(
            tmp_path,
            monkeypatch,
        )
        adapter._static_routes = {
            "threaded": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "{message}",
                "deliver": "telegram",
                "deliver_extra": {
                    "chat_id": "12345",
                    "thread_id": "999",
                },
            }
        }
        adapter._routes = dict(adapter._static_routes)
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_create_app(adapter))) as cli:
            response = await cli.post(
                "/webhooks/threaded",
                json={"message": "run"},
                headers={"X-GitHub-Delivery": "thread-1"},
            )
            assert response.status == 202
        await asyncio.sleep(0.01)
        event = adapter.handle_message.await_args.args[0]
        await adapter.on_processing_start(event)

        result = await adapter.send(
            event.source.chat_id,
            "hello",
            metadata={"notify": True},
        )
        assert result.success is True
        mock_target.send.assert_awaited_once_with(
            "12345", "hello", metadata={"thread_id": "999"}
        )


class TestInsecureNoAuthSafetyRail:
    """connect() refuses to start when INSECURE_NO_AUTH is combined with a
    non-loopback bind. Guards against accidentally exposing an unauthenticated
    webhook endpoint on a public interface."""

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost"],
    )
    @pytest.mark.asyncio
    async def test_connect_allows_insecure_no_auth_on_loopback(self, host):
        """Recognised loopback hosts are permitted with INSECURE_NO_AUTH."""
        routes = {
            "r1": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "x",
            }
        }
        adapter = _make_adapter(routes=routes, host=host, port=0)
        try:
            with patch.object(adapter, "_reload_dynamic_routes"):
                result = await adapter.connect()
            assert result is True
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_connect_allows_real_secret_on_public_bind(self):
        """A real HMAC secret bound to 0.0.0.0 is the normal production case."""
        routes = {
            "r1": {
                "secret": "real-secret-abc123",
                "provider": "generic",
                "prompt": "x",
            }
        }
        adapter = _make_adapter(routes=routes, host="0.0.0.0", port=0)
        try:
            with patch.object(adapter, "_reload_dynamic_routes"):
                result = await adapter.connect()
            assert result is True
        finally:
            await adapter.disconnect()


class TestDualStackBind:
    """The default bind host must serve BOTH IPv4 and IPv6.

    Regression guard for the hosted-agent webhook reachability bug: Fly.io 6PN
    (the private network the edge router reverse-proxies webhook traffic over)
    is IPv6-only — an agent's ``<app>.internal`` name resolves to an ``fdaa:…``
    address. The adapter used to default to ``host="0.0.0.0"`` (IPv4 only), so
    the router's dial to ``<app>.internal:8644`` hit an address nothing was
    listening on → connection refused → public webhooks unreachable.

    The fix is ``DEFAULT_HOST = None`` (dual-stack). ``"::"`` is NOT a valid
    substitute: on hosts with the ``bindv6only`` sysctl set (verified on Fly
    machines) it yields an IPv6-ONLY socket, which would then break the IPv4
    loopback health check and the AF_INET port-conflict probe.
    """

    def test_missing_host_key_resolves_to_none(self):
        """Config with no host key → dual-stack (None), not a literal string."""
        cfg = PlatformConfig(enabled=True, extra={"port": 0, "routes": {}})
        adapter = WebhookAdapter(cfg)
        assert adapter._host is None

    @pytest.mark.asyncio
    async def test_default_bind_serves_both_families(self):
        """Binding the real server with the default host opens v4 AND v6 sockets.

        This is the behavioural proof: with host=None, asyncio.create_server
        opens a listening socket per resolved family, so both 127.0.0.1 (v4)
        and ::1 (v6) are reachable — exactly what 6PN needs. Uses a real bind
        on an OS-assigned port (no mock) and inspects the runner's addresses.
        """
        # Build config WITHOUT a host key so the real DEFAULT_HOST (None)
        # applies — _make_adapter's helper injects a loopback host by default,
        # which would mask the dual-stack default under test here.
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "port": 0,
                "routes": {
                    "r1": {
                        "secret": "real-secret-abc123",
                        "provider": "generic",
                        "prompt": "x",
                    }
                },
            },
        )
        adapter = WebhookAdapter(cfg)
        assert adapter._host is None
        try:
            with patch.object(adapter, "_reload_dynamic_routes"):
                result = await adapter.connect()
            assert result is True
            # runner.addresses lists one bound address per listening socket.
            # An IPv6 sockaddr is a 4-tuple (host, port, flowinfo, scopeid);
            # an IPv4 sockaddr is a 2-tuple (host, port). With the dual-stack
            # default we expect BOTH — that is precisely what makes the adapter
            # reachable over 6PN (v6) AND on the loopback health check (v4).
            addrs = list(adapter._runner.addresses)  # type: ignore[union-attr]
            has_v6 = any(len(a) == 4 for a in addrs)
            has_v4 = any(len(a) == 2 for a in addrs)
            assert has_v4, f"IPv4 bind missing — got {addrs}"
            assert has_v6, f"IPv6 bind missing (the 6PN reachability bug) — got {addrs}"
        finally:
            await adapter.disconnect()


# Regression coverage for #72041: profile-bound webhook authentication
class TestMultiplexProfileWebhookAuthentication:
    @staticmethod
    def _configure_profiles(adapter, tmp_path, monkeypatch):
        worker_home = tmp_path / "profiles" / "worker"
        other_home = tmp_path / "profiles" / "other"
        _write_deny_all_toolset_authority(worker_home)
        _write_deny_all_toolset_authority(other_home)
        runner = MagicMock()
        runner.config.multiplex_profiles = True
        runner._authorization_adapter = None
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._profile_adapters = {}
        runner._resolve_profile_home_for_source = lambda source: (
            tmp_path / "profiles" / str(source.profile)
        )
        adapter.gateway_runner = runner
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, profile_allowlist=None: [
                ("default", tmp_path),
                ("worker", worker_home),
                ("other", other_home),
            ],
        )

    @staticmethod
    def _app(adapter):
        app = _create_app(adapter)
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}",
            adapter._handle_webhook,
        )
        return app

    @staticmethod
    def _headers(body: bytes, secret: str):
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _github_signature(body, secret),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "profile-bound-1",
        }

    @pytest.mark.asyncio
    async def test_route_secret_is_bound_to_named_profile(self, tmp_path, monkeypatch):
        route_secret = "worker-route-secret-abc123"
        adapter = _make_adapter(
            routes={
                "gh": {
                    "profile": "worker",
                    "secret": route_secret,
                    "provider": "github",
                    "events": ["pull_request"],
                    "prompt": "PR: {action}",
                }
            },
            host="127.0.0.1",
        )
        self._configure_profiles(adapter, tmp_path, monkeypatch)
        adapter.handle_message = AsyncMock()
        body = _github_pull_request_body()
        headers = self._headers(body, route_secret)

        async with TestClient(TestServer(self._app(adapter))) as cli:
            accepted = await cli.post(
                "/p/worker/webhooks/gh",
                data=body,
                headers=headers,
            )
            assert accepted.status == 202
            assert (await accepted.json())["status"] == "accepted"

            wrong_profile = await cli.post(
                "/p/other/webhooks/gh",
                data=body,
                headers=headers,
            )
            assert wrong_profile.status == 404

            default_profile = await cli.post(
                "/webhooks/gh",
                data=body,
                headers=headers,
            )
            assert default_profile.status == 404

    @pytest.mark.asyncio
    async def test_single_profile_gateway_accepts_its_self_referential_prefix(
        self,
        monkeypatch,
    ):
        secret = "worker-route-secret-abc123"
        adapter = _make_adapter(
            routes={
                "gh": {
                    "profile": "worker",
                    "secret": secret,
                    "provider": "github",
                    "events": ["pull_request"],
                }
            },
            host="127.0.0.1",
        )
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={Platform.WEBHOOK: adapter},
        )
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home",
            lambda profile: profile == "worker",
        )
        body = _github_pull_request_body()

        async with TestClient(TestServer(self._app(adapter))) as client:
            response = await client.post(
                "/p/worker/webhooks/gh",
                data=body,
                headers=self._headers(body, secret),
            )
            response_body = await response.json()

        assert response.status == 202
        assert response_body["status"] == "accepted"
        adapter.handle_message.assert_awaited_once()


def test_route_profile_validation_fails_closed():
    assert WebhookAdapter._route_allows_profile({"provider": "generic"}, None) is True
    assert (
        WebhookAdapter._route_allows_profile(
            {"profile": "worker", "provider": "generic"}, "worker"
        )
        is True
    )
    assert (
        WebhookAdapter._route_allows_profile(
            {"profile": "worker", "provider": "generic"}, "other"
        )
        is False
    )
    for malformed in (None, "", "   ", 123, ["worker"]):
        assert (
            WebhookAdapter._route_allows_profile(
                {"profile": malformed, "provider": "generic"}, "worker"
            )
            is False
        )
