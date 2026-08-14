from events.schema import Event, EventType, Priority
from events.routing_policy import Attention, classify, ACTION_REQUIRED, ALERTS


def _event(outcome: str) -> Event:
    return Event.create(
        event_type=EventType.MODEL_RATE_LIMITED,
        source="matcher",
        payload={
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "reason": "rate_limit",
            "detector": "runtime",
            "outcome": outcome,
            "fallback_provider": "openai-codex",
            "fallback_model": "gpt-5.6-sol",
            "resets_at": "",
            "diverted_calls": 1,
            "episode_opened_at": "2026-08-14T10:00:00Z",
        },
    )


def test_diverted_is_warn_on_alerts():
    route = classify(_event("diverted"))
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS
    assert route.wa_tier is None


def test_chain_exhausted_is_act_and_pages():
    route = classify(_event("chain_exhausted"))
    assert route.attention is Attention.ACT
    assert route.topic_key == ACTION_REQUIRED
    assert route.wa_tier is not None


def test_no_fallback_is_also_act():
    route = classify(_event("no_fallback"))
    assert route.attention is Attention.ACT
    assert route.topic_key == ACTION_REQUIRED


def test_recovered_is_info_and_silent():
    route = classify(_event("recovered"))
    assert route.attention is Attention.INFO
    assert route.topic_key == ALERTS
    assert route.wa_tier is None


import json
from unittest.mock import patch

import pytest


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Point rate-limit state at a temp file."""
    p = tmp_path / "rate_limit_state.json"
    monkeypatch.setattr(
        "events.rate_limit_signal._state_path", lambda: p
    )
    from events import rate_limit_signal
    rate_limit_signal.reset_state_cache()
    return p


def test_load_state_missing_file_returns_empty(state_file):
    from events.rate_limit_signal import _load_state
    assert _load_state() == {}


def test_save_then_load_roundtrip(state_file):
    from events.rate_limit_signal import _load_state, _save_state
    episode = {
        "provider": "deepseek", "model": "deepseek-v4-pro",
        "opened_at": "2026-08-14T10:00:00Z", "resets_at": "",
        "worst_outcome": "diverted", "alerted_level": "diverted",
        "diverted_calls": 3, "fallbacks_seen": ["openai-codex/gpt-5.6-sol"],
    }
    assert _save_state({"deepseek/deepseek-v4-pro": episode}) is True
    from events import rate_limit_signal
    rate_limit_signal.reset_state_cache()
    loaded = _load_state()
    assert loaded["deepseek/deepseek-v4-pro"]["diverted_calls"] == 3


def test_malformed_state_fails_open_to_empty(state_file):
    state_file.write_text("{not json at all", encoding="utf-8")
    from events import rate_limit_signal
    rate_limit_signal.reset_state_cache()
    assert rate_limit_signal._load_state() == {}


def test_unreadable_state_never_raises(state_file, monkeypatch):
    from events import rate_limit_signal
    rate_limit_signal.reset_state_cache()

    def _boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr("builtins.open", _boom)
    assert rate_limit_signal._load_state() == {}


# --- Extra fail-open / edge-case coverage beyond the brief's four tests.
# Kept only where it exercises behavior genuinely distinct from the tests
# above (e.g. a non-dict JSON top level, directory creation, overwrite
# semantics) and where it actually executes (no Windows-only skips).


class TestLoadState:
    """Additional load-state edge cases beyond the brief's four."""

    def test_load_non_dict_toplevel_returns_empty_dict(self, tmp_path):
        """Loading JSON that is not a dict at top level returns {}."""
        from events import rate_limit_signal
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(["list", "not", "dict"]), encoding="utf-8")

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()
            result = rate_limit_signal._load_state()
        assert result == {}


