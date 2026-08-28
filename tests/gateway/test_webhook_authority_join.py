"""Current-main composition tests for webhook intake authority."""

import asyncio
import hashlib
import hmac
import json
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from agent.outbound_webhooks import (
    WebhookTarget,
    _build_delivery,
    _serialize_payload,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import ProcessingOutcome, SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    WebhookConfigurationError,
    WebhookTargetDeliveryDisposition,
    WebhookTargetDeliveryResult,
    _IDEMPOTENCY_DEFAULT_MAX_ENTRIES,
    _IDEMPOTENCY_DEFAULT_MAX_STORAGE_BYTES,
    _IDEMPOTENCY_MAX_ENTRIES_LIMIT,
    _IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT,
    _INSECURE_NO_AUTH,
    _RAW_PAYLOAD_DEFAULT_CAP_BYTES,
    _RAW_PAYLOAD_MAX_CAP_BYTES,
    _RAW_PAYLOAD_MIN_CAP_BYTES,
    _clear_quarantined_retirement_owner,
    _quarantined_retirement_owners,
)
from gateway.platforms.webhook_auth import WebhookLocalBypassReceipt
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_filters import (
    MAX_SCRIPT_SNAPSHOT_BYTES,
    WebhookRouteProcessor,
    WebhookScriptDisposition,
    WebhookScriptResult,
)
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    AdmitResult,
    AdmitSaturationReason,
    MINIMUM_MAX_STORAGE_BYTES,
    OperationState,
    TargetState,
    WebhookOperationLedger,
    _TOMBSTONE_STORAGE_RESERVATION_BYTES,
)


def _adapter(
    routes=None,
    max_entries=8,
    rate_limit=30,
    max_storage_bytes=_IDEMPOTENCY_DEFAULT_MAX_STORAGE_BYTES,
):
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": routes or {},
                "rate_limit": rate_limit,
                "idempotency_max_entries": max_entries,
                "idempotency_max_storage_bytes": max_storage_bytes,
            },
        )
    )


def _grant_snapshot(adapter: WebhookAdapter, envelope: WebhookEnvelope) -> dict:
    return {
        "v": 1,
        "toolsets": [],
        "profile_generation": adapter._current_profile_authority_generation(
            envelope.authority_profile,
            route_name=envelope.route.name,
        ),
    }


