import pytest

@pytest.fixture
def ov(tmp_path, monkeypatch):
    p = tmp_path / "model_overrides.json"
    monkeypatch.setattr("events.model_override._store_path", lambda: p)
    from events import model_override
    model_override.reset_cache()
    return p


def test_no_override_returns_none(ov):
    from events.model_override import get_override
    assert get_override("deepseek", "deepseek-v4-pro") is None


def test_set_then_get_roundtrip(ov):
    from events.model_override import set_override, get_override
    ok, _ = set_override(provider="deepseek", model="deepseek-v4-pro",
                         replacement_provider="openai-codex",
                         replacement_model="gpt-5.6-sol",
                         ttl_seconds=6 * 3600, set_by="telegram:diego")
    assert ok is True
    rec = get_override("deepseek", "deepseek-v4-pro")
    assert rec["replacement_model"] == "gpt-5.6-sol"


def test_expired_override_is_not_returned(ov):
    from events.model_override import set_override, get_override
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=-1, set_by="test")
    assert get_override("deepseek", "deepseek-v4-pro") is None


def test_ttl_is_capped_at_24h(ov):
    from events.model_override import set_override, get_override, MAX_TTL_SECONDS
    from datetime import datetime, timezone
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=99 * 3600, set_by="test")
    rec = get_override("deepseek", "deepseek-v4-pro")
    exp = datetime.fromisoformat(rec["expires_at"])
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    assert remaining <= MAX_TTL_SECONDS + 5
    # Lower bound matters as much as the upper one: an upper-bound-only
    # assertion passes just as happily if a regression clamps the TTL to a
    # few seconds, which would silently expire every override almost
    # immediately. Pin both sides so the cap is proven to CAP, not crush.
    assert remaining > MAX_TTL_SECONDS - 60


def test_self_target_is_rejected(ov):
    """Overriding a model to itself is a routing loop."""
    from events.model_override import set_override
    ok, reason = set_override(provider="deepseek", model="deepseek-v4-pro",
                              replacement_provider="deepseek",
                              replacement_model="deepseek-v4-pro",
                              ttl_seconds=3600, set_by="test")
    assert ok is False
    assert "itself" in reason.lower() or "loop" in reason.lower()


def test_malformed_store_fails_open(ov):
    ov.write_text("{not json", encoding="utf-8")
    from events import model_override
    model_override.reset_cache()
    assert model_override.get_override("deepseek", "deepseek-v4-pro") is None


def test_get_override_never_raises(ov, monkeypatch):
    from events import model_override
    model_override.reset_cache()
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert model_override.get_override("deepseek", "deepseek-v4-pro") is None


def test_clear_and_list(ov):
    from events.model_override import set_override, clear_override, list_overrides
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="test")
    assert len(list_overrides()) == 1
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") is True
    assert list_overrides() == []
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") is False


def test_target_with_an_open_episode_is_rejected(ov, monkeypatch):
    from events import model_override
    monkeypatch.setattr(
        "events.rate_limit_signal._load_state",
        lambda: {"openai-codex/gpt-5.6-sol": {"worst_outcome": "diverted"}},
    )
    ok, reason = model_override.set_override(
        provider="deepseek", model="deepseek-v4-pro",
        replacement_provider="openai-codex", replacement_model="gpt-5.6-sol",
        ttl_seconds=3600, set_by="test")
    assert ok is False
    assert "rate limited" in reason.lower() or "episode" in reason.lower()


def test_target_with_no_episode_is_accepted(ov, monkeypatch):
    from events import model_override
    monkeypatch.setattr("events.rate_limit_signal._load_state", lambda: {})
    ok, _ = model_override.set_override(
        provider="deepseek", model="deepseek-v4-pro",
        replacement_provider="openai-codex", replacement_model="gpt-5.6-sol",
        ttl_seconds=3600, set_by="test")
    assert ok is True


def test_unreadable_episode_state_does_not_veto_the_operator(ov, monkeypatch):
    """Fail-open is INVERTED here vs. everywhere else in this module: the
    state being read is telemetry, and the action being gated is the
    operator's deliberate control action. If the episode-state read blows
    up, the override must still be ACCEPTED -- a telemetry read must never
    veto an operator's explicit instruction. See the comment in
    set_override() for the full reasoning."""
    from events import model_override

    def _boom():
        raise OSError("boom")

    monkeypatch.setattr("events.rate_limit_signal._load_state", _boom)
    ok, _ = model_override.set_override(
        provider="deepseek", model="deepseek-v4-pro",
        replacement_provider="openai-codex", replacement_model="gpt-5.6-sol",
        ttl_seconds=3600, set_by="test")
    assert ok is True


