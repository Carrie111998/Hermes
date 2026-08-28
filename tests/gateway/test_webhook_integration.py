"""Integration tests for the generic webhook platform adapter.

These tests exercise end-to-end flows through the webhook adapter:
1. GitHub PR webhook → agent MessageEvent created
2. Skills config injects skill content into the prompt
3. Cross-platform delivery routes to a mock Telegram adapter
4. GitHub comment delivery invokes ``gh`` CLI (mocked subprocess)
"""

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    """Create a WebhookAdapter with the given routes."""
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    """Build the aiohttp Application from the adapter."""
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _github_signature(body: bytes, secret: str) -> str:
    """Compute X-Hub-Signature-256 for *body* using *secret*."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# A realistic GitHub pull_request event payload (trimmed)
GITHUB_PR_PAYLOAD = {
    "action": "opened",
    "number": 42,
    "pull_request": {
        "id": 701,
        "number": 42,
        "state": "open",
        "title": "Add webhook adapter",
        "body": "This PR adds a generic webhook platform adapter.",
        "html_url": "https://github.com/org/repo/pull/42",
        "user": {"login": "contributor"},
        "head": {"ref": "feature/webhooks"},
        "base": {"ref": "main"},
    },
    "repository": {
        "full_name": "org/repo",
        "html_url": "https://github.com/org/repo",
    },
    "sender": {"login": "contributor"},
}


# ===================================================================
# Test 1: GitHub PR webhook triggers agent
# ===================================================================


class TestGitHubPRWebhook:
    @pytest.mark.asyncio
    async def test_github_pr_webhook_triggers_agent(self):
        """POST with a realistic GitHub PR payload should:
        1. Return 202 Accepted
        2. Call handle_message with a MessageEvent
        3. The event text contains the rendered prompt
        4. The event source has chat_type 'webhook'
        """
        secret = "gh-webhook-test-secret"
        routes = {
            "github-pr": {
                "secret": secret,
                "provider": "github",
                "events": ["pull_request"],
                "prompt": (
                    "Review PR #{number} by {sender.login}: "
                    "{pull_request.title}\n\n{pull_request.body}"
                ),
                "deliver": "log",
            }
        }
        adapter = _make_adapter(routes)

        captured_events: list[MessageEvent] = []

        async def _capture(event: MessageEvent):
            captured_events.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        body = json.dumps(GITHUB_PR_PAYLOAD).encode()
        sig = _github_signature(body, secret)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/github-pr",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Delivery": "gh-delivery-001",
                },
            )
            assert resp.status == 202
            data = await resp.json()
            assert data["status"] == "accepted"
            assert data["route"] == "github-pr"
            assert data["event"] == "pull_request"
            # GitHub's delivery header is not covered by the body MAC, so it
            # remains diagnostic-only and cannot become execution authority.
            delivery_id = data["delivery_id"]
            assert delivery_id != "gh-delivery-001"
            assert len(delivery_id) == 32

        # Let the asyncio.create_task fire
        await asyncio.sleep(0.05)

        assert len(captured_events) == 1
        event = captured_events[0]
        assert "Review PR #42 by contributor" in event.text
        assert "Add webhook adapter" in event.text
        assert event.source.chat_type == "webhook"
        assert event.source.platform == Platform.WEBHOOK
        assert "github-pr" in event.source.chat_id
        assert event.message_id == delivery_id


# ===================================================================
# Test 2: Skills injected into prompt
# ===================================================================


class TestSkillsInjection:
    @pytest.mark.asyncio
    async def test_skills_injected_into_prompt(self):
        """When a route has skills: [code-review], the adapter should
        call build_skill_invocation_message() and use its output as the
        prompt instead of the raw template render."""
        routes = {
            "pr-review": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "events": ["pull_request"],
                "prompt": "Review this PR: {pull_request.title}",
                "skills": ["code-review"],
            }
        }
        adapter = _make_adapter(routes)

        captured_events: list[MessageEvent] = []

        async def _capture(event: MessageEvent):
            captured_events.append(event)

        adapter.handle_message = _capture

        skill_content = (
            "You are a code reviewer. Review the following:\n"
            "Review this PR: Add webhook adapter"
        )

        # The imports are lazy (inside the handler), so patch the source module
        with (
            patch(
                "agent.skill_commands.build_skill_invocation_message",
                return_value=skill_content,
            ) as mock_build,
            patch(
                "agent.skill_commands.get_skill_commands",
                return_value={"/code-review": {"name": "code-review"}},
            ),
        ):
            app = _create_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/pr-review",
                    json=GITHUB_PR_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "skill-test-001",
                    },
                )
                assert resp.status == 202

            await asyncio.sleep(0.05)

            assert len(captured_events) == 1
            event = captured_events[0]
            # The prompt should be the skill content, not the raw template
            assert "You are a code reviewer" in event.text
            mock_build.assert_called_once()


# ===================================================================
# Test 3: Cross-platform delivery (webhook → Telegram)
# ===================================================================


class TestCrossPlatformDelivery:
    @pytest.mark.asyncio
    async def test_cross_platform_delivery(self):
        """When deliver='telegram', the response is routed to the
        Telegram adapter via gateway_runner.adapters."""
        routes = {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "Alert: {message}",
                "deliver": "telegram",
                "deliver_extra": {"chat_id": "12345"},
            }
        }
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()

        # Set up a mock gateway runner with a mock Telegram adapter
        mock_tg_adapter = AsyncMock()
        mock_tg_adapter.send = AsyncMock(return_value=SendResult(success=True))
        mock_tg_adapter.config = PlatformConfig(enabled=True, token="fake")

        mock_runner = MagicMock()
        mock_runner.adapters = {
            Platform.WEBHOOK: adapter,
            Platform.TELEGRAM: mock_tg_adapter,
        }
        mock_runner.config = GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}
        )
        mock_runner._authorization_adapter = None
        # MagicMock otherwise fabricates a callable profile-config resolver,
        # making this narrow delivery double look like a full GatewayRunner.
        mock_runner._resolve_profile_home_for_source = None
        adapter.gateway_runner = mock_runner

        # First, admit and durably prepare the exact target authority.
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/alerts",
                json={"message": "Server is on fire!"},
                headers={"X-GitHub-Delivery": "alert-001"},
            )
            assert resp.status == 202

        # Delivery target lookup is keyed by the execution trace, not the
        # provider-controlled retry ID.
        await asyncio.sleep(0.05)
        event = adapter.handle_message.await_args.args[0]
        chat_id = event.source.chat_id
        assert chat_id.startswith("webhook:default:alerts:github:")
        assert "alert-001" not in chat_id
        assert event.webhook_authority.target_snapshot == {
            "v": 1,
            "kind": "platform",
            "profile": "default",
            "platform": "telegram",
            "chat_id": "12345",
        }

        # Now call send() as if the agent has finished
        await adapter.on_processing_start(event)
        result = await adapter.send(
            chat_id,
            "I've acknowledged the alert.",
            metadata={"notify": True},
        )

        assert result.success is True
        mock_tg_adapter.send.assert_awaited_once_with(
            "12345", "I've acknowledged the alert.", metadata=None
        )
        settled = adapter._operation_ledger.lookup_session(chat_id)
        assert settled is not None
        assert settled.delivery is not None
        assert settled.delivery.content == "I've acknowledged the alert."


# ===================================================================
# Test 4: GitHub comment delivery via gh CLI
# ===================================================================


class TestGitHubCommentDelivery:
    @pytest.mark.asyncio
    async def test_github_comment_delivery(self):
        """When deliver='github_comment', the adapter invokes
        ``gh pr comment`` via subprocess.run (mocked)."""
        routes = {
            "pr-bot": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "prompt": "Review: {pull_request.title}",
                "deliver": "github_comment",
                "deliver_extra": {
                    "repo": "{repository.full_name}",
                    "pr_number": "{number}",
                },
            }
        }
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()

        # Mock the exact executable resolution and subprocess target mutation.
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Comment posted"
        mock_result.stderr = ""

        with (
            patch(
                "gateway.platforms.webhook_route_authority.shutil.which",
                return_value="/usr/bin/gh",
            ),
            patch(
                "gateway.platforms.webhook_delivery.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            app = _create_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/pr-bot",
                    json=GITHUB_PR_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "gh-comment-001",
                    },
                )
                assert resp.status == 202

            await asyncio.sleep(0.05)
            event = adapter.handle_message.await_args.args[0]
            chat_id = event.source.chat_id
            assert chat_id.startswith("webhook:default:pr-bot:github:")
            assert "gh-comment-001" not in chat_id
            assert event.webhook_authority.target_snapshot == {
                "v": 1,
                "kind": "github_comment",
                "profile": "default",
                "repo": "org/repo",
                "pr_number": 42,
            }

            await adapter.on_processing_start(event)
            result = await adapter.send(
                chat_id,
                "LGTM! The code looks great.",
                metadata={"notify": True},
            )

        assert result.success is True
        mock_run.assert_called_once()
        command = mock_run.call_args.args[0]
        assert command == [
            "/usr/bin/gh",
            "pr",
            "comment",
            "42",
            "--repo",
            "org/repo",
            "--body",
            "LGTM! The code looks great.",
        ]
        kwargs = mock_run.call_args.kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 30
        assert kwargs["check"] is False
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        settled = adapter._operation_ledger.lookup_session(chat_id)
        assert settled is not None
        assert settled.delivery is not None
