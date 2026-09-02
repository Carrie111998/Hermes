"""Tests for /heartbeat (hermes_cli/heartbeat.py)."""

import logging
import time

import pytest

from hermes_cli.heartbeat import (
    HeartbeatManager,
    HeartbeatState,
    MIN_INTERVAL_SECONDS,
    format_interval,
    load_heartbeat,
    migrate_heartbeat_to_session,
    parse_interval,
    save_heartbeat,
)


# ──────────────────────────────────────────────────────────────────────
# interval parsing
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10m", 600),
        ("every 10m", 600),
        ("2h", 7200),
        ("every 2 hours", 7200),
        ("1d", 86400),
        ("90 minutes", 5400),
        ("600s", 600),
    ],
)
def test_parse_interval_valid(text, expected):
    assert parse_interval(text) == expected


@pytest.mark.parametrize("text", ["", "banana", "check CI", "every", "m10"])
def test_parse_interval_not_an_interval(text):
    assert parse_interval(text) is None


def test_parse_interval_too_small_is_rejected():
    assert parse_interval("5s") == -1
    assert parse_interval("30s") == -1
    # Exactly the floor is allowed.
    assert parse_interval(f"{MIN_INTERVAL_SECONDS}s") == MIN_INTERVAL_SECONDS


def test_format_interval():
    assert format_interval(600) == "10m"
    assert format_interval(7200) == "2h"
    assert format_interval(86400) == "1d"
    assert format_interval(90) == "90s"


# ──────────────────────────────────────────────────────────────────────
# state + due logic
# ──────────────────────────────────────────────────────────────────────


def test_state_roundtrip():
    s = HeartbeatState(prompt="check CI", interval_seconds=600, created_at=time.time())
    loaded = HeartbeatState.from_json(s.to_json())
    assert loaded.prompt == "check CI"
    assert loaded.interval_seconds == 600
    assert loaded.status == "active"


def test_is_due_anchors_on_created_then_last_fired():
    now = time.time()
    s = HeartbeatState(prompt="p", interval_seconds=600, created_at=now)
    assert s.is_due(now + 1) is False
    assert s.is_due(now + 601) is True
    s.last_fired_at = now + 601
    assert s.is_due(now + 700) is False
    assert s.is_due(now + 1300) is True


def test_paused_never_due():
    now = time.time()
    s = HeartbeatState(prompt="p", interval_seconds=60, created_at=now - 3600, status="paused")
    assert s.is_due(now) is False


def test_render_prompt_contains_instruction_and_interval():
    s = HeartbeatState(prompt="check the deploy", interval_seconds=600)
    rendered = s.render_prompt()
    assert "check the deploy" in rendered
    assert "10m" in rendered
    assert "Heartbeat" in rendered


# ──────────────────────────────────────────────────────────────────────
# manager
# ──────────────────────────────────────────────────────────────────────


def test_manager_set_pause_resume_clear():
    mgr = HeartbeatManager(session_id="hb-lifecycle-sid")
    state = mgr.set("watch CI", 600)
    assert state.status == "active"
    assert mgr.is_active()

    mgr.pause()
    assert not mgr.is_active()
    assert mgr.has_heartbeat()

    mgr.resume()
    assert mgr.is_active()

    assert mgr.clear() is True
    assert not mgr.has_heartbeat()
    # Cleared rows don't resurrect on reload.
    assert load_heartbeat("hb-lifecycle-sid") is None


def test_manager_rejects_bad_input():
    mgr = HeartbeatManager(session_id="hb-bad-sid")
    with pytest.raises(ValueError):
        mgr.set("", 600)
    with pytest.raises(ValueError):
        mgr.set("ok", 5)


def test_manager_persists_across_instances():
    mgr = HeartbeatManager(session_id="hb-persist-sid")
    mgr.set("persisted prompt", 600)
    again = HeartbeatManager(session_id="hb-persist-sid")
    assert again.has_heartbeat()
    assert again.state.prompt == "persisted prompt"


def test_due_prompt_claims_then_confirm_records_fire_and_reanchors():
    mgr = HeartbeatManager(session_id="hb-due-sid")
    mgr.set("tick", 600)
    # Not due immediately after set.
    assert mgr.due_prompt() is None
    # Force due by rewinding the anchor.
    mgr.state.created_at = time.time() - 700
    prompt = mgr.due_prompt()
    assert prompt is not None and "tick" in prompt
    # Claiming the tick must NOT record a fire — delivery isn't confirmed yet.
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is not None
    assert mgr.confirm_delivery() is True
    assert mgr.state.fire_count == 1
    assert mgr.state.claimed_at is None
    # Immediately after confirmation it re-anchors — not due again.
    assert mgr.due_prompt() is None