# ---------------------------------------------------------------------------
# Task 5: MODEL_OVERRIDE_SET audit event -- every override write leaves a
# trail (spec Sec:Containment: "each write emits an event, so audit.jsonl
# records who diverted what, when"). Mirrors the injectable-bus pattern
# proven in tests/events/test_rate_limit_signal.py::_FakeBus.
# ---------------------------------------------------------------------------

class _FakeBus:
    def __init__(self):
        self.emitted = []

    def emit(self, *, event_type, source, payload, priority=None, **kw):
        self.emitted.append((event_type, source, payload, priority))
        return "evt-id"


class _ExplodingBus:
    def emit(self, *args, **kwargs):
        raise RuntimeError("bus is down")


def test_set_override_emits_audit_event_on_success(ov):
    from events.model_override import set_override
    from events.schema import EventType
    bus = _FakeBus()
    ok, _ = set_override(provider="deepseek", model="deepseek-v4-pro",
                         replacement_provider="openai-codex",
                         replacement_model="gpt-5.6-sol",
                         ttl_seconds=3600, set_by="telegram:diego",
                         bus=bus)
    assert ok is True
    assert len(bus.emitted) == 1
    et, source, payload, _ = bus.emitted[0]
    assert et is EventType.MODEL_OVERRIDE_SET
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["replacement_provider"] == "openai-codex"
    assert payload["replacement_model"] == "gpt-5.6-sol"
    assert payload["set_by"] == "telegram:diego"
    assert payload["action"] == "set"
    assert "expires_at" in payload


def test_set_override_does_not_emit_on_rejected_self_target(ov):
    """A rejected write (self-target routing loop) must not emit -- only a
    write that actually lands leaves a trail."""
    from events.model_override import set_override
    bus = _FakeBus()
    ok, _ = set_override(provider="deepseek", model="deepseek-v4-pro",
                         replacement_provider="deepseek",
                         replacement_model="deepseek-v4-pro",
                         ttl_seconds=3600, set_by="test", bus=bus)
    assert ok is False
    assert bus.emitted == []


def test_set_override_does_not_emit_on_rejected_open_episode(ov, monkeypatch):
    """A rejected write (divert-into-a-wall) must not emit either."""
    from events import model_override
    monkeypatch.setattr(
        "events.rate_limit_signal._load_state",
        lambda: {"openai-codex/gpt-5.6-sol": {"worst_outcome": "diverted"}},
    )
    bus = _FakeBus()
    ok, _ = model_override.set_override(
        provider="deepseek", model="deepseek-v4-pro",
        replacement_provider="openai-codex", replacement_model="gpt-5.6-sol",
        ttl_seconds=3600, set_by="test", bus=bus)
    assert ok is False
    assert bus.emitted == []


def test_clear_override_emits_audit_event_when_something_removed(ov):
    from events.model_override import set_override, clear_override
    from events.schema import EventType
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="test")
    bus = _FakeBus()
    assert clear_override(provider="deepseek", model="deepseek-v4-pro",
                          bus=bus) is True
    assert len(bus.emitted) == 1
    et, source, payload, _ = bus.emitted[0]
    assert et is EventType.MODEL_OVERRIDE_SET
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["action"] == "cleared"


def test_clear_override_does_not_emit_when_nothing_removed(ov):
    """clear_override on a key with no active override must not emit --
    nothing was actually diverted or un-diverted."""
    from events.model_override import clear_override
    bus = _FakeBus()
    assert clear_override(provider="deepseek", model="deepseek-v4-pro",
                          bus=bus) is False
    assert bus.emitted == []


def test_set_override_still_succeeds_when_bus_explodes(ov):
    """Fail-open: emission must never break the override write."""
    from events.model_override import set_override, get_override
    ok, _ = set_override(provider="deepseek", model="deepseek-v4-pro",
                         replacement_provider="openai-codex",
                         replacement_model="gpt-5.6-sol",
                         ttl_seconds=3600, set_by="test",
                         bus=_ExplodingBus())
    assert ok is True
    rec = get_override("deepseek", "deepseek-v4-pro")
    assert rec["replacement_model"] == "gpt-5.6-sol"


def test_clear_override_still_succeeds_when_bus_explodes(ov):
    """Fail-open: emission must never break the clear."""
    from events.model_override import set_override, clear_override, get_override
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="test")
    assert clear_override(provider="deepseek", model="deepseek-v4-pro",
                          bus=_ExplodingBus()) is True
    assert get_override("deepseek", "deepseek-v4-pro") is None