class TestSaveState:
    """Additional save-state edge cases beyond the brief's four."""

    def test_save_state_returns_false_when_write_fails(self, tmp_path, monkeypatch):
        """_save_state() returns False and does not raise when write fails."""
        from events import rate_limit_signal
        state_file = tmp_path / "state.json"
        state = {
            "provider/model": {
                "opened_at": "2026-08-14T10:00:00Z",
                "diverted_calls": 1,
            }
        }

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()

            # Mock os.fdopen to raise an exception (Windows-compatible)
            def _boom(fd, *a, **k):
                raise OSError("disk write failed")

            monkeypatch.setattr("os.fdopen", _boom)

            # The function should return False, not raise
            result = rate_limit_signal._save_state(state)

        assert result is False

    def test_save_creates_file_with_valid_json(self, tmp_path):
        """Saving state creates a valid, independently-parseable JSON file."""
        from events import rate_limit_signal
        state_file = tmp_path / "state.json"
        state = {
            "deepseek/deepseek-v4-pro": {
                "opened_at": "2026-08-14T10:00:00Z",
                "diverted_calls": 5,
            }
        }

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()
            result = rate_limit_signal._save_state(state)

        assert result is True
        assert state_file.exists()
        with open(state_file, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == state

    def test_save_overwrites_existing_file(self, tmp_path):
        """Saving state overwrites an existing state file (old keys dropped)."""
        from events import rate_limit_signal
        state_file = tmp_path / "state.json"
        old_state = {"old-provider/model": {"diverted_calls": 1}}
        state_file.write_text(json.dumps(old_state), encoding="utf-8")

        new_state = {"new-provider/model": {"diverted_calls": 2}}

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()
            result = rate_limit_signal._save_state(new_state)

        assert result is True
        with open(state_file, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == new_state
        assert "old-provider/model" not in loaded

    def test_save_creates_parent_directory(self, tmp_path):
        """Saving state creates parent directories if needed."""
        from events import rate_limit_signal
        state_file = tmp_path / "nested" / "dir" / "state.json"
        state = {"provider/model": {"diverted_calls": 1}}

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()
            result = rate_limit_signal._save_state(state)

        assert result is True
        assert state_file.exists()


class TestLoadSaveRoundTrip:
    """Additional roundtrip edge case beyond the brief's roundtrip test."""

    def test_empty_state_roundtrip(self, tmp_path):
        """Save and load empty state works correctly."""
        from events import rate_limit_signal
        state_file = tmp_path / "state.json"

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            rate_limit_signal.reset_state_cache()
            assert rate_limit_signal._save_state({}) is True
            loaded = rate_limit_signal._load_state()

        assert loaded == {}


def test_first_hit_always_alerts():
    from events.rate_limit_signal import _should_alert
    assert _should_alert(None, "diverted", "openai-codex/gpt-5.6-sol") is True


def test_repeat_same_outcome_is_silent():
    from events.rate_limit_signal import _should_alert
    ep = {
        "worst_outcome": "diverted", "alerted_level": "diverted",
        "fallbacks_seen": ["openai-codex/gpt-5.6-sol"],
    }
    assert _should_alert(ep, "diverted", "openai-codex/gpt-5.6-sol") is False


def test_worsening_to_chain_exhausted_alerts():
    from events.rate_limit_signal import _should_alert
    ep = {
        "worst_outcome": "diverted", "alerted_level": "diverted",
        "fallbacks_seen": ["openai-codex/gpt-5.6-sol"],
    }
    assert _should_alert(ep, "chain_exhausted", "") is True


def test_new_fallback_target_alerts():
    """A second model absorbing traffic means the first one also died."""
    from events.rate_limit_signal import _should_alert
    ep = {
        "worst_outcome": "diverted", "alerted_level": "diverted",
        "fallbacks_seen": ["openai-codex/gpt-5.6-sol"],
    }
    assert _should_alert(ep, "diverted", "anthropic/claude-opus-5") is True


def test_severity_never_downgrades():
    """Once ACT-level, a later diverted hit must not re-alert as if new."""
    from events.rate_limit_signal import _should_alert
    ep = {
        "worst_outcome": "chain_exhausted", "alerted_level": "chain_exhausted",
        "fallbacks_seen": [],
    }
    assert _should_alert(ep, "diverted", "") is False


def test_alert_decision_uses_alerted_level_not_worst_outcome():
    """Discriminating case: worst_outcome and alerted_level deliberately
    diverge, so this only passes if condition 2 reads alerted_level.

    Every other fixture in this file sets worst_outcome == alerted_level,
    so swapping the field read at rate_limit_signal.py:122 produces
    identical results on those tests -- zero regression protection against
    the exact bug this function exists to guard against. Do not "tidy"
    these two fields back into agreement; the divergence is the point.

    Here worst_outcome is already chain_exhausted (e.g. from a prior
    episode state) but the user was only ever ALERTED at diverted. A new
    chain_exhausted hit must alert because it's worse than what was
    alerted, even though it is not worse than what was already recorded
    as worst_outcome. fallback_key is "" so condition 3 (new fallback
    target) cannot supply a false pass.
    """
    from events.rate_limit_signal import _should_alert
    ep = {
        "worst_outcome": "chain_exhausted", "alerted_level": "diverted",
        "fallbacks_seen": ["openai-codex/gpt-5.6-sol"],
    }
    assert _should_alert(ep, "chain_exhausted", "") is True


class _FakeBus:
    def __init__(self):
        self.emitted = []

    def emit(self, *, event_type, source, payload, priority=None, **kw):
        self.emitted.append((event_type, source, payload, priority))
        return "evt-id"


def test_record_emits_on_first_hit(state_file):
    from events.rate_limit_signal import record
    bus = _FakeBus()
    assert record(provider="deepseek", model="deepseek-v4-pro",
                  reason="rate_limit", detector="runtime",
                  fallback_provider="openai-codex",
                  fallback_model="gpt-5.6-sol",
                  source_hint="matcher", bus=bus) is True
    assert len(bus.emitted) == 1
    et, source, payload, _ = bus.emitted[0]
    assert et is EventType.MODEL_RATE_LIMITED
    assert payload["provider"] == "deepseek"
    assert payload["fallback_model"] == "gpt-5.6-sol"
    assert payload["outcome"] == "diverted"
    assert payload["diverted_calls"] == 1


def test_record_coalesces_repeat_hits(state_file):
    from events.rate_limit_signal import record
    bus = _FakeBus()
    kw = dict(provider="deepseek", model="deepseek-v4-pro",
              reason="rate_limit", detector="runtime",
              fallback_provider="openai-codex",
              fallback_model="gpt-5.6-sol", bus=bus)
    for _ in range(200):
        record(**kw)
    assert len(bus.emitted) == 1, "200 hits must produce exactly one alert"


def test_record_realerts_on_chain_exhausted(state_file):
    from events.rate_limit_signal import record
    bus = _FakeBus()
    record(provider="deepseek", model="deepseek-v4-pro", reason="rate_limit",
           detector="runtime", fallback_provider="openai-codex",
           fallback_model="gpt-5.6-sol", bus=bus)
    record(provider="deepseek", model="deepseek-v4-pro", reason="rate_limit",
           detector="runtime", outcome="chain_exhausted", bus=bus)
    assert len(bus.emitted) == 2
    assert bus.emitted[1][2]["outcome"] == "chain_exhausted"


def test_record_counts_diverted_calls(state_file):
    from events.rate_limit_signal import record
    bus = _FakeBus()
    kw = dict(provider="deepseek", model="deepseek-v4-pro",
              reason="rate_limit", detector="runtime",
              fallback_provider="openai-codex",
              fallback_model="gpt-5.6-sol", bus=bus)
    for _ in range(5):
        record(**kw)
    record(**{**kw, "outcome": "chain_exhausted",
              "fallback_provider": "", "fallback_model": ""})
    assert bus.emitted[-1][2]["diverted_calls"] == 6


def test_clear_emits_recovered_and_closes_episode(state_file):
    from events.rate_limit_signal import record, clear, _load_state
    bus = _FakeBus()
    record(provider="deepseek", model="deepseek-v4-pro", reason="rate_limit",
           detector="runtime", fallback_provider="openai-codex",
           fallback_model="gpt-5.6-sol", bus=bus)
    assert clear(provider="deepseek", model="deepseek-v4-pro", bus=bus) is True
    assert bus.emitted[-1][2]["outcome"] == "recovered"
    assert _load_state() == {}


def test_clear_on_healthy_provider_is_a_noop(state_file):
    from events.rate_limit_signal import clear
    bus = _FakeBus()
    assert clear(provider="deepseek", model="deepseek-v4-pro", bus=bus) is False
    assert bus.emitted == []


def test_kill_switch_suppresses_all_emission(state_file, monkeypatch):
    from events.rate_limit_signal import record
    monkeypatch.setenv("HERMES_RATE_LIMIT_ALERTS", "0")
    bus = _FakeBus()
    assert record(provider="deepseek", model="deepseek-v4-pro",
                  reason="rate_limit", detector="runtime", bus=bus) is False
    assert bus.emitted == []


def test_record_never_raises_when_bus_explodes(state_file):
    from events.rate_limit_signal import record

    class _ExplodingBus:
        def emit(self, **kw):
            raise RuntimeError("bus is down")

    assert record(provider="deepseek", model="deepseek-v4-pro",
                  reason="rate_limit", detector="runtime",
                  bus=_ExplodingBus()) is False


def test_record_never_raises_when_state_write_fails(state_file, monkeypatch):
    from events import rate_limit_signal
    monkeypatch.setattr(rate_limit_signal, "_save_state",
                        lambda s: (_ for _ in ()).throw(OSError("nope")))
    bus = _FakeBus()
    assert rate_limit_signal.record(
        provider="deepseek", model="deepseek-v4-pro",
        reason="rate_limit", detector="runtime", bus=bus) in (True, False)


def test_failed_persist_does_not_swallow_a_real_escalation(state_file, monkeypatch):
    """Named risk (carried forward from Task 2 review): _load_state() caches
    the SAME dict object across calls, and record() does
    ``state = dict(_load_state())`` -- a SHALLOW copy. The nested episode
    dict is therefore still the cached object, so mutating it (e.g.
    ``episode["alerted_level"] = ...``) happens in place BEFORE
    ``_save_state`` is ever called.

    If ``_save_state`` then fails, the in-memory cache has already been
    advanced to reflect the escalation that was never actually persisted
    NOR emitted (the exception aborts record() before it reaches _emit).
    A later, otherwise-identical escalation must still get through once
    persistence recovers -- it must not be silently treated as
    "already alerted" because of a mutation that never made it to disk.
    """
    from events import rate_limit_signal
    from events.rate_limit_signal import record

    bus = _FakeBus()

    # Establish an open episode at "diverted", persisted successfully.
    assert record(provider="deepseek", model="deepseek-v4-pro",
                  reason="rate_limit", detector="runtime",
                  outcome="diverted", bus=bus) is True
    assert len(bus.emitted) == 1

    # A genuine escalation arrives while persistence is broken.
    real_save_state = rate_limit_signal._save_state
    monkeypatch.setattr(
        rate_limit_signal, "_save_state",
        lambda s: (_ for _ in ()).throw(OSError("disk full")),
    )
    record(provider="deepseek", model="deepseek-v4-pro", reason="rate_limit",
           detector="runtime", outcome="chain_exhausted", bus=bus)

    # Persistence recovers.
    monkeypatch.setattr(rate_limit_signal, "_save_state", real_save_state)

    # The same escalation hits again. If the in-memory cache was corrupted
    # by the failed attempt above, this will be silently coalesced away too
    # -- the escalation is then swallowed forever within this process.
    record(provider="deepseek", model="deepseek-v4-pro", reason="rate_limit",
           detector="runtime", outcome="chain_exhausted", bus=bus)

    escalation_outcomes = [e[2]["outcome"] for e in bus.emitted[1:]]
    assert "chain_exhausted" in escalation_outcomes, (
        "the chain_exhausted escalation was never delivered to the bus, "
        "even after persistence recovered on a subsequent identical call "
        "-- the failed _save_state call corrupted the shared in-memory "
        "episode cache before the failure was caught"
    )