def test_missed_ticks_coalesce():
    mgr = HeartbeatManager(session_id="hb-coalesce-sid")
    mgr.set("tick", 600)
    # Simulate 5 missed intervals: exactly ONE fire results.
    mgr.state.created_at = time.time() - 600 * 5 - 10
    assert mgr.due_prompt() is not None
    assert mgr.confirm_delivery() is True
    assert mgr.due_prompt() is None
    assert mgr.state.fire_count == 1


def test_fire_is_recorded_only_after_confirm_delivery():
    mgr = HeartbeatManager(session_id="hb-confirm-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    prompt = mgr.due_prompt()
    assert prompt is not None
    # Claimed but not fired: the persisted state must not lie about a
    # delivery that never happened.
    assert mgr.state.fire_count == 0
    assert mgr.state.last_delivered_at == 0.0
    assert mgr.confirm_delivery() is True
    assert mgr.state.fire_count == 1
    assert mgr.state.last_delivered_at > 0
    assert mgr.state.claimed_at is None


def test_inflight_claim_blocks_second_claim():
    mgr = HeartbeatManager(session_id="hb-inflight-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    assert mgr.due_prompt() is not None
    # An unconfirmed claim is in flight: overlapping polls must not
    # re-claim the same tick (no double-fire, no backlog pileup).
    assert mgr.due_prompt() is None
    assert mgr.state.fire_count == 0


def test_abandon_claim_counts_missed_and_keeps_tick_due(caplog):
    mgr = HeartbeatManager(session_id="hb-abandon-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    assert mgr.due_prompt() is not None
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        assert mgr.abandon_claim("input queue handoff failed") is True
    assert "input queue handoff failed" in caplog.text
    assert mgr.state.missed_count == 1
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is None
    # The tick was never delivered: it stays due and is re-claimed.
    assert mgr.due_prompt() is not None


def test_stale_claim_from_previous_process_warns_and_counts_missed(caplog):
    import hermes_cli.heartbeat as hb

    mgr = HeartbeatManager(session_id="hb-stale-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    # Simulate a claim made by a previous process that died before
    # confirming or abandoning it (crash between claim and handoff).
    # The claim must sit OUTSIDE the NTP skew tolerance band to read as
    # an orphan rather than a live-process claim.
    mgr.state.claimed_at = (
        hb._PROCESS_START_TS - hb._PROCESS_START_SKEW_TOLERANCE_SECONDS - 60
    )
    save_heartbeat("hb-stale-sid", mgr.state)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        assert mgr.due_prompt() is None
    assert "never confirmed" in caplog.text
    assert mgr.state.missed_count == 1
    assert mgr.state.claimed_at is None
    assert mgr.state.fire_count == 0
    # Next poll re-claims the still-due tick.
    assert mgr.due_prompt() is not None


def test_ntp_step_back_within_tolerance_keeps_claim_in_flight(caplog, monkeypatch):
    """A backwards NTP step must not misread live claims as orphans.

    Wall-clock ``_PROCESS_START_TS`` is captured at import. If NTP steps
    the clock backwards afterwards, a claim recorded by THIS process on
    the corrected clock can read slightly OLDER than the start marker.
    Inside the skew tolerance that must stay a live in-flight claim (no
    spurious "previous process died" warning, no missed_count bump);
    outside it, the orphan handling still applies (covered above).
    """
    import hermes_cli.heartbeat as hb

    # The process "started" 30s in the future of the claim: the import
    # happened on the old, ahead clock before the backwards step.
    monkeypatch.setattr(hb, "_PROCESS_START_TS", time.time() + 30)
    mgr = HeartbeatManager(session_id="hb-ntp-sid", claim_timeout_seconds=60)
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    mgr.state.claimed_at = time.time() - 10  # 40s < tolerance 120s
    save_heartbeat("hb-ntp-sid", mgr.state)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        assert mgr.due_prompt() is None
    assert "never confirmed" not in caplog.text
    assert mgr.state.missed_count == 0
    assert mgr.state.fire_count == 0
    # The claim stays in flight for the live process to resolve.
    assert mgr.state.claimed_at is not None


def test_resume_reanchors_instead_of_instant_fire():
    mgr = HeartbeatManager(session_id="hb-resume-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 3600
    mgr.pause()
    mgr.resume()
    assert mgr.due_prompt() is None


# ──────────────────────────────────────────────────────────────────────
# compression migration
# ──────────────────────────────────────────────────────────────────────


def test_migrate_heartbeat_to_session():
    save_heartbeat(
        "hb-parent-sid",
        HeartbeatState(prompt="carry me", interval_seconds=600, created_at=time.time()),
    )
    assert migrate_heartbeat_to_session("hb-parent-sid", "hb-child-sid") is True
    child = load_heartbeat("hb-child-sid")
    assert child is not None and child.prompt == "carry me"
    assert load_heartbeat("hb-parent-sid") is None


def test_migrate_noop_without_source():
    assert migrate_heartbeat_to_session("hb-none-a", "hb-none-b") is False
    assert migrate_heartbeat_to_session("same", "same") is False


# ──────────────────────────────────────────────────────────────────────
# claim timeout — a claimed tick that produces no turn must not hang
# silently (#92837 expectation #3)
# ──────────────────────────────────────────────────────────────────────


def test_claim_timeout_in_due_prompt_abandons_and_reclaims(caplog, monkeypatch):
    import hermes_cli.heartbeat as hb

    # Anchor "this process" far enough in the past that a 30s-old claim
    # reads as a live-process claim, not a stale previous-process one.
    monkeypatch.setattr(hb, "_PROCESS_START_TS", time.time() - 1000)

    mgr = HeartbeatManager(session_id="hb-timeout-sid", claim_timeout_seconds=10)
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    assert mgr.due_prompt() is not None  # claims the due tick
    assert mgr.state.claimed_at is not None

    # A live-process claim that produced no turn within the timeout
    # window must be abandoned loudly, counted missed, and the still-due
    # tick re-claimed on the same call instead of stalling silently.
    mgr.state.claimed_at = time.time() - 30
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        prompt = mgr.due_prompt()

    assert prompt is not None  # re-claimed: the tick stayed due
    assert mgr.state.missed_count == 1
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is not None  # fresh claim
    assert "no turn" in caplog.text


def test_fresh_claim_within_timeout_stays_in_flight():
    mgr = HeartbeatManager(session_id="hb-timeout-fresh-sid", claim_timeout_seconds=60)
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    assert mgr.due_prompt() is not None
    # The claim is only seconds old: overlapping polls must not touch it.
    assert mgr.due_prompt() is None
    assert mgr.state.missed_count == 0
    assert mgr.state.claimed_at is not None


def test_claim_timeout_resolves_from_config_defaults(monkeypatch):
    import hermes_cli.config as cfg_mod
    import hermes_cli.heartbeat as hb

    monkeypatch.setattr(
        cfg_mod, "load_config_readonly",
        lambda: {"heartbeat": {"claim_timeout_seconds": 42}},
    )
    mgr = HeartbeatManager(session_id="hb-cfg-sid")
    assert mgr.claim_timeout_seconds == 42.0

    # Without the config key (or with unreadable config) the documented
    # fallback applies — never a crash.
    monkeypatch.setattr(cfg_mod, "load_config_readonly", lambda: {})
    mgr2 = HeartbeatManager(session_id="hb-cfg-sid-2")
    assert mgr2.claim_timeout_seconds == hb._CLAIM_TIMEOUT_FALLBACK_SECONDS


# ──────────────────────────────────────────────────────────────────────
# CLI watchdog tick — confirm failures must never wedge the claim
# ──────────────────────────────────────────────────────────────────────


def test_cli_watchdog_tick_confirms_after_queueing():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)  # bypass __init__ (no full app needed)
    cli.session_id = "hb-cli-ok-sid"
    cli._heartbeat_manager = None
    cli._agent_running = False
    cli._voice_recording = False
    cli._voice_processing = False
    import queue

    cli._pending_input = queue.Queue()

    mgr = HeartbeatManager(session_id="hb-cli-ok-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    cli._heartbeat_manager = mgr

    cli._heartbeat_watchdog_tick()

    assert cli._pending_input.qsize() == 1  # prompt queued for the REPL
    assert mgr.state.fire_count == 1
    assert mgr.state.missed_count == 0
    assert mgr.state.claimed_at is None


def test_cli_confirm_delivery_failure_abandons_claim_instead_of_wedging():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "hb-cli-boom-sid"
    cli._heartbeat_manager = None
    cli._agent_running = False
    cli._voice_recording = False
    cli._voice_processing = False
    import queue

    cli._pending_input = queue.Queue()

    mgr = HeartbeatManager(session_id="hb-cli-boom-sid")
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - 700
    cli._heartbeat_manager = mgr

    def _boom():
        raise RuntimeError("persisted write failed")

    mgr.confirm_delivery = _boom  # type: ignore[method-assign]

    cli._heartbeat_watchdog_tick()

    # The prompt was queued but confirmation blew up: the claim must be
    # abandoned (not left in flight forever), so the next tick re-claims
    # the still-due interval.
    assert cli._pending_input.qsize() == 1
    assert mgr.state.fire_count == 0
    assert mgr.state.missed_count == 1
    assert mgr.state.claimed_at is None


def test_confirm_delivery_docstring_states_acceptance_boundary():
    """Review guard (#92837): confirm_delivery fires at the ACCEPTANCE
    boundary (turn START for the gateway), not at turn completion. Wording
    that says "consumed by a turn" invites someone to move the call
    post-turn and reintroduce stuck claims."""
    doc = HeartbeatManager.confirm_delivery.__doc__
    assert doc is not None
    assert "accepted into the live pipeline" in doc
    assert "consumed by a turn" not in doc