@pytest.fixture(autouse=True)
def _isolated_ledger_home(tmp_path, monkeypatch):
    """Every adapter in this module owns a fresh durable ledger."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _app(adapter):
    app = web.Application(client_max_size=adapter._max_body_bytes)
    # This focused harness predates the public liveness/readiness split. Keep
    # its historical authority assertions pointed at readiness; production
    # connect() exposes readiness at /ready and keeps /health liveness-only.
    app.router.add_get("/health", adapter._handle_ready)
    app.router.add_get("/ready", adapter._handle_ready)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post(
        "/p/{profile}/webhooks/{route_name}",
        adapter._handle_webhook,
    )
    return app


def _stage_replay_safe_platform_delivery(
    adapter: WebhookAdapter,
    *,
    trace_id: str,
):
    """Leave one exact target carrier ready for a replacement to recover."""

    raw_body = json.dumps({"trace": trace_id}, separators=(",", ":")).encode()
    route = WebhookRouteConfig.bind(
        "primary-reconnect",
        {"provider": "generic"},
        headers={},
        request_profile="default",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id=trace_id,
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    source = adapter._source_for_envelope(envelope)
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "direct",
            "text": "one reconnect recovery response",
            "payload": {"trace": trace_id},
            "message_id": envelope.delivery_id,
            "source": source.to_dict(),
        },
        target_snapshot={
            "v": 1,
            "kind": "platform",
            "profile": "default",
            "platform": "telegram",
            "chat_id": "recovery-chat",
        },
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    assert adapter._operation_ledger.mark_running(prepared)
    staged = adapter._stage_exact_delivery(
        prepared,
        "one reconnect recovery response",
        {"v": 1, "kind": "direct"},
    )
    assert staged.state is OperationState.DELIVERY_READY
    assert adapter._operation_ledger.relinquish_recovery_claim(staged)
    return envelope


def _prepare_owned_direct_operation(
    adapter: WebhookAdapter,
    *,
    raw_body: bytes,
    content: str,
    target_snapshot=None,
):
    """Leave one exact direct-delivery carrier owned by ``adapter``."""

    route = WebhookRouteConfig.bind(
        "events",
        {"provider": "generic"},
        headers={},
        request_profile="default",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    source = adapter._source_for_envelope(envelope)
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "direct",
            "text": content,
            "payload": envelope.mutable_payload(),
            "message_id": envelope.delivery_id,
            "source": source.to_dict(),
        },
        target_snapshot=(
            target_snapshot
            or {
                "v": 1,
                "kind": "platform",
                "profile": "default",
                "platform": "telegram",
                "chat_id": "recovery-chat",
            }
        ),
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    return envelope, prepared


def _runner_for_primary_webhook_reconnect(
    old_adapter: WebhookAdapter,
    replacement_adapter: WebhookAdapter,
    target_adapter,
):
    """Build the minimum real runner registry used by the primary watcher."""

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.WEBHOOK: replacement_adapter.config},
        multiplex_profiles=False,
    )
    runner._running = True
    runner._draining = False
    runner._external_drain_active = False
    runner._shutdown_event = asyncio.Event()
    runner._failed_platforms = {
        Platform.WEBHOOK: {
            "config": replacement_adapter.config,
            "attempts": 0,
            "next_retry": time.monotonic() - 1,
        }
    }
    runner.adapters = {
        Platform.WEBHOOK: old_adapter,
        Platform.TELEGRAM: target_adapter,
    }
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner.session_store = MagicMock()
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    runner._background_tasks = set()
    runner._webhook_recovery_retry_task = None
    runner._webhook_recovery_retry_adapter = None
    runner._startup_restore_in_progress = False
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._update_platform_runtime_status = MagicMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock(return_value=0)
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    old_adapter.gateway_runner = runner
    replacement_adapter.gateway_runner = runner
    return runner


class TestLedgerConfiguration:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (0, 1),
            (-1, 1),
            ("bad", _IDEMPOTENCY_DEFAULT_MAX_ENTRIES),
            (True, _IDEMPOTENCY_DEFAULT_MAX_ENTRIES),
            (False, _IDEMPOTENCY_DEFAULT_MAX_ENTRIES),
            (float("inf"), _IDEMPOTENCY_DEFAULT_MAX_ENTRIES),
            (_IDEMPOTENCY_MAX_ENTRIES_LIMIT + 1, _IDEMPOTENCY_MAX_ENTRIES_LIMIT),
        ],
    )
    def test_ceiling_normalization(self, configured, expected):
        assert _adapter(max_entries=configured)._idempotency_max_entries == expected

    @pytest.mark.parametrize(
        "configured",
        [
            None,
            True,
            False,
            1.5,
            "bad",
            MINIMUM_MAX_STORAGE_BYTES - 1,
            _IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT + 1,
        ],
    )
    def test_storage_safety_limit_rejects_invalid_configuration(self, configured):
        with pytest.raises(
            WebhookConfigurationError,
            match="idempotency_max_storage_bytes",
        ):
            _adapter(max_storage_bytes=configured)

    @pytest.mark.parametrize(
        "configured",
        [
            MINIMUM_MAX_STORAGE_BYTES,
            str(MINIMUM_MAX_STORAGE_BYTES),
            _IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT,
        ],
    )
    def test_storage_safety_limit_accepts_exact_bounded_integers(self, configured):
        assert _adapter(
            max_storage_bytes=configured
        )._idempotency_max_storage_bytes == int(configured)


class TestRateLimitContract:
    def test_exact_window_boundary_is_expired(self):
        adapter = _adapter(rate_limit=1)
        assert adapter._record_rate_limit_hit("events", 100.0) is True
        assert adapter._record_rate_limit_hit("events", 160.0) is True


class TestRawEnvelopeContract:
    @pytest.mark.parametrize("cap", [64, 128, 4000, 8192])
    def test_envelope_is_valid_json_and_never_exceeds_utf8_cap(self, cap):
        payload = {"text": "🦊" * 5000, "quote": '"\\' * 200}
        rendered = _adapter()._render_raw_payload(payload, cap)
        assert len(rendered.encode("utf-8")) <= cap
        envelope = json.loads(rendered)
        assert set(envelope) == {"payload", "truncated", "original_bytes"}
        assert isinstance(envelope["payload"], str)
        assert envelope["original_bytes"] > 0

    def test_default_raw_token_uses_complete_envelope(self):
        rendered = _adapter()._render_prompt("raw={__raw__}", {"a": "b"}, "push", "r")
        envelope = json.loads(rendered.split("raw=", 1)[1])
        assert envelope["truncated"] is False
        assert '"a": "b"' in envelope["payload"]
        assert (
            len(
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            <= _RAW_PAYLOAD_DEFAULT_CAP_BYTES
        )

    def test_explicit_raw_cap_is_supported(self):
        rendered = _adapter()._render_prompt(
            "{__raw__:128}", {"x": "z" * 5000}, "push", "r"
        )
        assert len(rendered.encode("utf-8")) <= 128
        assert json.loads(rendered)["truncated"] is True

    @pytest.mark.parametrize(
        "template",
        [
            "{__raw__:63}",
            f"{{__raw__:{_RAW_PAYLOAD_MAX_CAP_BYTES + 1}}}",
            "{__raw__:banana}",
            "{__raw__:}",
        ],
    )
    def test_invalid_raw_caps_fail_closed(self, template):
        with pytest.raises(ValueError):
            _adapter()._render_prompt(template, {"x": 1}, "push", "r")

    def test_raw_payload_does_not_trigger_second_template_pass(self):
        rendered = _adapter()._render_prompt(
            "{__raw__}", {"x": "{event_type}"}, "push", "r"
        )
        assert "{event_type}" in json.loads(rendered)["payload"]

    def test_constants_are_ordered(self):
        assert (
            _RAW_PAYLOAD_MIN_CAP_BYTES
            < _RAW_PAYLOAD_DEFAULT_CAP_BYTES
            < _RAW_PAYLOAD_MAX_CAP_BYTES
        )


class TestFilterAuthority:
    def test_regex_filter_preserves_normal_search_semantics(self):
        processor = WebhookRouteProcessor()

        assert processor.filter_matches(
            {"field": "ref", "regex": r"^refs/heads/(main|release-\d+)$"},
            {"ref": "refs/heads/release-42"},
            "push",
            {},
        )

    def test_pathological_regex_is_timed_out_outside_the_gateway_process(self):
        processor = WebhookRouteProcessor()
        started = time.monotonic()

        matched = processor.filter_matches(
            {"field": "value", "regex": r"(a+)+$"},
            {"value": "a" * 50_000 + "!"},
            "push",
            {},
        )

        assert matched is False
        assert time.monotonic() - started < 1.0

    @pytest.mark.parametrize(
        "filters",
        [
            [{"field": "value", "equals": 1}] * 65,
            {
                "not": {
                    "not": {
                        "not": {
                            "not": {
                                "not": {
                                    "not": {
                                        "not": {
                                            "not": {
                                                "field": "value",
                                                "equals": 1,
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            {"all": [{"field": "value", "regex": "1"} for _ in range(9)]},
        ],
    )
    def test_filter_complexity_limits_fail_closed(self, filters):
        processor = WebhookRouteProcessor()

        assert not processor.route_filters_match(
            {"filters": filters},
            {"value": 1},
            "push",
            {},
        )

    @pytest.mark.asyncio
    async def test_blocked_regex_worker_does_not_block_health(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        real_popen = subprocess.Popen

        def blocked_regex_worker(*args, **kwargs):
            # Preserve the production READY/stdin/match protocol.  Holding the
            # caller after the real spawn only models a route-worker stall; it
            # must not stall the aiohttp event loop or its health endpoint.
            process = real_popen(*args, **kwargs)
            started.set()
            assert release.wait(5)
            return process

        monkeypatch.setattr(
            "gateway.platforms.webhook_filters.subprocess.Popen",
            blocked_regex_worker,
        )
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "filters": {"field": "value", "regex": "^safe$"},
            }
        })

        async with TestClient(TestServer(_app(adapter))) as client:
            post_task = asyncio.create_task(
                client.post("/webhooks/events", json={"value": "unsafe"})
            )
            try:
                # Process startup competes with the full 18-worker CI shard;
                # this wait synchronizes the test and is not the health SLA.
                assert await asyncio.to_thread(started.wait, 5)
                health = await asyncio.wait_for(client.get("/health"), timeout=0.5)
                assert health.status == 200
            finally:
                release.set()
            response = await asyncio.wait_for(post_task, timeout=2)

        assert response.status == 200

    @pytest.mark.parametrize(
        "nested",
        [
            None,
            "bad",
            [],
            7,
            {},
            {"field": "admin", "exists": "bad"},
            {"field": "admin", "regex": "["},
            {"field": "admin", "in_file": None},
            {"all": []},
            {"any": []},
            {
                "field": "admin",
                "equals": True,
                "regex": "^True$",
            },
            {
                "all": [{"field": "admin", "equals": True}],
                "not": {"field": "admin", "equals": False},
            },
        ],
    )
    def test_malformed_not_filter_cannot_invert_failure_into_acceptance(self, nested):
        processor = WebhookRouteProcessor()

        assert not processor.filter_matches(
            {"not": nested},
            {"admin": True},
            "push",
            {},
        )

    def test_valid_not_filter_can_invert_an_exact_non_match(self):
        processor = WebhookRouteProcessor()

        assert processor.filter_matches(
            {"not": {"field": "admin", "equals": True}},
            {"admin": False},
            "push",
            {},
        )

    def test_not_equals_requires_the_guarded_field_to_exist(self):
        processor = WebhookRouteProcessor()

        assert not processor.filter_matches(
            {"field": "role", "not_equals": "admin"},
            {},
            "push",
            {},
        )

    @pytest.mark.parametrize("expected", [None, "false", 0, 1])
    def test_exists_filter_requires_a_boolean(self, expected):
        processor = WebhookRouteProcessor()

        assert not processor.filter_matches(
            {"field": "admin", "exists": expected},
            {"admin": True},
            "push",
            {},
        )

    @pytest.mark.parametrize("filters", [None, "", {}, 0, False])
    def test_explicit_malformed_filters_do_not_mean_allow_all(self, filters):
        processor = WebhookRouteProcessor()

        assert not processor.route_filters_match(
            {"filters": filters},
            {"admin": True},
            "push",
            {},
        )

    @pytest.mark.asyncio
    async def test_unsigned_observed_provider_header_is_not_filter_authority(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "events": ["push"],
                "filters": {
                    "field": "headers.X-GitHub-Event",
                    "equals": "push",
                },
            }
        })
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                json={"value": 1},
                headers={
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": "observed-only",
                },
            )
            payload = await response.json()

        assert response.status == 200
        assert payload["status"] == "ignored"
        assert payload["reason"] == "filter"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_event_headers_cannot_satisfy_route_authority(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "events": ["push"],
            }
        })
        adapter.handle_message = AsyncMock()
        headers = CIMultiDict()
        headers.add("Content-Type", "application/json")
        headers.add("X-GitHub-Event", "push")
        headers.add("X-GitHub-Event", "push")

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                data=b'{"value":1}',
                headers=headers,
            )

        assert response.status == 401
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()


class TestAuthenticatedGitHubEventBodyAuthority:
    @pytest.mark.asyncio
    async def test_signed_deployment_status_cannot_be_relabelled_as_check_run(self):
        secret = "github-check-run-body-secret"
        adapter = _adapter({
            "checks": {
                "secret": secret,
                "provider": "github",
                "events": ["check_run"],
            }
        })
        adapter.handle_message = AsyncMock()
        body = json.dumps(
            {
                "action": "created",
                "check_run": {"id": 401, "name": "deploy", "status": "queued"},
                "deployment": {"id": 301},
                "deployment_status": {"id": 302, "state": "pending"},
                "repository": {"id": 801, "full_name": "org/repo"},
                "sender": {"id": 901, "login": "octocat"},
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/checks",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": f"sha256={signature}",
                    "X-GitHub-Event": "check_run",
                },
            )
            response_body = await response.json()

        assert response.status == 401
        assert response_body == {"error": "Invalid authenticated webhook metadata"}
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signed_sub_issues_cannot_be_relabelled_as_issues(self):
        secret = "github-issues-body-secret"
        adapter = _adapter({
            "issues": {
                "secret": secret,
                "provider": "github",
                "events": ["issues"],
            }
        })
        adapter.handle_message = AsyncMock()
        body = json.dumps(
            {
                "action": "parent_issue_added",
                "issue": {"id": 601, "number": 3},
                "sub_issue": {"id": 602, "number": 4},
                "repository": {"id": 801, "full_name": "org/repo"},
                "sender": {"id": 901, "login": "octocat"},
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/issues",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": f"sha256={signature}",
                    # The captured sub_issues body is relabeled only here.
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "signed-sub-issues",
                },
            )
            response_body = await response.json()

        assert response.status == 401
        assert response_body == {"error": "Invalid authenticated webhook metadata"}
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signed_ping_cannot_be_relabelled_but_signed_pr_is_accepted(self):
        secret = "github-event-body-secret"
        adapter = _adapter({
            "pulls": {
                "secret": secret,
                "provider": "github",
                "events": ["pull_request"],
                "prompt": "Review PR #{number}: {pull_request.title}",
            }
        })
        adapter.handle_message = AsyncMock()

        ping_body = json.dumps(
            {
                "zen": "Keep it logically awesome.",
                "hook_id": 12345,
                "hook": {"id": 12345, "type": "Repository", "name": "web"},
                "repository": {"id": 801, "full_name": "org/repo"},
                "sender": {"id": 901, "login": "octocat"},
            },
            separators=(",", ":"),
        ).encode()
        pull_request_body = json.dumps(
            {
                "action": "opened",
                "number": 42,
                "pull_request": {
                    "id": 701,
                    "number": 42,
                    "state": "open",
                    "title": "Authenticated PR",
                },
                "repository": {"id": 801, "full_name": "org/repo"},
                "sender": {"id": 901, "login": "octocat"},
            },
            separators=(",", ":"),
        ).encode()

        def headers(body, delivery):
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
                # The attack changes only this unsigned header to pull_request.
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": delivery,
            }

        async with TestClient(TestServer(_app(adapter))) as client:
            relabelled = await client.post(
                "/webhooks/pulls",
                data=ping_body,
                headers=headers(ping_body, "signed-ping"),
            )
            relabelled_body = await relabelled.json()

            assert relabelled.status == 401
            assert relabelled_body == {
                "error": "Invalid authenticated webhook metadata"
            }
            assert adapter._operation_ledger.count() == 0
            adapter.handle_message.assert_not_awaited()

            legitimate = await client.post(
                "/webhooks/pulls",
                data=pull_request_body,
                headers=headers(pull_request_body, "signed-pull-request"),
            )
            legitimate_body = await legitimate.json()

        assert legitimate.status == 202
        assert legitimate_body["status"] == "accepted"
        assert legitimate_body["event"] == "pull_request"
        await asyncio.sleep(0.05)
        adapter.handle_message.assert_awaited_once()


class TestRouteScriptAuthority:
    def test_script_snapshot_read_is_bounded_at_one_byte_past_limit(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        script = scripts / "oversized.py"
        script.write_bytes(b"x" * (MAX_SCRIPT_SNAPSHOT_BYTES + 4096))
        processor = WebhookRouteProcessor()

        prepared, error = processor.prepare_route_script("oversized.py")

        assert prepared is None
        assert error == (
            f"script exceeds {MAX_SCRIPT_SNAPSHOT_BYTES} byte snapshot limit"
        )

    def test_snapshotted_python_script_preserves_file_and_argv_contract(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        script = scripts / "identity.py"
        script.write_text(
            "import json, sys\n"
            "print(json.dumps({'file': __file__, 'argv0': sys.argv[0]}))\n",
            encoding="utf-8",
        )
        processor = WebhookRouteProcessor()

        prepared, error = processor.prepare_route_script("identity.py")
        assert error is None
        assert prepared is not None

        result = processor.run_prepared_script(prepared, {})

        assert result.disposition is WebhookScriptDisposition.CONTINUE
        identifier = f"hermes-webhook-script:{prepared.source_sha256}.py"
        assert result.payload == {"file": identifier, "argv0": identifier}

    def test_prepared_script_executes_snapshotted_bytes_after_file_changes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        script = scripts / "transform.py"
        script.write_text("print('{\"version\": 1}')\n", encoding="utf-8")
        processor = WebhookRouteProcessor()

        prepared, error = processor.prepare_route_script("transform.py")
        assert error is None
        assert prepared is not None
        script.write_text("print('{\"version\": 2}')\n", encoding="utf-8")

        result = processor.run_prepared_script(prepared, {})
        assert result.disposition is WebhookScriptDisposition.CONTINUE
        assert result.payload == {"version": 1}

        substituted = processor.run_prepared_script(
            replace(prepared, source="print('substituted')\n"),
            {},
        )
        assert substituted.disposition is WebhookScriptDisposition.FAILED
        assert "snapshot digest" in str(substituted.error)


class TestHTTPComposition:
    @pytest.mark.asyncio
    async def test_unverified_provider_id_cannot_alias_distinct_bodies(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "github"}
        })
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture
        headers = {"X-GitHub-Delivery": "delivery-1"}
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            second = await client.post(
                "/webhooks/events", json={"value": 2}, headers=headers
            )
        assert first.status == second.status == 202
        await asyncio.sleep(0.05)
        assert len(captured) == 2
        assert len({event.source.chat_id for event in captured}) == 2

    @pytest.mark.asyncio
    async def test_authenticated_delivery_id_conflicts_on_different_body(self):
        secret = "shared-hermes-secret"
        delivery_id = "delivery-1"
        first_body = _serialize_payload("post_tool_call", {"value": 1}, delivery_id)
        second_body = _serialize_payload("post_tool_call", {"value": 2}, delivery_id)
        target = WebhookTarget(
            url="https://receiver.invalid/events",
            events=["post_tool_call"],
            secret=secret,
        )
        first_delivery = _build_delivery(
            "post_tool_call", target, first_body, delivery_id
        )
        second_delivery = _build_delivery(
            "post_tool_call", target, second_body, delivery_id
        )
        adapter = _adapter({"events": {"secret": secret, "provider": "hermes"}})
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events",
                data=first_delivery["body"],
                headers=first_delivery["headers"],
            )
            conflict = await client.post(
                "/webhooks/events",
                data=second_delivery["body"],
                headers=second_delivery["headers"],
            )
            conflict_body = await conflict.json()
        assert first.status == 202
        assert conflict.status == 409
        assert conflict_body["status"] == "conflict"
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_body_replay_identity_deduplicates_without_provider_id(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post("/webhooks/events", json={"value": 1})
            second = await client.post("/webhooks/events", json={"value": 1})
            first_body = await first.json()
            second_body = await second.json()
        assert first.status == second.status == 202
        assert first_body["deduplication"] == "local_bypass_body_sha256"
        assert second_body["status"] == "in_progress"
        await asyncio.sleep(0.05)
        assert len(captured) == 1
        assert captured[0].source.chat_id.startswith("webhook:default:events:generic:")

    @pytest.mark.asyncio
    async def test_tool_grants_are_snapshotted_at_admission(self, monkeypatch):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "toolsets": ["terminal"],
            }
        })
        admitted_profile_generation = adapter._current_profile_authority_generation(
            "default",
            route_name="events",
        )
        monkeypatch.setattr(
            adapter,
            "_resolve_admitted_toolsets",
            lambda route_config, source: ["terminal"],
        )
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
        assert response.status == 202
        await asyncio.sleep(0.05)
        source = captured[0].source
        adapter._routes["events"]["toolsets"] = ["web"]
        assert adapter.resolved_toolsets_for_source(source) == ["terminal"]
        authority = adapter._operation_ledger.lookup_session(source.chat_id)
        assert authority is not None
        assert dict(authority.grant_snapshot) == {
            "v": 1,
            "toolsets": ("terminal",),
            "profile_generation": admitted_profile_generation,
        }

    @pytest.mark.asyncio
    async def test_declared_provider_cannot_be_switched_by_valid_other_signature(self):
        adapter = _adapter({"events": {"secret": "secret", "provider": "github"}})
        body = b'{"value":1}'
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Gitlab-Token": "secret",
                },
            )
        assert response.status == 401

    @pytest.mark.asyncio
    async def test_failed_direct_effect_is_not_laundered_as_duplicate_success(
        self, monkeypatch
    ):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "deliver_only": True,
                "deliver": "telegram",
                "deliver_extra": {"chat_id": "target-chat"},
            }
        })
        target_adapter = SimpleNamespace(
            config=SimpleNamespace(home_channel=None),
            send=AsyncMock(
                return_value=SendResult(success=False, error="unknown effect")
            ),
        )
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={
                Platform.WEBHOOK: adapter,
                Platform.TELEGRAM: target_adapter,
            },
            _profile_adapters={},
            _authorization_adapter=lambda platform, profile: (
                target_adapter
                if platform is Platform.TELEGRAM and profile == "default"
                else None
            ),
        )
        grant_resolver = MagicMock(
            side_effect=AssertionError("direct delivery must not resolve agent grants")
        )
        monkeypatch.setattr(adapter, "_resolve_admitted_toolsets", grant_resolver)
        headers = {"X-GitHub-Delivery": "delivery-unknown"}
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            first_body = await first.json()
            retry = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            retry_body = await retry.json()
        assert first.status == 502
        assert first_body["status"] == "indeterminate"
        assert retry.status == 409
        assert retry_body["status"] == "indeterminate"
        target_adapter.send.assert_awaited_once()
        grant_resolver.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_execution_script_failure_releases_claim_for_safe_retry(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "script": "late.py",
            }
        })
        headers = {"X-GitHub-Delivery": "script-retry-1"}
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            first_body = await first.json()
            scripts = tmp_path / "scripts"
            scripts.mkdir()
            (scripts / "late.py").write_text("print('[SILENT]')\n", encoding="utf-8")
            retry = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            retry_body = await retry.json()
        assert first.status == 500
        assert first_body["status"] == "failed"
        assert retry.status == 200
        assert retry_body["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_started_script_failure_is_indeterminate_not_completed(
        self, tmp_path
    ):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "effectful.py").write_text(
            "print('{\"value\": 1}')\n", encoding="utf-8"
        )
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
                "script": "effectful.py",
            }
        })
        adapter._route_processor.run_prepared_script = MagicMock(
            return_value=WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script timed out after execution started",
            )
        )
        headers = {"X-GitHub-Delivery": "script-unknown-1"}
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            first_body = await first.json()
            retry = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            retry_body = await retry.json()
        assert first.status == 500
        assert first_body["status"] == "indeterminate"
        assert retry.status == 409
        assert retry_body["status"] == "indeterminate"
        adapter._route_processor.run_prepared_script.assert_called_once()

    @pytest.mark.asyncio
    async def test_strict_media_encoding_and_object_contract(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            unsupported = await client.post(
                "/webhooks/events",
                data=b"{}",
                headers={"Content-Type": "text/plain"},
            )
            array = await client.post("/webhooks/events", json=[1, 2])
            duplicate = await client.post(
                "/webhooks/events",
                data=b'{"value":1,"value":2}',
                headers={"Content-Type": "application/json"},
            )
        assert unsupported.status == 415
        assert array.status == 400
        assert duplicate.status == 400

        # aiohttp may reject unsupported encodings in its parser before a
        # handler is entered, so pin the adapter's own defense in depth at the
        # direct handler boundary as well.
        request = MagicMock()
        request.match_info = {"route_name": "events"}
        request.headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "br",
        }
        request.content_length = 2
        encoded = await adapter._handle_webhook(request)
        assert encoded.status == 415

    @pytest.mark.asyncio
    async def test_non_finite_json_is_malformed_body_not_auth_failure(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                data=b'{"amount":NaN}',
                headers={"Content-Type": "application/json"},
            )
            response_body = await response.json()
        assert response.status == 400
        assert response_body["error"] == "Cannot parse JSON body"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_signed_event_isolated_before_durable_admission(self):
        secret = "bounded-event-secret"
        adapter = _adapter({
            "events": {
                "secret": secret,
                "signature_mode": "generic_v1",
            }
        })
        adapter.handle_message = AsyncMock()

        def signed(payload):
            body = json.dumps(payload, separators=(",", ":")).encode()
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return body, {
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            }

        oversized_body, oversized_headers = signed({"type": "é" * 513})
        valid_body, valid_headers = signed({"type": "tick", "value": 1})
        async with TestClient(TestServer(_app(adapter))) as client:
            rejected = await client.post(
                "/webhooks/events",
                data=oversized_body,
                headers=oversized_headers,
            )
            rejected_payload = await rejected.json()
            healthy = await client.get("/health")
            accepted = await client.post(
                "/webhooks/events",
                data=valid_body,
                headers=valid_headers,
            )

        assert rejected.status == 400
        assert rejected_payload == {"error": "Invalid authenticated webhook payload"}
        assert healthy.status == 200
        assert accepted.status == 202
        assert adapter._accepting_webhooks is True
        assert adapter._operation_ledger.count() == 1
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bounded_legacy_v1_route_uses_explicit_selected_mode(self):
        secret = "legacy-secret"
        body = b'{"event_type":"tick"}'
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        adapter = _adapter({
            "events": {"secret": secret, "signature_mode": "generic_v1"}
        })
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": signature,
                    "X-Request-ID": "legacy-1",
                },
            )
        assert response.status == 202

    @pytest.mark.asyncio
    async def test_providerless_route_fails_closed_before_header_inference(self):
        secret = "route-secret"
        body = b'{"event_type":"tick"}'
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        adapter = _adapter({"events": {"secret": secret}})
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": signature,
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
            )
        assert response.status == 500
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_auth_bypass_fails_closed_on_public_host_at_request_gate(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter._host = "0.0.0.0"
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
        assert response.status == 403
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_signed_hermes_outbound_delivery_is_valid_inbound(self):
        secret = "shared-hermes-secret"
        delivery_id = "h-1"
        body = _serialize_payload(
            "post_tool_call", {"tool_name": "terminal"}, delivery_id
        )
        delivery = _build_delivery(
            "post_tool_call",
            WebhookTarget(
                url="https://receiver.invalid/events",
                events=["post_tool_call"],
                secret=secret,
            ),
            body,
            delivery_id,
        )
        adapter = _adapter({"events": {"secret": secret, "provider": "hermes"}})
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events",
                data=delivery["body"],
                headers=delivery["headers"],
            )
            first_body = await first.json()
            retry = await client.post(
                "/webhooks/events",
                data=delivery["body"],
                headers=delivery["headers"],
            )
            retry_body = await retry.json()
        assert first.status == 202
        assert first_body["event"] == "post_tool_call"
        assert first_body["deduplication"] == "authenticated_delivery"
        assert retry.status == 202
        assert retry_body["status"] == "in_progress"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("header", "attacker_value"),
        [
            ("X-Hermes-Delivery", "attacker-delivery"),
            ("X-Hermes-Event", "attacker-event"),
        ],
    )
    async def test_signed_hermes_body_rejects_mutated_unsigned_metadata_header(
        self, header, attacker_value
    ):
        secret = "shared-hermes-secret"
        delivery_id = "real-delivery"
        body = _serialize_payload("post_tool_call", {}, delivery_id)
        delivery = _build_delivery(
            "post_tool_call",
            WebhookTarget(
                url="https://receiver.invalid/events",
                events=["post_tool_call"],
                secret=secret,
            ),
            body,
            delivery_id,
        )
        headers = {**delivery["headers"], header: attacker_value}
        adapter = _adapter({"events": {"secret": secret, "provider": "hermes"}})
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events", data=delivery["body"], headers=headers
            )
        assert response.status == 401
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_header", ["X-Hermes-Delivery", "X-Hermes-Event"])
    async def test_signed_hermes_body_requires_matching_metadata_header(
        self, missing_header
    ):
        secret = "shared-hermes-secret"
        delivery_id = "real-delivery"
        body = _serialize_payload("post_tool_call", {}, delivery_id)
        delivery = _build_delivery(
            "post_tool_call",
            WebhookTarget(
                url="https://receiver.invalid/events",
                events=["post_tool_call"],
                secret=secret,
            ),
            body,
            delivery_id,
        )
        headers = {
            key: value
            for key, value in delivery["headers"].items()
            if key != missing_header
        }
        adapter = _adapter({"events": {"secret": secret, "provider": "hermes"}})
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events", data=delivery["body"], headers=headers
            )
        assert response.status == 401
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_signed_hermes_delivery_rejects_stale_body_timestamp(self):
        secret = "shared-hermes-secret"
        delivery_id = "stale-delivery"
        current = json.loads(_serialize_payload("post_tool_call", {}, delivery_id))
        current["timestamp"] = "2020-01-01T00:00:00Z"
        stale_body = json.dumps(current).encode("utf-8")
        delivery = _build_delivery(
            "post_tool_call",
            WebhookTarget(
                url="https://receiver.invalid/events",
                events=["post_tool_call"],
                secret=secret,
            ),
            stale_body,
            delivery_id,
        )
        adapter = _adapter({"events": {"secret": secret, "provider": "hermes"}})
        adapter.handle_message = AsyncMock()
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/events", data=delivery["body"], headers=delivery["headers"]
            )
        assert response.status == 401
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_agent_processing_stages_terminal_error_once(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "github",
            }
        })
        captured = []

        async def capture(event):
            captured.append(event)

        adapter.handle_message = capture
        headers = {"X-GitHub-Delivery": "agent-failed"}
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            await asyncio.sleep(0.05)
            event = captured[0]
            assert event.webhook_envelope.observed_delivery_id == "agent-failed"
            assert event.webhook_envelope.delivery_identity is None
            await adapter.on_processing_start(event)
            await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
            settled = adapter._operation_ledger.lookup_session(
                event.webhook_authority.session_key
            )
            retry = await client.post(
                "/webhooks/events", json={"value": 1}, headers=headers
            )
            retry_body = await retry.json()
        assert first.status == 202
        assert settled is not None
        assert settled.state is OperationState.SETTLED
        assert settled.target_state is TargetState.SUPPRESSED
        assert settled.delivery is not None
        assert settled.delivery.content == (
            "Webhook processing failed before a final response was produced."
        )
        assert dict(settled.delivery.carrier) == {
            "v": 1,
            "kind": "terminal_outcome",
            "outcome": "error",
        }
        assert retry.status == 200
        assert retry_body["status"] == "duplicate"


class TestRuntimeOwnershipAndFinalCarrier:
    @pytest.mark.asyncio
    async def test_final_send_invokes_staged_target_once_and_preserves_result(self):
        adapter = _adapter()
        _envelope, prepared = _prepare_owned_direct_operation(
            adapter,
            raw_body=b'{"case":"single-final-send"}',
            content="one final response",
        )
        assert adapter._operation_ledger.mark_running(prepared)
        invoke = AsyncMock(
            return_value=WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.CONFIRMED,
                message_id="confirmed-message-id",
            )
        )
        adapter._invoke_staged_target = invoke

        result = await adapter.send(
            prepared.session_key,
            "one final response",
            metadata={"notify": True},
        )

        invoke.assert_awaited_once()
        assert result.success is True
        assert result.message_id == "confirmed-message-id"
        assert result.retryable is False
        assert result.raw_response == {"webhook_settlement": "confirmed"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("route_profile", "expected_profile"),
        [(None, "default"), ("ops", "ops")],
    )
    async def test_named_single_profile_self_prefix_reaches_bound_route(
        self,
        monkeypatch,
        route_profile,
        expected_profile,
    ):
        from gateway.run import GatewayRunner

        route = {
            "secret": _INSECURE_NO_AUTH,
            "provider": "generic",
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "named-home-chat"},
        }
        if route_profile is not None:
            route["profile"] = route_profile
        adapter = _adapter({"events": route})
        target_adapter = SimpleNamespace(
            config=PlatformConfig(enabled=True),
        )
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {
            Platform.WEBHOOK: adapter,
            Platform.TELEGRAM: target_adapter,
        }
        runner._profile_adapters = {}
        runner._active_profile_name = lambda: "ops"
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        adapter.gateway_runner = runner
        adapter._resolve_admitted_toolsets = lambda *_args: []
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home",
            lambda profile: profile == "ops",
        )

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/p/ops/webhooks/events",
                json={"value": 1},
            )

        assert response.status == 202
        assert adapter._operation_ledger.count() == 1
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.source.profile == expected_profile

    @pytest.mark.asyncio
    async def test_saturated_unique_admission_has_no_false_retry_interval(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        adapter._operation_ledger.admit = MagicMock(
            return_value=AdmitResult(
                AdmitDisposition.SATURATED,
                saturation=AdmitSaturationReason.GLOBAL_STORAGE_LIMIT,
            )
        )

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
            payload = await response.json()

        assert response.status == 503
        assert payload == {
            "status": "unavailable",
            "error": "Durable webhook evidence capacity exhausted for global",
        }
        assert "Retry-After" not in response.headers
        assert adapter._global_ledger_saturated is True
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scope_saturation_does_not_claim_global_health_failure(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        adapter._operation_ledger.admit = MagicMock(
            return_value=AdmitResult(
                AdmitDisposition.SATURATED,
                saturation=AdmitSaturationReason.SCOPE_STORAGE_LIMIT,
            )
        )

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
            payload = await response.json()
            health = await client.get("/health")
            health_payload = await health.json()

        assert response.status == 503
        assert payload["error"].endswith("for route scope")
        assert "Retry-After" not in response.headers
        assert health.status == 200
        assert health_payload["accepting_webhooks"] is True
        assert adapter._global_ledger_saturated is False
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("terminal_outcome", "first_status", "remaining_rows"),
        [
            ("settle", 200, 1),
            ("release", 500, 0),
        ],
    )
    async def test_global_saturation_health_recovers_when_active_capacity_frees(
        self,
        monkeypatch,
        terminal_outcome,
        first_status,
        remaining_rows,
    ):
        routes = {
            name: {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
            for name in ("first", "second")
        }
        adapter = _adapter(routes, max_entries=1)
        adapter.handle_message = AsyncMock()
        filter_entered = threading.Event()
        release_filter = threading.Event()

        def blocking_filter(*_args, **_kwargs):
            filter_entered.set()
            assert release_filter.wait(2)
            if terminal_outcome == "settle":
                return False
            raise RuntimeError("injected pre-effect filter failure")

        monkeypatch.setattr(
            adapter._route_processor,
            "route_filters_match",
            blocking_filter,
        )

        async with TestClient(TestServer(_app(adapter))) as client:
            first_request = asyncio.create_task(
                client.post("/webhooks/first", json={"value": 1})
            )
            try:
                assert await asyncio.to_thread(filter_entered.wait, 1)
                saturated = await client.post(
                    "/webhooks/second",
                    json={"value": 2},
                )
                saturated_payload = await saturated.json()
                degraded = await client.get("/health")
                degraded_payload = await degraded.json()
            finally:
                release_filter.set()
            first = await asyncio.wait_for(first_request, timeout=2)
            healthy = await client.get("/health")
            healthy_payload = await healthy.json()

        assert saturated.status == 503
        assert saturated_payload["error"].endswith("for global")
        assert degraded.status == 503
        assert degraded_payload["status"] == "not_ready"
        assert first.status == first_status
        assert healthy.status == 200
        assert healthy_payload["status"] == "ready"
        assert healthy_payload["accepting_webhooks"] is True
        assert adapter._global_ledger_saturated is False
        assert adapter._operation_ledger.count() == remaining_rows
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permanent_global_proofs_keep_health_degraded_without_retry_hint(
        self,
    ):
        route_names = [f"proof-{index}" for index in range(4)]
        secrets = {name: f"permanent-proof-secret-{name}" for name in route_names}
        routes = {
            name: {
                "secret": secrets[name],
                "provider": "generic",
                "signature_mode": "generic_v1",
                "events": ["never-selected"],
            }
            for name in route_names
        }
        storage_limit = (
            MINIMUM_MAX_STORAGE_BYTES + 2 * _TOMBSTONE_STORAGE_RESERVATION_BYTES
        )
        adapter = _adapter(
            routes,
            max_entries=8,
            max_storage_bytes=storage_limit,
        )
        adapter.handle_message = AsyncMock()

        def signed_request(route_name, index):
            body = json.dumps(
                {"value": index},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            signature = hmac.new(
                secrets[route_name].encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
            return body, {
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            }

        async with TestClient(TestServer(_app(adapter))) as client:
            for index, route_name in enumerate(route_names[:3]):
                body, headers = signed_request(route_name, index)
                settled = await client.post(
                    f"/webhooks/{route_name}",
                    data=body,
                    headers=headers,
                )
                assert settled.status == 200

            overflow_body, overflow_headers = signed_request(route_names[3], 3)
            saturated = await client.post(
                f"/webhooks/{route_names[3]}",
                data=overflow_body,
                headers=overflow_headers,
            )
            saturated_payload = await saturated.json()
            health = await client.get("/health")
            payload = await health.json()

            replay_body, replay_headers = signed_request(route_names[0], 0)
            replay = await client.post(
                f"/webhooks/{route_names[0]}",
                data=replay_body,
                headers=replay_headers,
            )
            replay_payload = await replay.json()

        assert saturated.status == 503
        assert saturated_payload["error"].endswith("for global")
        assert health.status == 503
        assert payload["status"] == "not_ready"
        assert payload["platform"] == "webhook"
        assert payload["accepting_webhooks"] is False
        assert payload["problems"] == ["Durable webhook evidence capacity is exhausted"]
        assert "Retry-After" not in health.headers
        assert replay.status == 200
        assert replay_payload["status"] == "duplicate"
        assert adapter._operation_ledger.count() == 0
        assert adapter._operation_ledger.tombstone_count() == 3
        assert adapter._operation_ledger.storage_usage() == (
            3 * _TOMBSTONE_STORAGE_RESERVATION_BYTES,
            3,
            storage_limit,
        )
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("lifecycle_attribute", "lifecycle_value"),
        [
            ("_startup_restore_in_progress", True),
            ("_draining", True),
            ("_external_drain_active", True),
            ("_running", False),
        ],
    )
    async def test_runner_lifecycle_gate_rejects_before_durable_admission(
        self,
        lifecycle_attribute,
        lifecycle_value,
    ):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={Platform.WEBHOOK: adapter},
            _startup_restore_in_progress=False,
            _draining=False,
            _external_drain_active=False,
            _running=True,
        )
        setattr(runner, lifecycle_attribute, lifecycle_value)
        adapter.gateway_runner = runner
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})

        assert response.status == 503
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconnect_recovery_failure_closes_intake_and_degrades_health(self):
        from gateway.run import GatewayRunner

        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        adapter.recover_pending_operations = AsyncMock(
            side_effect=RuntimeError("injected recovery outage")
        )
        runner = SimpleNamespace(
            adapters={Platform.WEBHOOK: adapter},
            _startup_restore_in_progress=False,
            _running=True,
            _update_platform_runtime_status=MagicMock(),
        )
        adapter.gateway_runner = runner

        recovered = await GatewayRunner._recover_webhook_operations(
            runner,
            trigger="reconnect:webhook",
        )

        assert recovered == 0
        assert adapter._accepting_webhooks is False
        async with TestClient(TestServer(_app(adapter))) as client:
            health = await client.get("/health")
            health_payload = await health.json()
            response = await client.post("/webhooks/events", json={"value": 1})
        assert health.status == 503
        assert health_payload["status"] == "not_ready"
        assert health_payload["platform"] == "webhook"
        assert health_payload["accepting_webhooks"] is False
        assert health_payload["problems"] == ["Webhook intake is not active"]
        assert health.headers["Retry-After"] == "5"
        assert response.status == 503
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()
        runner._update_platform_runtime_status.assert_called_once_with(
            Platform.WEBHOOK.value,
            platform_state="retrying",
            error_code="webhook_recovery_failed",
            error_message="Durable webhook recovery failed",
        )

    @pytest.mark.asyncio
    async def test_transient_recovery_failure_retries_and_reopens_exact_adapter(
        self,
        monkeypatch,
    ):
        from gateway import run as gateway_run
        from gateway.run import GatewayRunner

        adapter = _adapter()
        adapter.recover_pending_operations = AsyncMock(
            side_effect=[RuntimeError("transient storage fault"), 0]
        )
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        runner._update_platform_runtime_status = MagicMock()
        adapter.gateway_runner = runner
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_INITIAL_SECONDS",
            0,
        )
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_MAX_SECONDS",
            0,
        )

        recovered = await runner._recover_webhook_operations(trigger="startup")
        retry_task = runner._webhook_recovery_retry_task
        assert recovered == 0
        assert adapter._accepting_webhooks is False
        assert retry_task is not None

        await retry_task
        await asyncio.sleep(0)

        assert adapter.recover_pending_operations.await_count == 2
        assert adapter.recover_pending_operations.await_args_list[1].kwargs == {
            "trigger": "retry"
        }
        assert adapter._accepting_webhooks is True
        assert runner._webhook_recovery_retry_task is None
        assert runner._background_tasks == set()
        assert runner._update_platform_runtime_status.call_args_list == [
            (
                (Platform.WEBHOOK.value,),
                {
                    "platform_state": "retrying",
                    "error_code": "webhook_recovery_failed",
                    "error_message": "Durable webhook recovery failed",
                },
            ),
            (
                (Platform.WEBHOOK.value,),
                {
                    "platform_state": "connected",
                    "error_code": None,
                    "error_message": None,
                    "needs_attention": False,
                    "retrying_since": None,
                },
            ),
        ]

        async with TestClient(TestServer(_app(adapter))) as client:
            health = await client.get("/health")
        assert health.status == 200

    @pytest.mark.asyncio
    async def test_sleeping_recovery_retry_stops_when_drain_begins(
        self,
        monkeypatch,
    ):
        """A retry waking inside shutdown cannot touch the ledger or reopen."""

        from gateway import run as gateway_run
        from gateway.run import GatewayRunner

        adapter = _adapter()
        adapter.recover_pending_operations = AsyncMock(
            side_effect=RuntimeError("transient storage fault")
        )
        adapter._accepting_webhooks = False
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        adapter.gateway_runner = runner

        sleep_entered = asyncio.Event()
        release_sleep = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(_delay):
            sleep_entered.set()
            await release_sleep.wait()

        monkeypatch.setattr(gateway_run.asyncio, "sleep", controlled_sleep)

        assert await runner._recover_webhook_operations(trigger="startup") == 0
        retry_task = runner._webhook_recovery_retry_task
        assert retry_task is not None
        await asyncio.wait_for(sleep_entered.wait(), timeout=1)

        runner._draining = True
        runner._running = False
        release_sleep.set()
        await asyncio.wait_for(retry_task, timeout=1)
        await real_sleep(0)

        assert adapter.recover_pending_operations.await_count == 1
        assert adapter._accepting_webhooks is False
        assert runner._webhook_recovery_retry_task is None
        assert runner._webhook_recovery_retry_adapter is None
        assert runner._background_tasks == set()

    @pytest.mark.asyncio
    async def test_inflight_recovery_cannot_reopen_after_drain_begins(self):
        """A recovery success that crosses the drain boundary stays fenced."""

        from gateway.run import GatewayRunner

        adapter = _adapter()
        adapter._accepting_webhooks = False
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def blocking_recovery(*, trigger):
            assert trigger == "reconnect:webhook"
            recovery_entered.set()
            await release_recovery.wait()
            return 0

        adapter.recover_pending_operations = blocking_recovery
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        adapter.gateway_runner = runner

        recovery = asyncio.create_task(
            runner._recover_webhook_operations(trigger="reconnect:webhook")
        )
        await asyncio.wait_for(recovery_entered.wait(), timeout=1)
        runner._draining = True
        runner._running = False
        release_recovery.set()

        assert await asyncio.wait_for(recovery, timeout=1) == 0
        assert adapter._accepting_webhooks is False
        assert runner._webhook_recovery_retry_task is None
        assert runner._background_tasks == set()

    @pytest.mark.asyncio
    async def test_replacement_adapter_replaces_stale_recovery_retry(
        self,
        monkeypatch,
    ):
        from gateway import run as gateway_run
        from gateway.run import GatewayRunner

        old_adapter = _adapter()
        old_adapter.recover_pending_operations = AsyncMock(
            side_effect=RuntimeError("old adapter storage fault")
        )
        replacement_adapter = _adapter()
        replacement_adapter.recover_pending_operations = AsyncMock(
            side_effect=[RuntimeError("replacement storage fault"), 0]
        )
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: old_adapter}
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        old_adapter.gateway_runner = runner
        replacement_adapter.gateway_runner = runner

        # Hold the old retry in its first sleep so replacement is
        # deterministic rather than scheduler-timing dependent.
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_INITIAL_SECONDS",
            3600,
        )
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_MAX_SECONDS",
            3600,
        )
        assert await runner._recover_webhook_operations(trigger="startup") == 0
        old_retry = runner._webhook_recovery_retry_task
        assert old_retry is not None
        assert runner._webhook_recovery_retry_adapter is old_adapter
        await asyncio.sleep(0)

        runner.adapters[Platform.WEBHOOK] = replacement_adapter
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_INITIAL_SECONDS",
            0,
        )
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_MAX_SECONDS",
            0,
        )
        assert (
            await runner._recover_webhook_operations(trigger="reconnect:webhook") == 0
        )
        replacement_retry = runner._webhook_recovery_retry_task

        assert replacement_retry is not None
        assert replacement_retry is not old_retry
        assert runner._webhook_recovery_retry_adapter is replacement_adapter

        await replacement_retry
        await asyncio.gather(old_retry, return_exceptions=True)
        await asyncio.sleep(0)

        assert old_retry.cancelled()
        assert old_adapter.recover_pending_operations.await_count == 1
        assert replacement_adapter.recover_pending_operations.await_count == 2
        assert replacement_adapter._accepting_webhooks is True
        assert runner._webhook_recovery_retry_task is None
        assert runner._webhook_recovery_retry_adapter is None
        assert runner._background_tasks == set()

    @pytest.mark.asyncio
    async def test_replacement_success_cancels_stale_adapter_retry(
        self,
        monkeypatch,
    ):
        from gateway import run as gateway_run
        from gateway.run import GatewayRunner

        old_adapter = _adapter()
        old_adapter.recover_pending_operations = AsyncMock(
            side_effect=RuntimeError("old adapter storage fault")
        )
        replacement_adapter = _adapter()
        replacement_adapter.recover_pending_operations = AsyncMock(return_value=0)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: old_adapter}
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        old_adapter.gateway_runner = runner
        replacement_adapter.gateway_runner = runner
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_INITIAL_SECONDS",
            3600,
        )
        monkeypatch.setattr(
            gateway_run,
            "_WEBHOOK_RECOVERY_RETRY_MAX_SECONDS",
            3600,
        )

        assert await runner._recover_webhook_operations(trigger="startup") == 0
        old_retry = runner._webhook_recovery_retry_task
        assert old_retry is not None
        await asyncio.sleep(0)

        runner.adapters[Platform.WEBHOOK] = replacement_adapter
        assert (
            await runner._recover_webhook_operations(trigger="reconnect:webhook") == 0
        )
        await asyncio.gather(old_retry, return_exceptions=True)
        await asyncio.sleep(0)

        assert old_retry.cancelled()
        assert replacement_adapter.recover_pending_operations.await_count == 1
        assert replacement_adapter._accepting_webhooks is True
        assert runner._webhook_recovery_retry_task is None
        assert runner._webhook_recovery_retry_adapter is None
        assert runner._background_tasks == set()

    @pytest.mark.asyncio
    async def test_startup_recovery_failure_releases_global_gate_but_health_stays_degraded(
        self,
    ):
        from gateway.run import GatewayRunner

        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        adapter.recover_pending_operations = AsyncMock(
            side_effect=RuntimeError("injected startup recovery outage")
        )
        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._startup_restore_in_progress = True
        runner._startup_restore_tasks = []
        runner._startup_restore_queue = []
        runner._drain_startup_restore_queue = AsyncMock(return_value=0)
        runner._running = True
        runner._update_platform_runtime_status = MagicMock()
        adapter.gateway_runner = runner

        await runner._finish_startup_with_webhook_recovery()

        assert runner._startup_restore_in_progress is False
        assert adapter._accepting_webhooks is False
        async with TestClient(TestServer(_app(adapter))) as client:
            health = await client.get("/health")
            health_payload = await health.json()
            response = await client.post("/webhooks/events", json={"value": 1})
        assert health.status == 503
        assert health_payload["status"] == "not_ready"
        assert response.status == 503
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()
        runner._update_platform_runtime_status.assert_called_once_with(
            Platform.WEBHOOK.value,
            platform_state="retrying",
            error_code="webhook_recovery_failed",
            error_message="Durable webhook recovery failed",
        )

    @pytest.mark.asyncio
    async def test_startup_recovery_finishes_before_global_intake_gate_opens(self):
        from gateway.run import GatewayRunner

        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def blocking_recovery(*, trigger):
            assert trigger == "startup"
            recovery_entered.set()
            await release_recovery.wait()
            return 0

        adapter.recover_pending_operations = blocking_recovery
        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._startup_restore_in_progress = True
        runner._startup_restore_tasks = []
        runner._startup_restore_queue = []
        runner._drain_startup_restore_queue = AsyncMock(return_value=0)
        adapter.gateway_runner = runner

        finish = asyncio.create_task(runner._finish_startup_with_webhook_recovery())
        await asyncio.wait_for(recovery_entered.wait(), timeout=1)
        async with TestClient(TestServer(_app(adapter))) as client:
            health = await client.get("/health")
            health_payload = await health.json()
            response = await client.post("/webhooks/events", json={"value": 1})
        assert health.status == 503
        assert health_payload["status"] == "not_ready"
        assert response.status == 503
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()
        assert runner._startup_restore_in_progress is True

        release_recovery.set()
        await asyncio.wait_for(finish, timeout=1)
        assert runner._startup_restore_in_progress is False
        assert adapter._accepting_webhooks is True
        async with TestClient(TestServer(_app(adapter))) as client:
            health = await client.get("/health")
            health_payload = await health.json()
        assert health.status == 200
        assert health_payload["status"] == "ready"
        assert health_payload["platform"] == "webhook"
        assert health_payload["accepting_webhooks"] is True

    @pytest.mark.asyncio
    async def test_runner_owned_reconnect_listener_stays_closed_through_recovery(self):
        from gateway.run import GatewayRunner

        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.handle_message = AsyncMock()
        runner = object.__new__(GatewayRunner)
        runner.adapters = {}
        runner._startup_restore_in_progress = False
        runner._running = True
        adapter.gateway_runner = runner
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def blocking_recovery(*, trigger):
            assert trigger == "reconnect:webhook"
            recovery_entered.set()
            await release_recovery.wait()
            return 0

        adapter.recover_pending_operations = blocking_recovery
        try:
            assert await adapter.connect(is_reconnect=True)
            assert adapter._accepting_webhooks is False
            runner.adapters[Platform.WEBHOOK] = adapter

            recovery = asyncio.create_task(
                runner._recover_webhook_operations(trigger="reconnect:webhook")
            )
            await asyncio.wait_for(recovery_entered.wait(), timeout=1)
            async with TestClient(TestServer(_app(adapter))) as client:
                response = await client.post(
                    "/webhooks/events",
                    json={"value": 1},
                )
            assert response.status == 503
            assert adapter._operation_ledger.count() == 0
            adapter.handle_message.assert_not_awaited()

            release_recovery.set()
            assert await asyncio.wait_for(recovery, timeout=1) == 0
            assert adapter._accepting_webhooks is True
        finally:
            release_recovery.set()
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_skill_resolution_runs_inside_the_routed_profile_scope(
        self,
        monkeypatch,
    ):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "profile": "ops",
                "prompt": "base prompt",
                "skills": ["ops-skill"],
                "deliver": "log",
            }
        })
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(
                multiplex_profiles=True,
                multiplex_profile_allowlist=None,
            ),
            adapters={Platform.WEBHOOK: adapter},
        )
        adapter._resolve_admitted_toolsets = lambda *_args: []
        active_profile = {"name": None}
        entered_profiles = []

        @contextmanager
        def profile_scope(source):
            previous = active_profile["name"]
            active_profile["name"] = source.profile
            entered_profiles.append(source.profile)
            try:
                yield
            finally:
                active_profile["name"] = previous

        adapter._profile_runtime_context = profile_scope
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda **_kwargs: [("ops", object())],
        )

        def get_skill_commands():
            assert active_profile["name"] == "ops"
            return {"/ops-skill": object()}

        def build_skill_invocation_message(command, *, user_instruction):
            assert active_profile["name"] == "ops"
            assert command == "/ops-skill"
            return f"ops:{user_instruction}"

        monkeypatch.setattr(
            "agent.skill_commands.get_skill_commands",
            get_skill_commands,
        )
        monkeypatch.setattr(
            "agent.skill_commands.build_skill_invocation_message",
            build_skill_invocation_message,
        )
        app = web.Application(client_max_size=adapter._max_body_bytes)
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}",
            adapter._handle_webhook,
        )

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/p/ops/webhooks/events",
                json={"value": 1},
            )
            assert response.status == 202

        pending = tuple(adapter._background_tasks)
        if pending:
            await asyncio.gather(*pending)
        event = adapter.handle_message.await_args.args[0]
        assert event.source.profile == "ops"
        assert event.text == "ops:base prompt"
        assert entered_profiles
        assert set(entered_profiles) == {"ops"}

    @pytest.mark.asyncio
    async def test_replaced_adapter_cannot_admit_through_stale_handler(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        adapter.gateway_runner = SimpleNamespace(
            adapters={Platform.WEBHOOK: object()},
        )
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})

        assert response.status == 503
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replacement_while_body_is_read_cannot_create_ledger_row(self):
        adapter = _adapter({
            "events": {"secret": _INSECURE_NO_AUTH, "provider": "generic"}
        })
        runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={Platform.WEBHOOK: adapter},
        )
        adapter.gateway_runner = runner
        adapter.handle_message = AsyncMock()
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        class BlockingRequest:
            match_info = {"route_name": "events"}
            headers = {"Content-Type": "application/json"}
            content_length = len(b'{"value":1}')

            async def read(self):
                read_started.set()
                await release_read.wait()
                return b'{"value":1}'

        task = asyncio.create_task(adapter._handle_webhook(BlockingRequest()))
        await asyncio.wait_for(read_started.wait(), timeout=1)
        runner.adapters[Platform.WEBHOOK] = object()
        release_read.set()
        response = await asyncio.wait_for(task, timeout=1)

        assert response.status == 503
        assert adapter._operation_ledger.count() == 0
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_webhook_listener_cannot_be_its_own_delivery_target(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "deliver_only": True,
                "deliver": "webhook",
                "deliver_extra": {"chat_id": "recursive"},
            }
        })
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={Platform.WEBHOOK: adapter},
        )

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})

        assert response.status == 503
        assert adapter._operation_ledger.count() == 0

    @pytest.mark.asyncio
    async def test_agent_final_is_one_replayable_text_carrier(self, tmp_path):
        attachment = tmp_path / "report.pdf"
        attachment.write_bytes(b"report")
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "run",
                "deliver": "log",
            }
        })
        adapter.config.typing_indicator = False
        adapter._message_handler = AsyncMock(
            return_value=f"Finished.\nMEDIA:{attachment}"
        )
        original_send = adapter.send
        adapter.send = AsyncMock(wraps=original_send)
        adapter.send_image = AsyncMock()
        adapter.send_document = AsyncMock()
        adapter.send_file = AsyncMock()
        adapter.send_multiple_images = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
            assert response.status == 202

        for _ in range(100):
            if not adapter._background_tasks:
                break
            await asyncio.sleep(0.01)

        adapter.send.assert_awaited_once()
        sent_content = adapter.send.await_args.kwargs["content"]
        assert adapter.send.await_args.kwargs["metadata"]["notify"] is True
        assert sent_content == (
            "Finished.\n\n⚠️ Local attachments were omitted from webhook delivery."
        )
        adapter.send_image.assert_not_awaited()
        adapter.send_document.assert_not_awaited()
        adapter.send_file.assert_not_awaited()
        adapter.send_multiple_images.assert_not_awaited()

        event = adapter._message_handler.await_args.args[0]
        authority = adapter._operation_ledger.lookup_session(event.source.chat_id)
        assert authority is not None
        assert authority.state is OperationState.SETTLED
        assert authority.target_state is TargetState.SUPPRESSED
        assert authority.delivery is not None
        assert authority.delivery.content == sent_content

    @pytest.mark.asyncio
    async def test_platform_final_uses_only_webhook_owned_delivery_ledger(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "run",
                "deliver": "telegram",
                "deliver_extra": {"chat_id": "target-chat"},
            }
        })
        adapter.config.typing_indicator = False
        adapter._message_handler = AsyncMock(return_value="one final response")
        target_adapter = SimpleNamespace(
            send=AsyncMock(
                return_value=SendResult(success=True, message_id="target-message")
            )
        )
        runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={
                Platform.WEBHOOK: adapter,
                Platform.TELEGRAM: target_adapter,
            },
            _authorization_adapter=lambda platform, profile: (
                target_adapter
                if platform is Platform.TELEGRAM and profile == "default"
                else adapter
                if platform is Platform.WEBHOOK and profile == "default"
                else None
            ),
            _redeliver_failed_obligations_for_platform=AsyncMock(return_value=0),
        )
        adapter.gateway_runner = runner

        with patch("gateway.delivery_ledger.record_obligation") as generic_record:
            async with TestClient(TestServer(_app(adapter))) as client:
                response = await client.post(
                    "/webhooks/events",
                    json={"value": 1},
                )
                assert response.status == 202

            pending = tuple(adapter._background_tasks)
            if pending:
                await asyncio.gather(*pending)

        target_adapter.send.assert_awaited_once_with(
            "target-chat",
            "one final response",
            metadata=None,
        )
        generic_record.assert_not_called()
        runner._redeliver_failed_obligations_for_platform.assert_not_awaited()
        event = adapter._message_handler.await_args.args[0]
        authority = adapter._operation_ledger.lookup_session(event.source.chat_id)
        assert authority is not None
        assert authority.state is OperationState.SETTLED
        assert authority.target_state is TargetState.CONFIRMED

    @pytest.mark.asyncio
    async def test_real_base_pipeline_failure_stages_terminal_error(self):
        adapter = _adapter({
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "prompt": "run",
                "deliver": "log",
            }
        })
        adapter.config.typing_indicator = False
        adapter._message_handler = AsyncMock(side_effect=RuntimeError("agent failed"))

        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post("/webhooks/events", json={"value": 1})
            assert response.status == 202

        pending = tuple(adapter._background_tasks)
        if pending:
            await asyncio.gather(*pending)

        event = adapter._message_handler.await_args.args[0]
        authority = adapter._operation_ledger.lookup_session(event.source.chat_id)
        assert authority is not None
        assert authority.state is OperationState.SETTLED
        assert authority.target_state is TargetState.SUPPRESSED
        assert authority.delivery is not None
        assert authority.delivery.content == (
            "Webhook processing failed before a final response was produced."
        )
        assert dict(authority.delivery.carrier) == {
            "v": 1,
            "kind": "terminal_outcome",
            "outcome": "error",
        }

    @pytest.mark.asyncio
    async def test_primary_reconnect_replacement_recovers_staged_target_once(self):
        """The primary watcher publishes B before one real durable recovery."""

        old_adapter = _adapter()
        replacement_adapter = _adapter()
        replacement_adapter._accepting_webhooks = False
        target_adapter = SimpleNamespace(
            send=AsyncMock(
                return_value=SendResult(success=True, message_id="recovered-once")
            )
        )
        envelope = _stage_replay_safe_platform_delivery(
            old_adapter,
            trace_id="primary-a-to-b-success",
        )
        runner = _runner_for_primary_webhook_reconnect(
            old_adapter,
            replacement_adapter,
            target_adapter,
        )
        real_recover = replacement_adapter.recover_pending_operations
        replacement_adapter.recover_pending_operations = AsyncMock(wraps=real_recover)
        real_sleep = asyncio.sleep
        sleep_calls = 0

        async def finish_after_one_pass(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                runner._running = False
            await real_sleep(0)

        with (
            patch.object(
                runner,
                "_create_adapter",
                return_value=replacement_adapter,
            ),
            patch.object(
                runner,
                "_connect_adapter_with_timeout",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "gateway.channel_directory.build_channel_directory",
                new=AsyncMock(return_value={"platforms": {}}),
            ),
            patch("asyncio.sleep", side_effect=finish_after_one_pass),
        ):
            await asyncio.wait_for(
                runner._platform_reconnect_watcher(),
                timeout=2,
            )

        pending = tuple(replacement_adapter._background_tasks)
        if pending:
            await asyncio.gather(*pending)

        assert runner.adapters[Platform.WEBHOOK] is replacement_adapter
        replacement_adapter.recover_pending_operations.assert_awaited_once_with(
            trigger="reconnect:webhook"
        )
        target_adapter.send.assert_awaited_once_with(
            "recovery-chat",
            "one reconnect recovery response",
            metadata=None,
        )
        restored = replacement_adapter._operation_ledger.lookup_session(
            envelope.session_key
        )
        assert restored is not None
        assert restored.state is OperationState.SETTLED
        assert restored.target_state is TargetState.CONFIRMED

    @pytest.mark.asyncio
    async def test_failed_disconnect_retirement_quarantines_exact_owner_until_recovery(
        self,
        tmp_path,
    ):
        """A same-process replacement fences A before opening B's intake."""

        from gateway.run import GatewayRunner

        routes = {
            "events": {
                "secret": _INSECURE_NO_AUTH,
                "provider": "generic",
                "deliver": "telegram",
                "deliver_extra": {"chat_id": "recovery-chat"},
            }
        }
        old_adapter = _adapter(routes)
        replacement_adapter = _adapter(routes)
        replacement_adapter._accepting_webhooks = False

        ready_body = b'{"case":"ready"}'
        ready_envelope, ready = _prepare_owned_direct_operation(
            old_adapter,
            raw_body=ready_body,
            content="recover ready once",
        )
        _delivery_envelope, delivery_ready = _prepare_owned_direct_operation(
            old_adapter,
            raw_body=b'{"case":"delivery-ready"}',
            content="recover delivery-ready once",
        )
        assert old_adapter._operation_ledger.mark_running(delivery_ready)
        delivery_ready = old_adapter._stage_exact_delivery(
            delivery_ready,
            "recover delivery-ready once",
            {"v": 1, "kind": "direct"},
        )
        _ambiguous_envelope, ambiguous = _prepare_owned_direct_operation(
            old_adapter,
            raw_body=b'{"case":"ambiguous"}',
            content="must not replay",
        )
        assert old_adapter._operation_ledger.mark_running(ambiguous)

        prior_owner = old_adapter._operation_ledger.instance_id
        with patch.object(
            old_adapter._operation_ledger,
            "retire_instance",
            side_effect=RuntimeError("injected retirement failure"),
        ):
            with pytest.raises(RuntimeError, match="injected retirement failure"):
                await old_adapter.disconnect()

        assert old_adapter._accepting_webhooks is False
        assert old_adapter.has_fatal_error
        assert old_adapter.fatal_error_code == "webhook_retirement_failed"

        # A different profile/physical ledger must not inherit this marker,
        # even though its replacement exists in the same Python process.
        unrelated_adapter = _adapter(routes)
        unrelated_adapter._operation_ledger = WebhookOperationLedger(
            tmp_path / "unrelated-profile" / "state.db"
        )
        unrelated_adapter._accepting_webhooks = False
        with patch.object(
            unrelated_adapter._operation_ledger,
            "retire_owner_instance",
            wraps=unrelated_adapter._operation_ledger.retire_owner_instance,
        ) as unrelated_retirement:
            assert (
                await unrelated_adapter.recover_pending_operations(
                    trigger="unrelated-profile"
                )
                == 0
            )
        unrelated_retirement.assert_not_called()

        sends_entered = 0
        all_sends_entered = asyncio.Event()
        release_sends = asyncio.Event()

        async def blocked_send(*_args, **_kwargs):
            nonlocal sends_entered
            sends_entered += 1
            if sends_entered == 2:
                all_sends_entered.set()
            await release_sends.wait()
            return SendResult(success=True, message_id=f"recovered-{sends_entered}")

        target_adapter = SimpleNamespace(send=AsyncMock(side_effect=blocked_send))
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {
            Platform.WEBHOOK: replacement_adapter,
            Platform.TELEGRAM: target_adapter,
        }
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._schedule_webhook_recovery_retry = MagicMock()
        replacement_adapter.gateway_runner = runner
        # A production reconnect publishes the replacement's immutable route
        # authority before intake can reopen.  Keep this composition fixture on
        # that same boundary: strict toolset authority must come from the
        # profile config, while the later ACTIVE retry remains an indexed
        # ledger result and performs no live-policy revalidation.
        (tmp_path / "config.yaml").write_text(
            "platform_toolsets:\n  webhook: []\n",
            encoding="utf-8",
        )
        replacement_adapter._bind_route_authentication_authorities(
            replacement_adapter._routes
        )

        real_retire_owner = replacement_adapter._operation_ledger.retire_owner_instance
        with patch.object(
            replacement_adapter._operation_ledger,
            "retire_owner_instance",
            side_effect=RuntimeError("injected retry failure"),
        ) as failed_retry:
            assert (
                await runner._recover_webhook_operations(trigger="reconnect:webhook")
                == 0
            )
        failed_retry.assert_called_once_with(prior_owner)
        assert replacement_adapter._accepting_webhooks is False
        runner._schedule_webhook_recovery_retry.assert_called_once_with(
            replacement_adapter
        )

        # The replacement listener exists for diagnostics, but no request can
        # cross admission while the exact prior-owner retirement still fails.
        before_count = replacement_adapter._operation_ledger.count()
        async with TestClient(TestServer(_app(replacement_adapter))) as client:
            health = await client.get("/health")
            blocked = await client.post(
                "/webhooks/events",
                data=ready_body,
                headers={"Content-Type": "application/json"},
            )
        assert health.status == 503
        assert blocked.status == 503
        assert replacement_adapter._operation_ledger.count() == before_count
        target_adapter.send.assert_not_awaited()

        with patch.object(
            replacement_adapter._operation_ledger,
            "retire_owner_instance",
            wraps=real_retire_owner,
        ) as successful_retry:
            assert await runner._recover_webhook_operations(trigger="retry") == 2
        successful_retry.assert_called_once_with(prior_owner)
        assert replacement_adapter._accepting_webhooks is True

        await asyncio.wait_for(all_sends_entered.wait(), timeout=1)
        # Recovery has claimed both replay-safe carriers, so an exact transport
        # retry may briefly observe in-progress, but it cannot start a second
        # effect and it cannot remain a permanent 202 after settlement.
        async with TestClient(TestServer(_app(replacement_adapter))) as client:
            active_duplicate = await client.post(
                "/webhooks/events",
                data=ready_body,
                headers={"Content-Type": "application/json"},
            )
        assert active_duplicate.status == 202

        release_sends.set()
        pending = tuple(replacement_adapter._background_tasks)
        if pending:
            await asyncio.gather(*pending)

        async with TestClient(TestServer(_app(replacement_adapter))) as client:
            settled_duplicate = await client.post(
                "/webhooks/events",
                data=ready_body,
                headers={"Content-Type": "application/json"},
            )
            settled_payload = await settled_duplicate.json()
        assert settled_duplicate.status == 200
        assert settled_payload["status"] == "duplicate"
        assert target_adapter.send.await_count == 2
        assert {call.args[1] for call in target_adapter.send.await_args_list} == {
            "recover ready once",
            "recover delivery-ready once",
        }

        recovered_ready = replacement_adapter._operation_ledger.lookup_session(
            ready_envelope.session_key
        )
        assert recovered_ready is not None
        assert recovered_ready.state is OperationState.SETTLED
        assert recovered_ready.target_state is TargetState.CONFIRMED
        assert recovered_ready.generation == ready.generation + 1
        recovered_delivery = replacement_adapter._operation_ledger.lookup_session(
            delivery_ready.session_key
        )
        assert recovered_delivery is not None
        assert recovered_delivery.state is OperationState.SETTLED
        assert recovered_delivery.target_state is TargetState.CONFIRMED
        assert recovered_delivery.generation == delivery_ready.generation + 1
        preserved_ambiguous = replacement_adapter._operation_ledger.lookup_session(
            ambiguous.session_key
        )
        assert preserved_ambiguous is not None
        assert preserved_ambiguous.state is OperationState.INDETERMINATE

        # The exact marker was cleared after its successful retry. Re-running
        # recovery neither retires A again nor schedules a duplicate carrier.
        with patch.object(
            replacement_adapter._operation_ledger,
            "retire_owner_instance",
            wraps=real_retire_owner,
        ) as no_second_retirement:
            assert (
                await replacement_adapter.recover_pending_operations(
                    trigger="idempotent-check"
                )
                == 0
            )
        no_second_retirement.assert_not_called()
        assert target_adapter.send.await_count == 2

    @pytest.mark.asyncio
    async def test_multiplex_replacement_uses_root_before_quarantine_recovery(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Profile A and B share one listener-owned root replay ledger."""

        from gateway.run import GatewayRunner

        root = tmp_path / "multiplex-root"
        profile_a = root / "profiles" / "alpha"
        profile_b = root / "profiles" / "beta"
        profile_a.mkdir(parents=True)
        profile_b.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile_a))
        root_state = root / "state.db"

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.adapters = {}
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._schedule_webhook_recovery_retry = MagicMock()
        runner._update_platform_runtime_status = MagicMock()
        runner._resolve_profile_home_for_source = lambda source: (
            root
            if (source.profile or "default") == "default"
            else root / "profiles" / str(source.profile)
        )

        old_adapter = _adapter()
        prior_instance = old_adapter._operation_ledger.instance_id
        assert old_adapter._operation_ledger.db_path == root_state
        old_adapter.gateway_runner = runner
        assert await old_adapter.connect()
        assert old_adapter._operation_ledger.db_path == root_state
        assert old_adapter._operation_ledger.instance_id == prior_instance
        assert (
            old_adapter._authentication_authority_ledger
            is old_adapter._operation_ledger
        )
        runner.adapters[Platform.WEBHOOK] = old_adapter

        ready_envelope, ready = _prepare_owned_direct_operation(
            old_adapter,
            raw_body=b'{"multiplex":"ready"}',
            content="root ready",
            target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        )
        delivery_envelope, delivery_ready = _prepare_owned_direct_operation(
            old_adapter,
            raw_body=b'{"multiplex":"delivery-ready"}',
            content="root delivery-ready",
            target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        )
        assert old_adapter._operation_ledger.mark_running(delivery_ready)
        delivery_ready = old_adapter._stage_exact_delivery(
            delivery_ready,
            "root delivery-ready",
            {"v": 1, "kind": "direct"},
        )

        with patch.object(
            old_adapter._operation_ledger,
            "retire_instance",
            side_effect=RuntimeError("injected multiplex retirement failure"),
        ):
            with pytest.raises(
                RuntimeError, match="injected multiplex retirement failure"
            ):
                await old_adapter.disconnect()

        monkeypatch.setenv("HERMES_HOME", str(profile_b))
        replacement = _adapter()
        replacement_instance = replacement._operation_ledger.instance_id
        assert replacement._operation_ledger.db_path == root_state
        replacement.gateway_runner = runner
        assert await replacement.connect(is_reconnect=True)
        assert replacement._operation_ledger.db_path == root_state
        assert replacement._operation_ledger.instance_id == replacement_instance
        assert (
            replacement._authentication_authority_ledger
            is replacement._operation_ledger
        )
        runner.adapters[Platform.WEBHOOK] = replacement

        real_retire_owner = replacement._operation_ledger.retire_owner_instance
        with patch.object(
            replacement._operation_ledger,
            "retire_owner_instance",
            wraps=real_retire_owner,
        ) as retried_retirement:
            assert (
                await runner._recover_webhook_operations(trigger="reconnect:webhook")
                == 2
            )
        retried_retirement.assert_called_once_with(prior_instance)
        assert replacement._accepting_webhooks is True

        pending = tuple(replacement._background_tasks)
        if pending:
            await asyncio.gather(*pending)

        recovered_ready = replacement._operation_ledger.lookup_session(
            ready_envelope.session_key
        )
        assert recovered_ready is not None
        assert recovered_ready.state is OperationState.SETTLED
        assert recovered_ready.target_state is TargetState.SUPPRESSED
        assert recovered_ready.generation == ready.generation + 1
        recovered_delivery = replacement._operation_ledger.lookup_session(
            delivery_envelope.session_key
        )
        assert recovered_delivery is not None
        assert recovered_delivery.state is OperationState.SETTLED
        assert recovered_delivery.target_state is TargetState.SUPPRESSED
        assert recovered_delivery.generation == delivery_ready.generation + 1

        await replacement.disconnect()

    @pytest.mark.parametrize(
        ("first_multiplex", "second_multiplex"),
        [(False, True), (True, False)],
    )
    def test_profile_mode_transition_preserves_signed_replay_proof(
        self,
        tmp_path,
        monkeypatch,
        first_multiplex,
        second_multiplex,
    ):
        """Toggling multiplexing cannot reopen a settled provider delivery."""

        root = tmp_path / "mode-root"
        profile_home = root / "profiles" / "alpha"
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")
        root_state = root / "state.db"
        secret = "stable-mode-transition-secret"
        raw_body = b'{"event_type":"tick","value":1}'
        signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        headers = CIMultiDict({
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        })

        def route_config(multiplex: bool) -> dict:
            route = {
                "secret": secret,
                "provider": "generic",
                "signature_mode": "generic_v1",
            }
            if multiplex:
                route["profile"] = "alpha"
            return route

        def build_adapter(multiplex: bool) -> WebhookAdapter:
            monkeypatch.setenv(
                "HERMES_HOME",
                str(root if multiplex else profile_home),
            )
            adapter = _adapter({"events": route_config(multiplex)})
            adapter.gateway_runner = SimpleNamespace(
                config=GatewayConfig(
                    multiplex_profiles=multiplex,
                    multiplex_profile_allowlist=["alpha"] if multiplex else None,
                ),
                _resolve_profile_home_for_source=lambda source: (
                    profile_home if source.profile == "alpha" else root
                ),
            )
            adapter._bind_route_authentication_authorities(adapter._routes)
            return adapter

        def signed_envelope(adapter: WebhookAdapter, multiplex: bool):
            bound_route = WebhookRouteConfig.bind(
                "events",
                route_config(multiplex),
                headers=headers,
                request_profile="alpha" if multiplex else "default",
            )
            request = SimpleNamespace(
                headers=headers,
                match_info={"route_name": "events"},
            )
            receipt = adapter._verify_signature_receipt(
                request,
                raw_body,
                secret,
                bound_route,
            )
            assert receipt is not None
            return WebhookEnvelope.from_receipt(
                receipt,
                raw_body=raw_body,
                media_type="application/json",
                authority_profile="alpha",
            )

        first = build_adapter(first_multiplex)
        assert first._operation_ledger.db_path == root_state
        envelope = signed_envelope(first, first_multiplex)
        assert envelope.authority_profile == "alpha"
        admitted = first._operation_ledger.admit(envelope)
        assert admitted.disposition is AdmitDisposition.ACCEPTED
        assert admitted.authority is not None
        assert first._operation_ledger.settle_no_effect(
            admitted.authority,
            "mode transition proof",
        )

        second = build_adapter(second_multiplex)
        assert second._operation_ledger.db_path == root_state
        replay_envelope = signed_envelope(second, second_multiplex)
        assert replay_envelope.authority_profile == "alpha"
        replay = second._operation_ledger.admit(replay_envelope)

        assert replay.disposition is AdmitDisposition.DUPLICATE
        assert replay.authority is not None
        assert replay.authority.state is OperationState.SETTLED
        assert second._operation_ledger.count() == 1

    @pytest.mark.asyncio
    async def test_primary_reconnect_shutdown_discards_b_without_recovery_effect(self):
        """If drain wins B's connect, A remains published and no target runs."""

        old_adapter = _adapter()
        replacement_adapter = _adapter()
        replacement_adapter._accepting_webhooks = False
        target_adapter = SimpleNamespace(
            send=AsyncMock(
                return_value=SendResult(success=True, message_id="must-not-send")
            )
        )
        envelope = _stage_replay_safe_platform_delivery(
            old_adapter,
            trace_id="primary-a-to-b-shutdown",
        )
        runner = _runner_for_primary_webhook_reconnect(
            old_adapter,
            replacement_adapter,
            target_adapter,
        )
        real_recover = replacement_adapter.recover_pending_operations
        replacement_adapter.recover_pending_operations = AsyncMock(wraps=real_recover)
        real_disconnect = replacement_adapter.disconnect
        replacement_adapter.disconnect = AsyncMock(wraps=real_disconnect)
        real_sleep = asyncio.sleep

        async def connect_finishes_during_drain(*_args, **_kwargs):
            runner._draining = True
            return True

        async def immediate_sleep(_delay):
            await real_sleep(0)

        with (
            patch.object(
                runner,
                "_create_adapter",
                return_value=replacement_adapter,
            ),
            patch.object(
                runner,
                "_connect_adapter_with_timeout",
                new=AsyncMock(side_effect=connect_finishes_during_drain),
            ),
            patch("asyncio.sleep", side_effect=immediate_sleep),
        ):
            await asyncio.wait_for(
                runner._platform_reconnect_watcher(),
                timeout=2,
            )

        assert runner.adapters[Platform.WEBHOOK] is old_adapter
        assert Platform.WEBHOOK in runner._failed_platforms
        replacement_adapter.disconnect.assert_awaited_once_with()
        replacement_adapter.recover_pending_operations.assert_not_awaited()
        target_adapter.send.assert_not_awaited()
        replayable = old_adapter._operation_ledger.lookup_session(envelope.session_key)
        assert replayable is not None
        assert replayable.state is OperationState.DELIVERY_READY
        assert replayable.target_state is TargetState.PENDING

    @pytest.mark.asyncio
    async def test_overlapping_recovery_triggers_invoke_one_staged_target(self):
        adapter = _adapter()
        target_adapter = SimpleNamespace(
            send=AsyncMock(return_value=SendResult(success=True, message_id="sent-1"))
        )
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=False),
            adapters={
                Platform.WEBHOOK: adapter,
                Platform.TELEGRAM: target_adapter,
            },
            _authorization_adapter=lambda platform, profile: (
                target_adapter
                if platform is Platform.TELEGRAM and profile == "default"
                else adapter
                if platform is Platform.WEBHOOK and profile == "default"
                else None
            ),
        )
        raw_body = b'{"value":1}'
        route = WebhookRouteConfig.bind(
            "recover",
            {"provider": "generic"},
            headers={},
            request_profile="default",
        )
        envelope = WebhookEnvelope.from_receipt(
            WebhookLocalBypassReceipt._issue(route, raw_body, {}),
            raw_body=raw_body,
            media_type="application/json",
            trace_id="recover-trace",
        )
        admitted = adapter._operation_ledger.admit(envelope)
        assert admitted.authority is not None
        source = adapter._source_for_envelope(envelope)
        prepared = adapter._operation_ledger.prepare(
            admitted.authority,
            event_snapshot={
                "v": 1,
                "mode": "direct",
                "text": "one durable response",
                "payload": {"value": 1},
                "message_id": envelope.delivery_id,
                "source": source.to_dict(),
            },
            target_snapshot={
                "v": 1,
                "kind": "platform",
                "profile": "default",
                "platform": "telegram",
                "chat_id": "chat-1",
            },
            grant_snapshot=_grant_snapshot(adapter, envelope),
        )
        assert adapter._operation_ledger.mark_running(prepared)
        staged = adapter._stage_exact_delivery(
            prepared,
            "one durable response",
            {"v": 1, "kind": "direct"},
        )
        assert staged.state is OperationState.DELIVERY_READY

        scheduled = await asyncio.gather(
            adapter.recover_pending_operations(trigger="startup"),
            adapter.recover_pending_operations(trigger="reconnect:webhook"),
        )
        pending = tuple(adapter._background_tasks)
        if pending:
            await asyncio.gather(*pending)

        assert scheduled == [1, 0]
        target_adapter.send.assert_awaited_once_with(
            "chat-1",
            "one durable response",
            metadata=None,
        )
        restored = adapter._operation_ledger.lookup_session(envelope.session_key)
        assert restored is not None
        assert restored.state is OperationState.SETTLED
        assert restored.target_state is TargetState.CONFIRMED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delivery_ready", [False, True])
    async def test_immediate_recovery_cancellation_relinquishes_replay_safe_work(
        self,
        delivery_ready,
    ):
        adapter = _adapter()
        raw_body = b'{"value":1}'
        route = WebhookRouteConfig.bind(
            "cancel-recovery",
            {"provider": "generic"},
            headers={},
            request_profile="default",
        )
        envelope = WebhookEnvelope.from_receipt(
            WebhookLocalBypassReceipt._issue(route, raw_body, {}),
            raw_body=raw_body,
            media_type="application/json",
            trace_id=f"cancel-recovery-{delivery_ready}",
        )
        admitted = adapter._operation_ledger.admit(envelope)
        assert admitted.authority is not None
        source = adapter._source_for_envelope(envelope)
        authority = adapter._operation_ledger.prepare(
            admitted.authority,
            event_snapshot={
                "v": 1,
                "mode": "direct",
                "text": "durable response",
                "payload": {"value": 1},
                "message_id": envelope.delivery_id,
                "source": source.to_dict(),
            },
            target_snapshot={"v": 1, "kind": "log", "profile": "default"},
            grant_snapshot=_grant_snapshot(adapter, envelope),
        )
        if delivery_ready:
            assert adapter._operation_ledger.mark_running(authority)
            authority = adapter._stage_exact_delivery(
                authority,
                "durable response",
                {"v": 1, "kind": "direct"},
            )

        # Simulate the owner disappearing so this adapter must claim recovery.
        assert adapter._operation_ledger.relinquish_recovery_claim(authority)
        assert await adapter.recover_pending_operations(trigger="first") == 1
        pending = tuple(adapter._background_tasks)
        assert len(pending) == 1
        pending[0].cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        replayable = adapter._operation_ledger.lookup_session(envelope.session_key)
        assert replayable is not None
        assert replayable.state is (
            OperationState.DELIVERY_READY if delivery_ready else OperationState.READY
        )
        assert replayable.owner_instance != adapter._operation_ledger.instance_id

        assert await adapter.recover_pending_operations(trigger="second") == 1
        pending = tuple(adapter._background_tasks)
        assert len(pending) == 1
        await asyncio.gather(*pending)

        restored = adapter._operation_ledger.lookup_session(envelope.session_key)
        assert restored is not None
        assert restored.state is OperationState.SETTLED
        assert restored.target_state is TargetState.SUPPRESSED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("callback_transition", "durable_outcome"),
        [
            pytest.param(
                "relinquish_recovery_claim",
                "exception",
                id="relinquish-exception",
            ),
            pytest.param(
                "relinquish_recovery_claim",
                "authority-loss",
                id="relinquish-authority-loss",
            ),
            pytest.param(
                "mark_indeterminate",
                "exception",
                id="indeterminate-exception",
            ),
            pytest.param(
                "mark_indeterminate",
                "authority-loss",
                id="indeterminate-authority-loss",
            ),
        ],
    )
    async def test_recovery_callback_durable_failure_fences_exact_current_owner(
        self,
        callback_transition,
        durable_outcome,
    ):
        adapter = _adapter()
        _envelope, authority = _prepare_owned_direct_operation(
            adapter,
            raw_body=(
                f'{{"transition":"{callback_transition}",'
                f'"outcome":"{durable_outcome}"}}'
            ).encode(),
            content="recovery callback fence",
            target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        )
        owner = adapter._operation_ledger.instance_id
        schedule_retry = MagicMock()
        update_status = MagicMock()
        adapter.gateway_runner = SimpleNamespace(
            _schedule_webhook_recovery_retry=schedule_retry,
            _update_platform_runtime_status=update_status,
        )
        assert adapter._accepting_webhooks is True
        assert _quarantined_retirement_owners(adapter._operation_ledger) == ()

        transition_effect = (
            {"side_effect": RuntimeError("injected durable callback failure")}
            if durable_outcome == "exception"
            else {"return_value": False}
        )

        async def fail_after_start() -> None:
            raise RuntimeError("injected recovery task failure")

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        try:
            with patch.object(
                adapter._operation_ledger,
                callback_transition,
                **transition_effect,
            ) as durable_transition:
                operation = (
                    wait_forever
                    if callback_transition == "relinquish_recovery_claim"
                    else fail_after_start
                )
                assert adapter._schedule_recovery_task(
                    authority,
                    "event recovery",
                    operation,
                )
                task = adapter._recovery_tasks_by_operation[authority.operation_id]
                if callback_transition == "relinquish_recovery_claim":
                    # Cancellation before the coroutine's first instruction
                    # exercises the callback's claim-handoff branch.
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # asyncio schedules task done callbacks with call_soon().
                await asyncio.sleep(0)

            assert durable_transition.call_count == 1
            assert durable_transition.call_args.args[0] == authority
            assert adapter._accepting_webhooks is False
            assert adapter._intake_is_authoritative("default") is False
            assert adapter._recovery_backlog_pending is True
            assert adapter._recovery_restart_dead_scan is True
            assert _quarantined_retirement_owners(adapter._operation_ledger) == (owner,)
            assert schedule_retry.call_count >= 1
            assert all(
                call.args == (adapter,) for call in schedule_retry.call_args_list
            )
            update_status.assert_called_once_with(
                Platform.WEBHOOK.value,
                platform_state="retrying",
                error_code="webhook_transition_failed",
                error_message="Durable webhook transition requires recovery",
            )
            assert authority.operation_id not in adapter._recovery_tasks_by_operation
            assert task not in adapter._background_tasks
        finally:
            _clear_quarantined_retirement_owner(
                adapter._operation_ledger,
                owner,
            )

    @pytest.mark.asyncio
    async def test_bounded_recovery_drains_multi_page_backlog_without_task_fanout(
        self,
    ):
        from gateway.run import GatewayRunner

        adapter = _adapter(max_entries=32)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        runner.adapters = {Platform.WEBHOOK: adapter}
        runner._startup_restore_in_progress = False
        runner._draining = False
        runner._external_drain_active = False
        runner._running = True
        runner._shutdown_event = asyncio.Event()
        runner._background_tasks = set()
        runner._webhook_recovery_retry_task = None
        runner._webhook_recovery_retry_adapter = None
        runner._schedule_webhook_recovery_retry = MagicMock()
        runner._update_platform_runtime_status = MagicMock()
        adapter.gateway_runner = runner
        adapter._accepting_webhooks = False

        staged_authorities = []
        for index in range(20):
            raw_body = json.dumps({"index": index}).encode()
            route = WebhookRouteConfig.bind(
                "backlog",
                {"provider": "generic"},
                headers={},
                request_profile="default",
            )
            envelope = WebhookEnvelope.from_receipt(
                WebhookLocalBypassReceipt._issue(route, raw_body, {}),
                raw_body=raw_body,
                media_type="application/json",
                trace_id=f"backlog-{index}",
            )
            admitted = adapter._operation_ledger.admit(envelope)
            assert admitted.authority is not None
            prepared = adapter._operation_ledger.prepare(
                admitted.authority,
                event_snapshot={
                    "v": 1,
                    "mode": "direct",
                    "text": f"backlog {index}",
                    "payload": {"index": index},
                    "message_id": envelope.delivery_id,
                    "source": adapter._source_for_envelope(envelope).to_dict(),
                },
                target_snapshot={
                    "v": 1,
                    "kind": "log",
                    "profile": "default",
                },
                grant_snapshot=_grant_snapshot(adapter, envelope),
            )
            assert adapter._operation_ledger.mark_running(prepared)
            staged = adapter._stage_exact_delivery(
                prepared,
                f"backlog {index}",
                {"v": 1, "kind": "direct"},
            )
            assert adapter._operation_ledger.relinquish_recovery_claim(staged)
            staged_authorities.append(staged)

        real_invoke = adapter._invoke_staged_target
        first_batch_release = asyncio.Event()
        invocation_count = 0

        async def blocked_first_batch(authority):
            nonlocal invocation_count
            invocation_count += 1
            await first_batch_release.wait()
            return await real_invoke(authority)

        adapter._invoke_staged_target = blocked_first_batch
        assert await runner._recover_webhook_operations(trigger="bounded-start") == 8
        assert len(adapter._recovery_tasks_by_operation) == 8
        assert adapter._accepting_webhooks is False
        assert adapter._recovery_backlog_pending is True

        first_batch_release.set()
        await asyncio.gather(*tuple(adapter._background_tasks))

        for turn in range(1, 10):
            await runner._recover_webhook_operations(trigger=f"bounded-{turn}")
            assert len(adapter._recovery_tasks_by_operation) <= 8
            pending = tuple(adapter._background_tasks)
            if pending:
                await asyncio.gather(*pending)
            if adapter._accepting_webhooks:
                break

        assert adapter._accepting_webhooks is True
        assert adapter._recovery_backlog_pending is False
        assert invocation_count == 20
        for staged in staged_authorities:
            restored = adapter._operation_ledger.lookup_session(staged.session_key)
            assert restored is not None
            assert restored.state is OperationState.SETTLED
            assert restored.target_state is TargetState.SUPPRESSED
