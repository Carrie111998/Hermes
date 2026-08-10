"""Integration tests for the generic webhook platform adapter.

These tests exercise end-to-end flows through the webhook adapter:
1. GitHub PR webhook → agent MessageEvent created
2. Skills config injects skill content into the prompt
3. Cross-platform delivery routes to a mock Telegram adapter
4. GitHub comment delivery invokes ``gh`` CLI (mocked subprocess)
"""

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.platforms.webhook_filters import WebhookRouteProcessor
from hermes_cli.tools_config import _get_platform_tools
from model_tools import _clear_tool_defs_cache, get_tool_definitions
from tools.github_pr_evidence import EvidenceScope, evidence_scope


def test_trusted_github_pr_script_receives_only_its_reviewed_control_plane_env(
    monkeypatch,
):
    allowed = {
        "NEWTONSAPPLE_REVIEW_BOT_LOGIN": "newtonsapple-bot",
        "NEWTONSAPPLE_REVIEW_ATTESTATION_PRIVATE_KEY": "signer",
        "GH_CONFIG_DIR": "/trusted/bot-gh",
        "BUZZ_RELAY_URL": "wss://relay.example",
        "BUZZ_PRIVATE_KEY": "buzz-key",
        "BUZZ_AUTH_TAG": "buzz-auth",
    }
    for key, value in allowed.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UNRELATED_PROVIDER_SECRET", "must-not-propagate")
    sanitized = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/safe-home"}

    with patch(
        "gateway.platforms.webhook_filters._resolve_script_path",
        return_value=(Path("/tmp/trusted-gate.py"), None),
    ), patch(
        "tools.environments.local.build_subprocess_env",
        return_value=sanitized.copy(),
    ), patch(
        "gateway.platforms.webhook_filters.subprocess.run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout='{"verified":true}',
            stderr="",
        ),
    ) as run:
        keep, transformed = WebhookRouteProcessor().run_route_script(
            "trusted-gate.py",
            {"operation": "reconcile"},
            trusted_github_pr_environment=True,
        )

    assert keep is True
    assert transformed == {"verified": True}
    child_env = run.call_args.kwargs["env"]
    assert {key: child_env[key] for key in allowed} == allowed
    assert "UNRELATED_PROVIDER_SECRET" not in child_env


def test_trusted_signed_control_plane_envelope_survives_generic_redaction():
    payload_value = base64.b64encode(b'{"baseline_gates":["quality"]}').decode()
    signature_value = base64.b64encode(b"s" * 64).decode()

    with patch(
        "gateway.platforms.webhook_filters._resolve_script_path",
        return_value=(Path("/tmp/trusted-gate.py"), None),
    ), patch(
        "tools.environments.local.build_subprocess_env",
        return_value={"PATH": "/usr/bin:/bin", "HOME": "/tmp/safe-home"},
    ), patch(
        "gateway.platforms.webhook_filters.subprocess.run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "gate_resolution_payload": payload_value,
                    "gate_resolution_signature": signature_value,
                }
            ),
            stderr="",
        ),
    ), patch(
        "agent.redact.redact_sensitive_text",
        side_effect=lambda value: value.replace(payload_value, "eyJiYX...JdfQ=="),
    ):
        keep, transformed = WebhookRouteProcessor().run_route_script(
            "trusted-gate.py",
            {"operation": "resolve_execution_gates"},
            trusted_github_pr_environment=True,
        )

    assert keep is True
    assert transformed == {
        "gate_resolution_payload": payload_value,
        "gate_resolution_signature": signature_value,
    }


def test_trusted_signed_control_plane_rejects_extra_fields():
    with patch(
        "gateway.platforms.webhook_filters._resolve_script_path",
        return_value=(Path("/tmp/trusted-gate.py"), None),
    ), patch(
        "tools.environments.local.build_subprocess_env",
        return_value={"PATH": "/usr/bin:/bin", "HOME": "/tmp/safe-home"},
    ), patch(
        "gateway.platforms.webhook_filters.subprocess.run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "gate_resolution_payload": "payload",
                    "gate_resolution_signature": "signature",
                    "unexpected": "must-not-cross-boundary",
                }
            ),
            stderr="",
        ),
    ):
        keep, transformed = WebhookRouteProcessor().run_route_script(
            "trusted-gate.py",
            {"operation": "resolve_execution_gates"},
            trusted_github_pr_environment=True,
        )

    assert keep is False
    assert transformed is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    """Create a WebhookAdapter with the given routes."""
    extra = {"host": "0.0.0.0", "port": 0, "routes": routes}
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
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


