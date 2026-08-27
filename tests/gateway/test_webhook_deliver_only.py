"""Tests for the webhook adapter's ``deliver_only`` route mode.

``deliver_only`` lets external services (Supabase webhooks, monitoring
alerts, background jobs, other agents) push plain-text notifications to
a user's chat via the webhook adapter WITHOUT invoking the agent.  The
rendered prompt template becomes the literal message body.

Covers:
- Agent is NOT invoked (``handle_message`` never called)
- Rendered content is delivered to the target platform adapter
- HTTP returns 200 OK on success, 502 on delivery failure
- Startup validation rejects ``deliver_only`` without a real delivery target
- HMAC auth, rate limiting, and idempotency still apply
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _wire_mock_target(adapter: WebhookAdapter, platform_name: str = "telegram"):
    """Attach a gateway_runner with a mocked target adapter."""
    mock_target = AsyncMock()
    mock_target.send = AsyncMock(return_value=SendResult(success=True))

    mock_runner = MagicMock()
    mock_runner.adapters = {Platform(platform_name): mock_target}
    mock_runner.config.get_home_channel.return_value = None

    adapter.gateway_runner = mock_runner
    return mock_target


# ===================================================================
# Core behaviour: agent bypass
# ===================================================================

class TestDeliverOnlyBypassesAgent:
    """The whole point of the feature — handle_message must not be called."""

    @pytest.mark.asyncio
    async def test_post_delivers_directly_without_agent(self):
        routes = {
            "match-alert": {
                "secret": _INSECURE_NO_AUTH,
                "deliver": "telegram",
                "deliver_only": True,
                "deliver_extra": {"chat_id": "12345"},
                "prompt": "{payload.user} matched with {payload.other}!",
            }
        }
        adapter = _make_adapter(routes)
        mock_target = _wire_mock_target(adapter)

        # Guard: handle_message must NOT be called in deliver_only mode
        handle_message_calls: list[MessageEvent] = []

        async def _capture(event):
            handle_message_calls.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        body = json.dumps(
            {"payload": {"user": "alice", "other": "bob"}}
        ).encode()

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/match-alert",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": "delivery-1",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "delivered"
            assert data["route"] == "match-alert"
            assert data["target"] == "telegram"

        # Let any background tasks settle before asserting no agent call
        await asyncio.sleep(0.05)

        # Agent was NOT invoked
        assert handle_message_calls == []

        # Target adapter.send() WAS called with the rendered template
        mock_target.send.assert_awaited_once()
        call_args = mock_target.send.await_args
        chat_id_arg, content_arg = call_args.args[0], call_args.args[1]
        assert chat_id_arg == "12345"
        assert content_arg == "alice matched with bob!"


# ===================================================================
# HTTP status codes
# ===================================================================

class TestDeliverOnlyStatusCodes:

    @pytest.mark.asyncio
    async def test_delivery_failure_returns_502(self):
        """If the target adapter returns SendResult(success=False), 502."""
        routes = {
            "r": {
                "secret": _INSECURE_NO_AUTH,
                "deliver": "telegram",
                "deliver_only": True,
                "deliver_extra": {"chat_id": "c-1"},
                "prompt": "hi",
            }
        }
        adapter = _make_adapter(routes)
        mock_target = _wire_mock_target(adapter)
        mock_target.send = AsyncMock(
            return_value=SendResult(success=False, error="rate limited by tg")
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/r",
                json={},
                headers={"X-GitHub-Delivery": "d-fail-1"},
            )
            assert resp.status == 502
            data = await resp.json()
            # Generic error — no adapter-level detail leaks
            assert data["error"] == "Delivery failed"
            assert "rate limited" not in json.dumps(data)


# ===================================================================
# Direct-delivery idempotency
# ===================================================================

class TestDeliverOnlyIdempotency:

    @staticmethod
    def _route():
        return {
            "secret": _INSECURE_NO_AUTH,
            "deliver": "telegram",
            "deliver_only": True,
            "deliver_extra": {"chat_id": "c-1"},
            "prompt": "hello {attempt}",
            "script": "unused-test-script",
        }

    @staticmethod
    async def _post(cli, delivery_id, attempt=1):
        return await cli.post(
            "/webhooks/r",
            json={"attempt": attempt},
            headers={"X-GitHub-Delivery": delivery_id},
        )

    @pytest.mark.asyncio
    async def test_success_commits_id_before_later_duplicate(self):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=lambda _script, payload: (True, payload)
        )

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            first = await self._post(cli, "stable-success")
            duplicate = await self._post(cli, "stable-success")

            assert first.status == 200
            assert (await first.json())["status"] == "delivered"
            assert duplicate.status == 200
            assert (await duplicate.json())["status"] == "duplicate"

        mock_target.send.assert_awaited_once()
        assert adapter._route_processor.run_route_script.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["result", "exception"])
    async def test_failure_releases_id_for_successful_retry(self, failure):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=lambda _script, payload: (True, payload)
        )
        failed = (
            SendResult(success=False, error="private target detail")
            if failure == "result"
            else RuntimeError("private target detail")
        )
        mock_target.send = AsyncMock(
            side_effect=[failed, SendResult(success=True)]
        )

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            first = await self._post(cli, "stable-retry", attempt=1)
            retry = await self._post(cli, "stable-retry", attempt=2)
            duplicate = await self._post(cli, "stable-retry", attempt=3)

            assert first.status == 502
            assert (await first.json())["error"] == "Delivery failed"
            assert retry.status == 200
            assert (await retry.json())["status"] == "delivered"
            assert duplicate.status == 200
            assert (await duplicate.json())["status"] == "duplicate"

        assert mock_target.send.await_count == 2
        assert adapter._route_processor.run_route_script.call_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_same_id_is_retryable_without_processing(self):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=lambda _script, payload: (True, payload)
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_send(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return SendResult(success=True)

        mock_target.send = AsyncMock(side_effect=_blocking_send)

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            first_task = asyncio.create_task(
                self._post(cli, "stable-in-flight")
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            concurrent = await self._post(cli, "stable-in-flight")
            assert concurrent.status == 503
            assert await concurrent.json() == {
                "status": "error",
                "error": "Delivery in progress",
            }
            assert mock_target.send.await_count == 1
            assert adapter._route_processor.run_route_script.call_count == 1

            release.set()
            first = await first_task
            assert first.status == 200
            duplicate = await self._post(cli, "stable-in-flight")
            assert duplicate.status == 200
            assert (await duplicate.json())["status"] == "duplicate"

        assert mock_target.send.await_count == 1
        assert adapter._route_processor.run_route_script.call_count == 1

    @pytest.mark.asyncio
    async def test_failed_in_flight_request_allows_later_retry(self):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=lambda _script, payload: (True, payload)
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _first_fails(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return SendResult(success=False, error="private")

        send_count = 0

        async def _fail_then_succeed(*args, **kwargs):
            nonlocal send_count
            send_count += 1
            if send_count == 1:
                return await _first_fails(*args, **kwargs)
            return SendResult(success=True)

        mock_target.send = AsyncMock(side_effect=_fail_then_succeed)

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            first_task = asyncio.create_task(
                self._post(cli, "stable-in-flight-failure")
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            concurrent = await self._post(cli, "stable-in-flight-failure")
            assert concurrent.status == 503
            release.set()
            assert (await first_task).status == 502
            retry = await self._post(cli, "stable-in-flight-failure")
            assert retry.status == 200
            assert (await retry.json())["status"] == "delivered"

        assert mock_target.send.await_count == 2
        assert adapter._route_processor.run_route_script.call_count == 2

    @pytest.mark.asyncio
    async def test_script_exception_releases_reservation_before_target(self):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[RuntimeError("script failed"), (True, {"attempt": 2})]
        )

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            failed = await self._post(cli, "stable-script-error", attempt=1)
            retry = await self._post(cli, "stable-script-error", attempt=2)

            assert failed.status == 500
            assert retry.status == 200
            assert (await retry.json())["status"] == "delivered"

        mock_target.send.assert_awaited_once()
        assert adapter._route_processor.run_route_script.call_count == 2

    @pytest.mark.asyncio
    async def test_script_ignore_commits_id_and_suppresses_duplicate(self):
        adapter = _make_adapter({"r": self._route()})
        mock_target = _wire_mock_target(adapter)
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(False, None)
        )

        async with TestClient(TestServer(_create_app(adapter))) as cli:
            ignored = await self._post(cli, "stable-ignore")
            duplicate = await self._post(cli, "stable-ignore")

            assert ignored.status == 200
            assert (await ignored.json())["status"] == "ignored"
            assert duplicate.status == 200
            assert (await duplicate.json())["status"] == "duplicate"

        mock_target.send.assert_not_awaited()
        assert adapter._route_processor.run_route_script.call_count == 1


# ===================================================================
# Startup validation
# ===================================================================

class TestDeliverOnlyStartupValidation:


    @pytest.mark.asyncio
    async def test_deliver_only_with_real_target_accepted(self):
        """Sanity check — a valid deliver_only config passes validation."""
        routes = {
            "good": {
                "secret": _INSECURE_NO_AUTH,
                "deliver": "telegram",
                "deliver_only": True,
                "deliver_extra": {"chat_id": "c-1"},
                "prompt": "hi",
            }
        }
        adapter = _make_adapter(routes)
        # connect() does more than validation (binds a socket) — we just
        # want to verify the validation doesn't raise.  Call it and tear
        # down immediately.
        try:
            started = await adapter.connect()
            if started:
                await adapter.disconnect()
        except ValueError:
            pytest.fail("valid deliver_only config should not raise ValueError")


# ===================================================================
# Security + reliability invariants still hold
# ===================================================================

class TestDeliverOnlySecurityInvariants:

    @pytest.mark.asyncio
    async def test_hmac_still_enforced(self):
        """deliver_only does NOT bypass HMAC validation."""
        secret = "real-secret-123"
        routes = {
            "r": {
                "secret": secret,
                "deliver": "telegram",
                "deliver_only": True,
                "deliver_extra": {"chat_id": "c-1"},
                "prompt": "hi",
            }
        }
        adapter = _make_adapter(routes)
        mock_target = _wire_mock_target(adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # No signature header → reject
            resp = await cli.post(
                "/webhooks/r",
                json={},
                headers={"X-GitHub-Delivery": "d-noauth-1"},
            )
            assert resp.status == 401

        # Target never called
        mock_target.send.assert_not_awaited()


# ===================================================================
# Unit: _direct_deliver dispatch
# ===================================================================

class TestDirectDeliverUnit:


    @pytest.mark.asyncio
    async def test_dispatches_to_github_comment(self):
        adapter = _make_adapter({})
        with patch.object(
            adapter, "_deliver_github_comment",
            new=AsyncMock(return_value=SendResult(success=True)),
        ) as mock_gh:
            result = await adapter._direct_deliver(
                "review body",
                {
                    "deliver": "github_comment",
                    "deliver_extra": {"repo": "org/r", "pr_number": "1"},
                },
            )
            assert result.success is True
            mock_gh.assert_awaited_once()
