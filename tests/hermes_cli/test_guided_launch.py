"""Replay-safe one-shot guided Dashboard launch contracts."""

from __future__ import annotations

import hashlib
import threading

import pytest

from hermes_cli import guided_launch
from hermes_cli.guided_launch import (
    GuidedLaunchInvalid,
    _reset_for_tests,
    consume_guided_launch,
    mint_guided_launch,
)


@pytest.fixture(autouse=True)
def _reset_store():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _claim(**overrides):
    values = {
        "profile": "default",
        "conversation_id": "Bot Chat",
        "session_id": "20260822_120000_deadbeef",
        "board": "mission-control",
        "task_id": "t_afd09696",
        "brief": "Open the official form, fill known fields, stop before Submit.",
        "lease_id": "lease-123",
        "approval_surface": "http://100.65.87.91:9119/chat",
        "approval_decision": "approved",
        "approval_expires_at": 1_000_300,
        "lease_expires_at": 1_000_300,
        "expires_at": 1_000_300,
    }
    values.update(overrides)
    return values


def _selectors(claim):
    return {
        "profile": claim["profile"],
        "conversation_id": claim["conversation_id"],
        "session_id": claim["session_id"],
        "board": claim["board"],
        "task_id": claim["task_id"],
        "lease_id": claim["lease_id"],
        "brief_sha256": hashlib.sha256(claim["brief"].encode()).hexdigest(),
    }


def test_valid_contract_binds_context_and_is_single_use(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, minted = mint_guided_launch(**_claim())

    consumed = consume_guided_launch(token, **_selectors(minted))

    assert consumed == minted
    assert consumed["profile"] == "default"
    assert consumed["approval_surface"].endswith("/chat")
    with pytest.raises(GuidedLaunchInvalid, match="unknown or replayed"):
        consume_guided_launch(token, **_selectors(minted))


@pytest.mark.parametrize(
    ("field", "altered"),
    [
        ("profile", "link"),
        ("conversation_id", "another-chat"),
        ("session_id", "another-session"),
        ("board", "other-board"),
        ("task_id", "t_deadbeef"),
        ("lease_id", "lease-tampered"),
        ("brief_sha256", "0" * 64),
    ],
)
def test_any_presented_binding_tamper_burns_token_with_zero_action(monkeypatch, field, altered):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, minted = mint_guided_launch(**_claim())
    presented = _selectors(minted)
    presented[field] = altered

    with pytest.raises(GuidedLaunchInvalid, match="binding mismatch"):
        consume_guided_launch(token, **presented)
    with pytest.raises(GuidedLaunchInvalid, match="unknown or replayed"):
        consume_guided_launch(token, **_selectors(minted))


def test_expired_launch_and_lease_reject_with_zero_action(monkeypatch):
    clock = {"now": 1_000_000}
    monkeypatch.setattr(guided_launch.time, "time", lambda: clock["now"])
    token, minted = mint_guided_launch(**_claim())
    clock["now"] = 1_000_301

    with pytest.raises(GuidedLaunchInvalid, match="expired"):
        consume_guided_launch(token, **_selectors(minted))


@pytest.mark.parametrize("decision", ["denied", "pending"])
def test_deny_or_missing_approval_mints_no_launch(monkeypatch, decision):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    with pytest.raises(GuidedLaunchInvalid, match="approval"):
        mint_guided_launch(**_claim(approval_decision=decision))
    assert guided_launch._active_count_for_tests() == 0


def test_approval_timeout_mints_no_launch(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    with pytest.raises(GuidedLaunchInvalid, match="approval timeout"):
        mint_guided_launch(**_claim(approval_expires_at=999_999))
    assert guided_launch._active_count_for_tests() == 0


def test_wrong_profile_never_mints(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    with pytest.raises(GuidedLaunchInvalid, match="profile"):
        mint_guided_launch(**_claim(profile="link"))


def test_ttl_is_capped_at_five_minutes(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, minted = mint_guided_launch(
        **_claim(
            approval_expires_at=2_000_000,
            lease_expires_at=2_000_000,
            expires_at=2_000_000,
        )
    )
    assert token
    assert minted["expires_at"] == 1_000_300


def test_concurrent_consumers_start_at_most_one_turn(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, minted = mint_guided_launch(**_claim())
    successes = []
    failures = []
    lock = threading.Lock()

    def consume():
        try:
            result = consume_guided_launch(token, **_selectors(minted))
            with lock:
                successes.append(result)
        except GuidedLaunchInvalid as exc:
            with lock:
                failures.append(str(exc))

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(successes) == 1
    assert len(failures) == 7


def test_startup_prompt_visibly_contains_exact_bound_context(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, minted = mint_guided_launch(**_claim())
    consumed = consume_guided_launch(token, **_selectors(minted))

    prompt = guided_launch.guided_launch_prompt(consumed)

    assert "Board: mission-control" in prompt
    assert "Task: t_afd09696" in prompt
    assert "Conversation: Bot Chat" in prompt
    assert "Session: 20260822_120000_deadbeef" in prompt
    assert "Lease: lease-123" in prompt
    assert consumed["brief"] in prompt
    assert "exactly one turn" in prompt
    assert "Do not submit" in prompt