# A realistic GitHub pull_request event payload (trimmed)
GITHUB_PR_PAYLOAD = {
    "action": "opened",
    "number": 42,
    "pull_request": {
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
            assert data["delivery_id"] == "gh-delivery-001"

        # Let the asyncio.create_task fire
        await asyncio.sleep(0.05)

        assert len(captured_events) == 1
        event = captured_events[0]
        assert "Review PR #42 by contributor" in event.text
        assert "Add webhook adapter" in event.text
        assert event.source.chat_type == "webhook"
        assert event.source.platform == Platform.WEBHOOK
        assert "github-pr" in event.source.chat_id
        assert event.message_id == "gh-delivery-001"


class TestGitHubPREvidenceScope:
    review_request_id = 123456
    payload = {
        "repository": "org/repo",
        "pr_number": 42,
        "expected_base_sha": "a" * 40,
        "expected_head_sha": "b" * 40,
        "contract_version": "v2",
        "review_request_id": review_request_id,
    }

    @pytest.mark.asyncio
    async def test_threaded_route_records_started_reply_before_dispatch(self):
        route = {
            "evidence": "github_pr",
            "script": "trusted-gate.py",
            "buzz_thread_lifecycle": True,
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(True, {"settled": "started"})
        )

        started = await adapter._mark_github_review_started(
            "github-pr",
            route,
            {
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": 42,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "l" * 43,
            "https://github.com/org/repo/pull/42",
            self.review_request_id,
        )

        assert started is True
        adapter._route_processor.run_route_script.assert_called_once_with(
            "trusted-gate.py",
            {
                "operation": "started",
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": "42",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "review_request_id": self.review_request_id,
                "lease_token": "l" * 43,
                "pr_url": "https://github.com/org/repo/pull/42",
            },
            trusted_github_pr_environment=True,
        )

    @pytest.mark.asyncio
    async def test_threaded_http_route_records_started_before_agent_dispatch(self):
        secret = "secret"
        route = {
            "evidence": "github_pr",
            "review_evidence_mode": "concise",
            "secret": secret,
            "events": ["pull_request"],
            "script": "trusted-gate.py",
            "prompt": "review",
            "buzz_thread_lifecycle": True,
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        gated = {
            **self.payload,
            "lease_token": "l" * 43,
            "pr_url": "https://github.com/org/repo/pull/42",
        }
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[(True, gated), (True, {"settled": "started"})]
        )
        adapter.handle_message = AsyncMock()
        body = json.dumps(GITHUB_PR_PAYLOAD).encode()

        async with TestClient(TestServer(_create_app(adapter))) as client:
            response = await client.post(
                "/webhooks/github-pr",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _github_signature(body, secret),
                    "X-GitHub-Delivery": "threaded-delivery",
                },
            )
        await asyncio.sleep(0)

        assert response.status == 202
        assert adapter._route_processor.run_route_script.call_count == 2
        assert adapter._route_processor.run_route_script.call_args_list[-1].args[1][
            "operation"
        ] == "started"
        adapter.handle_message.assert_awaited_once()

    def test_static_opted_in_route_builds_exact_tuple_scope(self):
        adapter = _make_adapter(
            {
                "github-pr": {
                    "evidence": "github_pr",
                    "review_evidence_mode": "concise",
                    "secret": "secret",
                    "script": "trusted-gate.py",
                    "deliver_extra": {"contract_version": "v2"},
                    "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
                    "baseline_execution_gates": ["quality", "integration", "e2e"],
                    "execution_gate_policy_version": "newtonsapple-v1",
                    "execution_gate_policy_sha256": "f" * 64,
                }
            }
        )

        scope = adapter._evidence_scope_for_route("github-pr", self.payload)

        assert scope is not None
        assert scope.concise_review is True
        assert scope.tuple_dict == {
            "contract_version": "v2",
            "repository": "org/repo",
            "pr_number": 42,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        }
        assert scope.execution_attestation_public_key == b"k" * 32
        assert scope.required_execution_gates == ()
        assert scope.baseline_execution_gates == ("quality", "integration", "e2e")
        assert scope.execution_gate_policy_version == "newtonsapple-v1"
        assert scope.execution_gate_policy_sha256 == "f" * 64
        assert scope.gate_resolution_loader is not None
        assert scope.execution_attestation_loader is not None

    @pytest.mark.parametrize("review_request_id", [None, True, "123456", 0, -1])
    def test_static_opted_in_route_rejects_invalid_review_request_generation(
        self, review_request_id
    ):
        adapter = _make_adapter(
            {
                "github-pr": {
                    "evidence": "github_pr",
                    "review_evidence_mode": "concise",
                    "secret": "secret",
                    "script": "trusted-gate.py",
                    "deliver_extra": {"contract_version": "v2"},
                    "execution_attestation_public_key": base64.b64encode(
                        b"k" * 32
                    ).decode(),
                    "baseline_execution_gates": ["quality", "integration", "e2e"],
                    "execution_gate_policy_version": "newtonsapple-v1",
                    "execution_gate_policy_sha256": "f" * 64,
                }
            }
        )
        payload = {**self.payload, "review_request_id": review_request_id}

        assert adapter._evidence_scope_for_route("github-pr", payload) is None

    @pytest.mark.asyncio
    async def test_opted_in_route_does_not_start_agent_without_valid_evidence_scope(self):
        secret = "secret"
        adapter = _make_adapter(
            {
                "github-pr": {
                    "evidence": "github_pr",
                    "secret": secret,
                    "events": ["pull_request"],
                    "script": "trusted-gate.py",
                    "prompt": "review",
                    "deliver_extra": {"contract_version": "v2"},
                }
            }
        )
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(True, {**self.payload, "lease_token": "lllllllllllllllllllllllllllllllllllllllllll"})
        )
        adapter.handle_message = AsyncMock()
        body = json.dumps(GITHUB_PR_PAYLOAD).encode()

        async with TestClient(TestServer(_create_app(adapter))) as client:
            response = await client.post(
                "/webhooks/github-pr",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _github_signature(body, secret),
                    "X-GitHub-Delivery": "invalid-evidence-scope",
                },
            )

        assert response.status == 503
        adapter.handle_message.assert_not_called()
        assert adapter._route_processor.run_route_script.call_args_list[-1].args == (
            "trusted-gate.py",
            {
                "operation": "release",
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": "42",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "review_request_id": self.review_request_id,
                "lease_token": "lllllllllllllllllllllllllllllllllllllllllll",
            },
        )

    @pytest.mark.asyncio
    async def test_duplicate_http_delivery_releases_newly_reclaimed_lease(self):
        secret = "secret"
        route = {
            "evidence": "github_pr",
            "secret": secret,
            "events": ["pull_request"],
            "script": "trusted-gate.py",
            "prompt": "review",
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        delivery_id = "duplicate-http-delivery"
        assert adapter._record_delivery_id(delivery_id, 10**12) is True
        gated = {**self.payload, "lease_token": "l" * 43}
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[(True, gated), (True, {"settled": "release"})]
        )
        adapter.handle_message = AsyncMock()
        body = json.dumps(GITHUB_PR_PAYLOAD).encode()

        async with TestClient(TestServer(_create_app(adapter))) as client:
            response = await client.post(
                "/webhooks/github-pr",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _github_signature(body, secret),
                    "X-GitHub-Delivery": delivery_id,
                },
            )
            response_data = await response.json()

        assert response.status == 200
        assert response_data["status"] == "duplicate"
        adapter.handle_message.assert_not_called()
        assert adapter._route_processor.run_route_script.call_count == 2
        assert adapter._route_processor.run_route_script.call_args_list[-1].args[1][
            "lease_token"
        ] == "l" * 43

    def test_execution_loader_uses_only_static_script_and_exact_tuple(self):
        route = {
            "evidence": "github_pr",
            "secret": "secret",
            "script": "trusted-gate.py",
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(
                True,
                {
                    "attestation_payload": base64.b64encode(b'{"report":true}').decode(),
                    "attestation_signature": base64.b64encode(b"s" * 64).decode(),
                },
            )
        )
        scope = adapter._evidence_scope_for_route("github-pr", self.payload)

        assert scope is not None
        assert scope.execution_attestation_loader is not None
        payload, signature = scope.execution_attestation_loader()

        assert payload == b'{"report":true}'
        assert signature == base64.b64encode(b"s" * 64).decode()
        adapter._route_processor.run_route_script.assert_called_once_with(
            "trusted-gate.py",
            {
                "operation": "execution_evidence",
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": "42",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "review_request_id": self.review_request_id,
            },
            timeout_seconds=4 * 60 * 60,
            trusted_github_pr_environment=True,
        )

    def test_gate_resolution_loader_uses_static_policy_and_exact_tuple(self):
        route = {
            "evidence": "github_pr",
            "secret": "secret",
            "script": "trusted-gate.py",
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(
                True,
                {
                    "gate_resolution_payload": base64.b64encode(b'{"gates":true}').decode(),
                    "gate_resolution_signature": base64.b64encode(b"s" * 64).decode(),
                },
            )
        )
        scope = adapter._evidence_scope_for_route("github-pr", self.payload)

        assert scope is not None
        assert scope.gate_resolution_loader is not None
        payload, signature = scope.gate_resolution_loader()

        assert payload == b'{"gates":true}'
        assert signature == base64.b64encode(b"s" * 64).decode()
        adapter._route_processor.run_route_script.assert_called_once_with(
            "trusted-gate.py",
            {
                "operation": "resolve_execution_gates",
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": "42",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "review_request_id": self.review_request_id,
            },
            trusted_github_pr_environment=True,
        )

    def test_dynamic_route_cannot_grant_evidence_scope(self):
        adapter = _make_adapter({})
        adapter._dynamic_routes["github-pr"] = {
            "evidence": "github_pr",
            "secret": "secret",
        }
        adapter._routes["github-pr"] = adapter._dynamic_routes["github-pr"]

        assert adapter._evidence_scope_for_route("github-pr", self.payload) is None

    def test_static_route_without_explicit_opt_in_has_no_scope(self):
        adapter = _make_adapter({"github-pr": {"secret": "secret"}})

        assert adapter._evidence_scope_for_route("github-pr", self.payload) is None

    def test_static_route_requires_gate_script_v2_and_execution_attestation_contract(self):
        adapter = _make_adapter(
            {"github-pr": {"evidence": "github_pr", "secret": "secret"}}
        )

        assert adapter._evidence_scope_for_route("github-pr", self.payload) is None

    def test_webhook_schema_exposes_no_execution_browser_file_or_mcp_tools(self):
        config = {
            "platform_toolsets": {
                "webhook": [
                    "terminal",
                    "file",
                    "code_execution",
                    "browser",
                    "computer_use",
                    "attacker-mcp",
                ]
            },
            "mcp_servers": {"attacker-mcp": {"enabled": True}},
        }
        adapter = _make_adapter(
            {
                "github-pr": {
                    "evidence": "github_pr",
                    "secret": "secret",
                    "script": "trusted-gate.py",
                    "deliver_extra": {"contract_version": "v2"},
                    "execution_attestation_public_key": base64.b64encode(
                        b"k" * 32
                    ).decode(),
                    "baseline_execution_gates": ["quality", "integration", "e2e"],
                    "execution_gate_policy_version": "newtonsapple-v1",
                    "execution_gate_policy_sha256": "f" * 64,
                }
            }
        )
        scope = adapter._evidence_scope_for_route("github-pr", self.payload)
        assert scope is not None
        enabled = _get_platform_tools(config, "webhook")

        def schema_names():
            _clear_tool_defs_cache()
            return {
                definition["function"]["name"]
                for definition in get_tool_definitions(
                    enabled_toolsets=sorted(enabled),
                    quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
            }

        assert "github_pr_evidence" not in schema_names()
        with evidence_scope(scope):
            names = schema_names()
        assert "github_pr_evidence" in names
        assert not names & {
            "terminal",
            "process",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "execute_code",
            "browser_navigate",
            "browser_cdp",
            "computer_use",
        }
        assert all(not name.startswith("mcp_") for name in names)


class TestStaticRouteRecovery:
    @pytest.mark.asyncio
    async def test_recovered_event_reenters_filters_gate_and_agent_dispatch(self):
        route = {
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "prompt": "Review PR {number}",
            "deliver": "log",
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(True, {"number": 185, "authorized": True})
        )
        adapter.handle_message = AsyncMock()
        recovered = {
            "delivery_id": "recovery-v2-pr-185",
            "event_type": "pull_request",
            "payload": {"action": "review_requested", "number": 185},
        }

        accepted = await adapter._dispatch_recovered_event(
            "github-pr", route, recovered
        )
        await asyncio.sleep(0)

        assert accepted is True
        adapter._route_processor.run_route_script.assert_called_once_with(
            "trusted-gate.py", recovered["payload"]
        )
        assert adapter.handle_message.await_args is not None
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "Review PR 185"
        assert event.raw_message == {"number": 185, "authorized": True}
        assert event.message_id == "recovery-v2-pr-185"

    @pytest.mark.asyncio
    async def test_reconciliation_does_not_start_when_route_is_disabled(self):
        route = {
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "reconcile": False,
            "reconcile_interval_seconds": 300,
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._run_reconciliation_once = AsyncMock(return_value=0)

        adapter._start_reconciliation_tasks()
        await asyncio.sleep(0)

        adapter._run_reconciliation_once.assert_not_awaited()
        assert adapter._reconciliation_tasks == set()

    @pytest.mark.asyncio
    async def test_reconciliation_task_runs_immediately_and_is_cancelled_on_disconnect(self):
        route = {
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "reconcile": True,
            "reconcile_interval_seconds": 300,
        }
        adapter = _make_adapter({"github-pr": route})
        adapter._run_reconciliation_once = AsyncMock(return_value=0)

        adapter._start_reconciliation_tasks()
        await asyncio.sleep(0)

        adapter._run_reconciliation_once.assert_awaited_once_with("github-pr", route)
        assert len(adapter._reconciliation_tasks) == 1

        await adapter.disconnect()
        assert adapter._reconciliation_tasks == set()

    @pytest.mark.asyncio
    async def test_reconciliation_dispatches_bounded_events_from_static_route_script(self):
        route = {
            "secret": "secret",
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "reconcile_interval_seconds": 300,
        }
        adapter = _make_adapter({"github-pr": route})
        recovered = {
            "delivery_id": "recovery-v2-pr-185",
            "event_type": "pull_request",
            "payload": {"action": "review_requested", "number": 185},
        }
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(True, {"events": [recovered]})
        )
        adapter._dispatch_recovered_event = AsyncMock(return_value=True)

        dispatched = await adapter._run_reconciliation_once("github-pr", route)

        assert dispatched == 1
        adapter._route_processor.run_route_script.assert_called_once_with(
            "trusted-gate.py", {"operation": "reconcile"}
        )
        adapter._dispatch_recovered_event.assert_awaited_once_with(
            "github-pr", route, recovered
        )

    @pytest.mark.asyncio
    async def test_dynamic_or_malformed_reconciliation_cannot_dispatch(self):
        adapter = _make_adapter({})
        route = {
            "script": "attacker.py",
            "reconcile_interval_seconds": 1,
        }
        adapter._dynamic_routes["dynamic"] = route
        adapter._route_processor.run_route_script = MagicMock(
            return_value=(True, {"events": [{"delivery_id": "x"}]})
        )
        adapter._dispatch_recovered_event = AsyncMock()

        dispatched = await adapter._run_reconciliation_once("dynamic", route)

        assert dispatched == 0
        adapter._route_processor.run_route_script.assert_not_called()
        adapter._dispatch_recovered_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_releases_lease_when_evidence_scope_is_invalid(self):
        route = {
            "evidence": "github_pr",
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "prompt": "review",
            "deliver_extra": {"contract_version": "v2"},
        }
        adapter = _make_adapter({"github-pr": route})
        gated = {
            "contract_version": "v2",
            "repository": "org/repo",
            "pr_number": 42,
            "expected_base_sha": "a" * 40,
            "expected_head_sha": "b" * 40,
            "review_request_id": 123456,
            "lease_token": "lllllllllllllllllllllllllllllllllllllllllll",
        }
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[(True, gated), (True, {"settled": "release"})]
        )

        accepted = await adapter._dispatch_recovered_event(
            "github-pr",
            route,
            {
                "delivery_id": "recovery-v2-pr-185",
                "event_type": "pull_request",
                "payload": {"action": "review_requested", "number": 185},
            },
        )

        assert accepted is False
        assert adapter._route_processor.run_route_script.call_args_list[-1].args == (
            "trusted-gate.py",
            {
                "operation": "release",
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": "42",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "review_request_id": 123456,
                "lease_token": "lllllllllllllllllllllllllllllllllllllllllll",
            },
        )

    @pytest.mark.asyncio
    async def test_recovery_releases_lease_when_required_skill_is_unavailable(self):
        route = {
            "evidence": "github_pr",
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "prompt": "review",
            "skills": ["pr-review"],
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        gated = {
            "contract_version": "v2",
            "repository": "org/repo",
            "pr_number": 42,
            "expected_base_sha": "a" * 40,
            "expected_head_sha": "b" * 40,
            "review_request_id": 123456,
            "lease_token": "l" * 43,
        }
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[(True, gated), (True, {"settled": "release"})]
        )
        adapter.handle_message = AsyncMock()

        with patch("agent.skill_commands.get_skill_commands", return_value={}):
            accepted = await adapter._dispatch_recovered_event(
                "github-pr",
                route,
                {
                    "delivery_id": "recovery-v2-pr-185",
                    "event_type": "pull_request",
                    "payload": {"action": "review_requested", "number": 185},
                },
            )

        assert accepted is False
        adapter.handle_message.assert_not_called()
        assert adapter._route_processor.run_route_script.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_recovered_delivery_releases_newly_reclaimed_lease(self):
        route = {
            "evidence": "github_pr",
            "script": "trusted-gate.py",
            "events": ["pull_request"],
            "prompt": "review",
            "deliver_extra": {"contract_version": "v2"},
            "execution_attestation_public_key": base64.b64encode(b"k" * 32).decode(),
            "baseline_execution_gates": ["quality", "integration", "e2e"],
            "execution_gate_policy_version": "newtonsapple-v1",
            "execution_gate_policy_sha256": "f" * 64,
        }
        adapter = _make_adapter({"github-pr": route})
        delivery_id = "recovery-v2-pr-185"
        assert adapter._record_delivery_id(delivery_id, 10**12) is True
        gated = {
            "contract_version": "v2",
            "repository": "org/repo",
            "pr_number": 42,
            "expected_base_sha": "a" * 40,
            "expected_head_sha": "b" * 40,
            "review_request_id": 123456,
            "lease_token": "l" * 43,
        }
        adapter._route_processor.run_route_script = MagicMock(
            side_effect=[(True, gated), (True, {"settled": "release"})]
        )
        adapter.handle_message = AsyncMock()

        accepted = await adapter._dispatch_recovered_event(
            "github-pr",
            route,
            {
                "delivery_id": delivery_id,
                "event_type": "pull_request",
                "payload": {"action": "review_requested", "number": 185},
            },
        )

        assert accepted is False
        adapter.handle_message.assert_not_called()
        assert adapter._route_processor.run_route_script.call_count == 2
        assert adapter._route_processor.run_route_script.call_args_list[-1].args[1][
            "lease_token"
        ] == "l" * 43


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
        with patch(
            "agent.skill_commands.build_skill_invocation_message",
            return_value=skill_content,
        ) as mock_build, patch(
            "agent.skill_commands.get_skill_commands",
            return_value={"/code-review": {"name": "code-review"}},
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

        mock_runner = MagicMock()
        mock_runner.adapters = {Platform.TELEGRAM: mock_tg_adapter}
        mock_runner.config = GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}
        )
        adapter.gateway_runner = mock_runner

        # First, simulate a webhook POST to set up delivery_info
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/alerts",
                json={"message": "Server is on fire!"},
                headers={"X-GitHub-Delivery": "alert-001"},
            )
            assert resp.status == 202

        # The adapter should have stored delivery info
        chat_id = "webhook:alerts:alert-001"
        assert chat_id in adapter._delivery_info

        # Now call send() as if the agent has finished
        result = await adapter.send(chat_id, "I've acknowledged the alert.")

        assert result.success is True
        mock_tg_adapter.send.assert_awaited_once_with(
            "12345", "I've acknowledged the alert.", metadata=None
        )
        # Delivery info is retained after send() so interim status messages
        # don't strand the final response (TTL-based cleanup happens on POST).
        assert chat_id in adapter._delivery_info


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

        # POST a webhook to set up delivery info
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

        chat_id = "webhook:pr-bot:gh-comment-001"
        assert chat_id in adapter._delivery_info

        # Verify deliver_extra was rendered with payload data
        delivery = adapter._delivery_info[chat_id]
        assert delivery["deliver_extra"]["repo"] == "org/repo"
        assert delivery["deliver_extra"]["pr_number"] == "42"

        # Mock subprocess.run and call send()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Comment posted"
        mock_result.stderr = ""

        with patch(
            "gateway.platforms.webhook.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            result = await adapter.send(
                chat_id, "LGTM! The code looks great."
            )

        assert result.success is True
        mock_run.assert_called_once_with(
            [
                "gh", "pr", "comment", "42",
                "--repo", "org/repo",
                "--body", "LGTM! The code looks great.",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        # Delivery info is retained after send() so interim status messages
        # don't strand the final response (TTL-based cleanup happens on POST).
        assert chat_id in adapter._delivery_info


class TestGitHubReviewDelivery:

    base_sha = "a" * 40
    head_sha = "b" * 40
    publisher = "newtonsapple-bot"
    review_request_id = 123456

    @pytest.fixture(autouse=True)
    def complete_evidence_scope(self):
        scope = EvidenceScope(
            contract_version="v2",
            repository="org/repo",
            pr_number=42,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        scope.manifest_created = True
        scope.expected_changed_files = 0
        scope.observed_changed_files = 0
        scope.pull_validated = True
        scope.workflow_runs_observed = 1
        scope.tree_diff_reconciled = True
        scope.canonical_files_materialized = True
        scope.required_logs_materialized = True
        scope.required_artifact_inventories_materialized = True
        scope.execution_attestation_valid = True
        with evidence_scope(scope):
            self.scope = scope
            yield

    def _marker(self, *, head_sha=None):
        return (
            "<!-- newtonsapple-pr-review:v2 repo=org/repo pr=42 "
            f"base={self.base_sha} head={head_sha or self.head_sha} "
            f"request={self.review_request_id} -->"
        )

    def _delivery(self, **overrides):
        extra = {
            "repo": "org/repo",
            "pr_number": "42",
            "base_sha": self.base_sha,
            "base_ref": "dev",
            "head_sha": self.head_sha,
            "publisher_login": self.publisher,
            "requested_reviewer": self.publisher,
        }
        extra.update(overrides)
        return {
            "deliver_extra": extra,
            "_review_request_id": self.review_request_id,
        }

    def _actor(self, login=None):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({"login": login or self.publisher}),
            stderr="",
        )

    def _live_pr(self, **overrides):
        data = {
            "state": "open",
            "draft": False,
            "base": {"sha": self.base_sha, "ref": "dev"},
            "head": {"sha": self.head_sha},
            "requested_reviewers": [{"login": self.publisher}],
        }
        data.update(overrides)
        return MagicMock(returncode=0, stdout=json.dumps(data), stderr="")

    @staticmethod
    def _page(items=None):
        return MagicMock(returncode=0, stdout=json.dumps([items or []]), stderr="")

    def _accepted_review(self, **overrides):
        data = {
            "id": 123,
            "user": {"login": self.publisher},
            "state": "COMMENTED",
            "commit_id": self.head_sha,
        }
        data.update(overrides)
        return MagicMock(returncode=0, stdout=json.dumps(data), stderr="")

    @pytest.mark.asyncio
    async def test_posts_formal_comment_review_after_exact_tuple_recheck(self):
        adapter = _make_adapter({})
        marker = self._marker()
        content = f"No findings.\n\n{marker}"
        posted = self._accepted_review()

        with patch(
            "gateway.platforms.webhook.subprocess.run",
            side_effect=[
                self._actor(), self._live_pr(), self._page(), posted,
            ],
        ) as mock_run:
            result = await adapter._deliver_github_review(content, self._delivery())

        assert result.success is True
        assert mock_run.call_args_list[-1].args[0] == [
            "gh", "api", "--method", "POST",
            "repos/org/repo/pulls/42/reviews",
            "-f", f"body={content}",
            "-f", "event=COMMENT",
            "-f", f"commit_id={self.head_sha}",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "delivery_overrides"),
        [
            ("No marker", {}),
            ("Noncanonical\n\n<!-- newtonsapple-pr-review:v2 repo=org/repo pr=42 base="
             + "a" * 40 + " head=" + "c" * 40 + " -->", {}),
        ],
    )
    async def test_rejects_missing_or_noncanonical_marker(
        self, content, delivery_overrides
    ):
        adapter = _make_adapter({})
        with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
            result = await adapter._deliver_github_review(
                content, self._delivery(**delivery_overrides)
            )
        assert result.success is False
        assert result.error == "Missing canonical review marker"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_any_additional_conflicting_review_marker(self):
        adapter = _make_adapter({})
        conflicting = (
            "<!-- newtonsapple-pr-review:v2 repo=org/repo pr=99 "
            f"base={self.base_sha} head={self.head_sha} -->"
        )
        content = f"No findings.\n\n{self._marker()}\n\n{conflicting}"

        with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
            result = await adapter._deliver_github_review(content, self._delivery())

        assert result.success is False
        assert result.error == "Conflicting canonical review marker"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_incomplete_evidence_before_github_access(self):
        adapter = _make_adapter({})
        incomplete = EvidenceScope(
            contract_version="v2",
            repository="org/repo",
            pr_number=42,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        with evidence_scope(incomplete):
            with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
                result = await adapter._deliver_github_review(
                    self._marker(), self._delivery()
                )

        assert result.success is False
        assert result.error == "GitHub PR review evidence is incomplete or out of scope"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_marker_bearing_review_when_execution_attestation_is_absent(self):
        adapter = _make_adapter({})
        self.scope.execution_attestation_valid = False

        with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )

        assert result.success is False
        assert result.error == "GitHub PR execution evidence is incomplete or out of scope"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_marker_bearing_review_when_review_attestation_is_absent(self):
        adapter = _make_adapter({})
        self.scope.tree_diff_reconciled = False

        with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )

        assert result.success is False
        assert result.error == "GitHub PR review evidence is incomplete or out of scope"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_wrong_publisher_identity(self):
        adapter = _make_adapter({})
        with patch(
            "gateway.platforms.webhook.subprocess.run",
            return_value=self._actor("different-bot"),
        ) as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )
        assert result.success is False
        assert result.error == "Publisher identity mismatch"
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "live_overrides",
        [
            {"state": "closed"},
            {"draft": True},
            {"base": {"sha": "a" * 40, "ref": "feature"}},
            {"base": {"sha": "a" * 40, "ref": "staging"}},
            {"base": {"sha": "c" * 40, "ref": "dev"}},
            {"head": {"sha": "c" * 40}},
            {"requested_reviewers": []},
        ],
    )
    async def test_rejects_changed_or_ineligible_pr_state(self, live_overrides):
        adapter = _make_adapter({})
        with patch(
            "gateway.platforms.webhook.subprocess.run",
            side_effect=[self._actor(), self._live_pr(**live_overrides)],
        ) as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )
        assert result.success is False
        assert result.error == "PR state changed before publish"
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_only_structured_exact_head_formal_review_is_successful_noop(self):
        adapter = _make_adapter({})
        marked_review = {
            "id": 456,
            "user": {"login": self.publisher},
            "body": self._marker(),
            "state": "COMMENTED",
            "commit_id": self.head_sha,
        }
        with patch(
            "gateway.platforms.webhook.subprocess.run",
            side_effect=[self._actor(), self._live_pr(), self._page([marked_review])],
        ) as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )
        assert result.success is True
        assert mock_run.call_count == 3
        assert all("--method" not in call.args[0] for call in mock_run.call_args_list)

    @pytest.mark.asyncio
    async def test_bot_issue_comment_marker_does_not_suppress_formal_review(self):
        adapter = _make_adapter({})
        marked_comment = {"user": {"login": self.publisher}, "body": self._marker()}

        def run(command, **kwargs):
            joined = " ".join(command)
            if command == ["gh", "api", "user"]:
                return self._actor()
            if joined.endswith("repos/org/repo/pulls/42"):
                return self._live_pr()
            if "repos/org/repo/issues/42/comments" in joined:
                return self._page([marked_comment])
            if "--method POST" in joined:
                return self._accepted_review()
            if "repos/org/repo/pulls/42/reviews" in joined:
                return self._page()
            raise AssertionError(f"unexpected command: {command}")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=run) as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )

        assert result.success is True
        assert "--method" in mock_run.call_args_list[-1].args[0]
        assert all(
            "repos/org/repo/issues/42/comments" not in " ".join(call.args[0])
            for call in mock_run.call_args_list
        )

    @pytest.mark.asyncio
    async def test_human_marker_does_not_suppress_publication(self):
        adapter = _make_adapter({})
        human_marker = {"user": {"login": "human"}, "body": self._marker()}
        posted = self._accepted_review()
        with patch(
            "gateway.platforms.webhook.subprocess.run",
            side_effect=[
                self._actor(), self._live_pr(), self._page([human_marker]), posted,
            ],
        ) as mock_run:
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )
        assert result.success is True
        assert "--method" in mock_run.call_args_list[-1].args[0]

    @pytest.mark.asyncio
    async def test_rejects_wrong_actor_in_acceptance_response(self):
        adapter = _make_adapter({})
        posted = self._accepted_review(user={"login": "different-bot"})
        with patch(
            "gateway.platforms.webhook.subprocess.run",
            side_effect=[
                self._actor(), self._live_pr(), self._page(), posted,
            ],
        ):
            result = await adapter._deliver_github_review(
                self._marker(), self._delivery()
            )

        assert result.success is False
        assert result.error == "GitHub did not confirm the expected formal review"

    @pytest.mark.asyncio
    async def test_github_review_send_publishes_with_reserved_delivery_authority(self):
        adapter = _make_adapter(
            {
                "pr-review": {
                    "evidence": "github_pr",
                    "script": "newtonsapple-pr-review-gate.py",
                }
            }
        )
        chat_id = "webhook:pr-review:delivery-1"
        delivery = {
            "deliver": "github_review",
            "deliver_extra": self._delivery()["deliver_extra"],
            "_trusted_evidence_route": "pr-review",
            "_settlement_lease_token": "l" * 43,
            "_review_request_id": self.review_request_id,
            "_evidence_tuple": {
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": 42,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
            },
        }
        adapter._delivery_info[chat_id] = delivery

        with patch.object(
            adapter,
            "_claim_review_publication",
            new=AsyncMock(return_value=True),
        ) as claim, patch.object(
            adapter,
            "_deliver_github_review",
            new=AsyncMock(return_value=SendResult(success=True)),
        ) as publish:
            result = await adapter.send(chat_id, self._marker())

        assert result.success is True
        claim.assert_awaited_once_with(delivery)
        publish.assert_awaited_once_with(self._marker(), delivery)

    @pytest.mark.asyncio
    async def test_github_review_send_does_not_publish_when_generation_claim_is_rejected(self):
        adapter = _make_adapter(
            {
                "pr-review": {
                    "evidence": "github_pr",
                    "script": "newtonsapple-pr-review-gate.py",
                }
            }
        )
        chat_id = "webhook:pr-review:delivery-1"
        delivery = {
            "deliver": "github_review",
            "deliver_extra": self._delivery()["deliver_extra"],
            "_trusted_evidence_route": "pr-review",
            "_settlement_lease_token": "l" * 43,
            "_review_request_id": self.review_request_id,
            "_evidence_tuple": {
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": 42,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
            },
        }
        adapter._delivery_info[chat_id] = delivery

        with patch.object(
            adapter,
            "_claim_review_publication",
            new=AsyncMock(return_value=False),
        ) as claim, patch.object(
            adapter,
            "_deliver_github_review",
            new=AsyncMock(),
        ) as publish:
            result = await adapter.send(chat_id, self._marker())

        assert result.success is False
        assert result.error == "Review publication claim unavailable"
        claim.assert_awaited_once_with(delivery)
        publish.assert_not_awaited()
        assert chat_id not in adapter._successful_github_reviews
        assert (
            adapter._delivery_info[chat_id]["_github_review_failure_code"]
            == "publication_failed"
        )

    @pytest.mark.asyncio
    async def test_github_review_send_propagates_publication_failure(self):
        adapter = _make_adapter(
            {
                "pr-review": {
                    "evidence": "github_pr",
                    "script": "newtonsapple-pr-review-gate.py",
                }
            }
        )
        chat_id = "webhook:pr-review:delivery-1"
        adapter._delivery_info[chat_id] = {
            "deliver": "github_review",
            "deliver_extra": self._delivery()["deliver_extra"],
            "_trusted_evidence_route": "pr-review",
            "_settlement_lease_token": "l" * 43,
            "_review_request_id": self.review_request_id,
            "_evidence_tuple": {
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": 42,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
            },
        }

        with patch.object(
            adapter,
            "_claim_review_publication",
            new=AsyncMock(return_value=True),
        ), patch.object(
            adapter,
            "_deliver_github_review",
            new=AsyncMock(return_value=SendResult(success=False, error="publish failed")),
        ) as publish:
            result = await adapter.send(chat_id, self._marker())

        assert result.success is False
        assert result.error == "publish failed"
        publish.assert_awaited_once()
        assert chat_id not in adapter._successful_github_reviews
        assert (
            adapter._delivery_info[chat_id]["_github_review_failure_code"]
            == "publication_failed"
        )

    @pytest.mark.asyncio
    async def test_webhook_response_without_delivery_authority_fails_closed(self):
        adapter = _make_adapter({})

        result = await adapter.send(
            "webhook:pr-review:expired-delivery", self._marker()
        )

        assert result.success is False
        assert result.error == "Webhook delivery authority is missing or expired"

    def test_delivery_authority_survives_the_maximum_review_window(self):
        adapter = _make_adapter({})
        adapter._idempotency_ttl = 60
        now = 20_000.0
        retained = "webhook:pr-review:retained"
        expired = "webhook:pr-review:expired"
        adapter._delivery_info = {retained: {}, expired: {}}
        adapter._delivery_info_created = {
            retained: now - (5 * 60 * 60) + 1,
            expired: now - (5 * 60 * 60) - 1,
        }

        adapter._prune_delivery_info(now)

        assert retained in adapter._delivery_info
        assert expired not in adapter._delivery_info

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("send_result", "outcome", "expected_operation", "expected_failure_code"),
        [
            (SendResult(success=True), ProcessingOutcome.SUCCESS, "complete", None),
            (
                SendResult(
                    success=False,
                    error="GitHub PR review evidence is incomplete or out of scope",
                ),
                ProcessingOutcome.SUCCESS,
                "release",
                "review_evidence_incomplete",
            ),
            (None, ProcessingOutcome.FAILURE, "release", "processing_failed"),
        ],
    )
    async def test_settles_exact_tuple_after_processing(
        self, send_result, outcome, expected_operation, expected_failure_code
    ):
        adapter = _make_adapter(
            {
                "pr-review": {
                    "evidence": "github_pr",
                    "script": "newtonsapple-pr-review-gate.py",
                    "deliver_extra": {"contract_version": "v2"},
                }
            }
        )
        chat_id = "webhook:pr-review:delivery-1"
        delivery = {
            "deliver": "github_review",
            "deliver_extra": self._delivery(contract_version="v2")["deliver_extra"],
            "_trusted_evidence_route": "pr-review",
            "_settlement_lease_token": "lllllllllllllllllllllllllllllllllllllllllll",
            "_review_request_id": self.review_request_id,
            "_evidence_tuple": {
                "contract_version": "v2",
                "repository": "org/repo",
                "pr_number": 42,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
            },
        }
        adapter._delivery_info[chat_id] = delivery
        event = MessageEvent(
            text="review",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id=chat_id,
                chat_name="webhook/pr-review",
                chat_type="webhook",
                user_id="webhook:pr-review",
                user_name="pr-review",
            ),
            message_id="delivery-1",
        )

        if send_result is not None:
            with patch.object(
                adapter,
                "_claim_review_publication",
                new=AsyncMock(return_value=True),
            ), patch.object(
                adapter,
                "_deliver_github_review",
                new=AsyncMock(return_value=send_result),
            ):
                await adapter.send(chat_id, self._marker())

        with patch.object(
            adapter._route_processor,
            "run_route_script",
            return_value=(True, {"settled": True}),
        ) as settle:
            await adapter.on_processing_complete(event, outcome)

        settlement = {
            "operation": expected_operation,
            "contract_version": "v2",
            "repository": "org/repo",
            "pr_number": "42",
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "review_request_id": self.review_request_id,
            "lease_token": "lllllllllllllllllllllllllllllllllllllllllll",
        }
        if expected_failure_code is not None:
            settlement["failure_code"] = expected_failure_code
        settle.assert_called_once_with(
            "newtonsapple-pr-review-gate.py",
            settlement,
            trusted_github_pr_environment=True,
        )

    @pytest.mark.asyncio
    async def test_dynamic_delivery_cannot_invoke_rendered_settlement_script(self):
        adapter = _make_adapter({})
        chat_id = "webhook:dynamic:delivery-1"
        adapter._delivery_info[chat_id] = {
            "deliver": "github_review",
            "deliver_extra": self._delivery(
                settlement_script="attacker-controlled.py",
                contract_version="v2",
            )["deliver_extra"],
        }
        event = MessageEvent(
            text="review",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id=chat_id,
                chat_name="webhook/dynamic",
                chat_type="webhook",
                user_id="webhook:dynamic",
                user_name="dynamic",
            ),
            message_id="delivery-1",
        )

        with patch.object(
            adapter._route_processor, "run_route_script"
        ) as settlement_script:
            await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

        settlement_script.assert_not_called()


def test_required_pr_review_skill_is_bundled_with_generation_contract():
    skill_path = (
        Path(__file__).parents[2] / "skills" / "github" / "pr-review" / "SKILL.md"
    )
    content = skill_path.read_text(encoding="utf-8")

    assert "name: pr-review" in content
    assert "positive GitHub review-request timeline event ID" in content
    assert "request=REQUEST_ID" in content
    assert "older generation's marker must not block" in content
