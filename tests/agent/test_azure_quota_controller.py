import json
from datetime import datetime, timezone

import pytest

from agent.azure_quota_controller import (
    AzureQuotaController, AzureQuotaError, OUTPUT_CEILINGS,
    ceiling_from_headers, quota_identity, trusted_output_ceiling,
)
from agent.retry_utils import azure_retry_delay
from agent.azure_identity_adapter import _quota_payload


class Clock:
    def __init__(self): self.now = 1_000.0
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def controller(tmp_path, clock, **kw):
    return AzureQuotaController(tmp_path / "quota.db", hard_cap=1_000_000,
                                clock=clock.time, sleeper=clock.sleep, **kw)


def test_all_trusted_output_classes_never_widen():
    for name, (floor, cap) in OUTPUT_CEILINGS.items():
        assert trusted_output_ceiling(name) == cap
        assert trusted_output_ceiling(name, cap + 99_999) == cap
        assert trusted_output_ceiling(name, floor) == floor
    with pytest.raises(AzureQuotaError, match="untrusted_request_class"):
        trusted_output_ceiling("caller-invented", 99_999)


def test_final_native_send_owner_clamps_chat_and_responses_payloads():
    chat, _ = _quota_payload({"messages": [], "max_tokens": 99_999}, "title")
    responses, _ = _quota_payload({"input": [], "max_output_tokens": 99_999}, "primary")
    narrowed, _ = _quota_payload({"messages": [], "max_tokens": 12}, "title")
    assert chat["max_tokens"] == 256
    assert responses["max_output_tokens"] == 20_000
    assert narrowed["max_tokens"] == 12


def test_header_ceiling_reduces_and_malformed_is_ignored():
    assert ceiling_from_headers({"X-RateLimit-Limit-Tokens": "1000000"}, 900_000) == 800_000
    assert ceiling_from_headers({"x-ratelimit-limit-tokens": "500000"}, 900_000) == 400_000
    assert ceiling_from_headers({"x-ratelimit-limit-tokens": "nope"}, 900_000) is None


def test_terra_luna_are_distinct_and_bucket_uses_deployment():
    terra, tq = quota_identity("https://example.services.ai.azure.com", "terra-prod")
    luna, lq = quota_identity("https://example.services.ai.azure.com", "luna-prod")
    assert tq == "terra" and lq == "luna" and terra != luna
    assert "example" not in terra and "prod" not in luna


def test_atomic_admission_generation_replay_reconcile_and_privacy(tmp_path):
    clock = Clock(); ctl = controller(tmp_path, clock)
    payload = {"messages": [{"role": "user", "content": "private prompt"}], "tools": [{"secret": "key"}]}
    a = ctl.admit(base_url="https://private.services.ai.azure.com", deployment="terra-one",
                  request_class="title", requested_output=100, payload=payload, request_identity="same")
    assert a.generation == 1 and a.reserved_tokens >= 100
    with pytest.raises(AzureQuotaError, match="replay_refused"):
        ctl.admit(base_url="https://private.services.ai.azure.com", deployment="terra-one",
                  request_class="title", payload=payload, request_identity="same")
    ctl.reconcile(a, usage_tokens=42, headers={"x-ratelimit-limit-tokens": "500000"})
    with ctl._connect() as db:
        receipt = db.execute("select body from receipts").fetchone()[0]
        ceiling = db.execute("select ceiling from buckets").fetchone()[0]
    assert ceiling == 400_000
    assert "private prompt" not in receipt and "secret" not in receipt and "private.services" not in receipt
    assert json.loads(receipt)["no_fallback"] is True


def test_queue_wait_cancellation_and_stale_recovery(tmp_path):
    clock = Clock(); ctl = AzureQuotaController(tmp_path / "q.db", hard_cap=300,
        max_wait=1, stale_after=.2, clock=clock.time, sleeper=clock.sleep)
    first = ctl.admit(base_url="https://x.azure.com", deployment="terra", request_class="title",
                      requested_output=256, payload={}, request_identity="one")
    calls = 0
    def cancelled():
        nonlocal calls; calls += 1; return calls > 2
    with pytest.raises(AzureQuotaError, match="cancelled_pre_send"):
        ctl.admit(base_url="https://x.azure.com", deployment="terra", request_class="title",
                  requested_output=256, payload={}, request_identity="two", cancelled=cancelled)
    clock.now += 61
    second = ctl.admit(base_url="https://x.azure.com", deployment="terra", request_class="title",
                       requested_output=256, payload={}, request_identity="three")
    assert second.generation > first.generation
    ctl.reconcile(second, usage_tokens=None, cancelled=True)


def test_retry_precedence_and_five_attempt_lifecycle():
    assert azure_retry_delay({"retry-after-ms": "1500", "Retry-After": "9"}, 1)[0:2] == (1.5, "retry-after-ms")
    assert azure_retry_delay({"Retry-After": "3"}, 2)[0:2] == (3.0, "retry-after")
    date = "Thu, 01 Jan 2026 00:00:10 GMT"
    delay, reason = azure_retry_delay({"Retry-After": date}, 3, now=datetime(2026,1,1,tzinfo=timezone.utc))
    assert (delay, reason) == (10.0, "retry-after")
    delay, reason = azure_retry_delay({}, 4, jitter_fn=lambda *a, **k: 7)
    assert delay == 7 and reason == "exponential"
    with pytest.raises(ValueError, match="attempts_exhausted"):
        azure_retry_delay({}, 5)


def test_mutation_guards_fail_if_comparisons_are_weakened(tmp_path):
    clock = Clock(); ctl = controller(tmp_path, clock)
    with pytest.raises(AzureQuotaError, match="reservation_exceeds_hard_cap"):
        ctl.admit(base_url="https://x.azure.com", deployment="terra", request_class="primary",
                  payload={"blob": "x" * 2_000_000})
    with pytest.raises(AzureQuotaError, match="invalid_output_ceiling"):
        trusted_output_ceiling("title", True)
