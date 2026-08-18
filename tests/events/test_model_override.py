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
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") == (True, "ok")
    assert list_overrides() == []
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") == (False, "not_found")


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
                          bus=bus) == (True, "ok")
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
                          bus=bus) == (False, "not_found")
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
                          bus=_ExplodingBus()) == (True, "ok")
    assert get_override("deepseek", "deepseek-v4-pro") is None


# ---------------------------------------------------------------------------
# Review finding: the "cleared" audit payload carried only the ORIGINAL
# setter's set_by, so it could not distinguish "the operator cleared their
# own override" from "someone else un-diverted traffic on their behalf".
# clear_override() now takes a keyword-only cleared_by, defaulted to "" so
# existing/pre-Task-7/8 callers are unaffected, and the payload records BOTH
# who set it and who cleared it as distinct fields.
# ---------------------------------------------------------------------------

def test_clear_records_who_cleared_it(ov):
    """The cleared payload must carry BOTH the original setter and the
    clearing actor as distinct fields -- that is the whole point of the
    audit trail (spec Sec:Containment: "who diverted what, when")."""
    from events.model_override import set_override, clear_override
    from events.schema import EventType
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="telegram:diego")
    bus = _FakeBus()
    assert clear_override(provider="deepseek", model="deepseek-v4-pro",
                          cleared_by="telegram:diego2", bus=bus) == (True, "ok")
    assert len(bus.emitted) == 1
    et, source, payload, _ = bus.emitted[0]
    assert et is EventType.MODEL_OVERRIDE_SET
    assert payload["set_by"] == "telegram:diego"
    assert payload["cleared_by"] == "telegram:diego2"
    assert payload["set_by"] != payload["cleared_by"]


def test_clear_without_actor_is_still_legible(ov):
    """When no cleared_by is supplied, the payload must not silently
    attribute the clear to the original setter -- it must be legible as
    'no actor was recorded', not indistinguishable from a real one."""
    from events.model_override import set_override, clear_override
    bus = _FakeBus()
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="telegram:diego")
    assert clear_override(provider="deepseek", model="deepseek-v4-pro",
                          bus=bus) == (True, "ok")
    _, _, payload, _ = bus.emitted[0]
    assert payload["set_by"] == "telegram:diego"
    assert payload["cleared_by"] != "telegram:diego"
    assert payload["cleared_by"] == "unknown"


class TestUnpersistedWrites:
    """I2: a failed disk write must never become an unrevocable ghost.

    Phase 1's ``_publish_unsaved`` semantics were mirrored here too
    faithfully. They are correct for a TELEMETRY store (keep counting
    locally, self-heals) and wrong for a CONTROL store whose entire value is
    cross-process: an override adopted in memory only makes Telegram answer
    "Diverted 6h", writes MODEL_OVERRIDE_SET to the audit trail and reroutes
    the gateway, while no cron sees it, ``hermes overrides list`` shows
    nothing, and ``hermes overrides clear`` says "Nothing matched" -- for the
    full 6h, with no revocation path short of a gateway restart. That is the
    direct inverse of the spec's "visible and reversible" containment
    requirement.

    This host has hit 0 free bytes on C:, so "the write failed" is not a
    hypothetical here.
    """

    def test_failed_save_returns_not_ok_with_a_reason(self, ov, monkeypatch):
        from events import model_override

        monkeypatch.setattr(model_override, "_save_store", lambda store: False)
        ok, reason = model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=6 * 3600, set_by="telegram:diego")

        assert ok is False
        assert reason and reason != "ok"
        assert "persist" in reason.lower()

    def test_failed_save_emits_no_audit_event(self, ov, monkeypatch):
        """An audit trail that records an override nothing can see or revoke
        is worse than no record at all."""
        from events import model_override

        monkeypatch.setattr(model_override, "_save_store", lambda store: False)
        bus = _FakeBus()
        ok, _ = model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=6 * 3600, set_by="telegram:diego", bus=bus)

        assert ok is False
        assert bus.emitted == []

    def test_failed_save_leaves_no_ghost_in_this_process(self, ov, monkeypatch):
        """The gateway must not route on an override no other process can
        see. Mutation check: restoring the _publish_unsaved() call makes this
        get_override return a record."""
        from events import model_override

        monkeypatch.setattr(model_override, "_save_store", lambda store: False)
        model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=6 * 3600, set_by="telegram:diego")

        assert model_override.get_override("deepseek", "deepseek-v4-pro") is None

    def test_unreliable_store_also_refuses_the_write(self, ov, monkeypatch):
        """The other half of the same branch: when the last read failed,
        writes are skipped entirely (``_store_reliable`` is False) -- that
        skip must fail the call, not silently succeed."""
        from events import model_override

        monkeypatch.setattr(model_override, "_store_reliable", lambda: False)
        bus = _FakeBus()
        ok, reason = model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=6 * 3600, set_by="telegram:diego", bus=bus)

        assert ok is False
        assert "persist" in reason.lower()
        assert bus.emitted == []
        assert model_override.get_override("deepseek", "deepseek-v4-pro") is None

    def test_successful_save_is_unchanged(self, ov):
        """THE CENTRAL INVARIANT: with a working disk, nothing about
        set_override changed."""
        from events import model_override

        bus = _FakeBus()
        ok, reason = model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=6 * 3600, set_by="telegram:diego", bus=bus)

        assert (ok, reason) == (True, "ok")
        assert len(bus.emitted) == 1
        rec = model_override.get_override("deepseek", "deepseek-v4-pro")
        assert rec["replacement_model"] == "gpt-5.6-sol"

    def test_failed_clear_reports_failure(self, ov, monkeypatch):
        """Inverted, same defect: telling the operator traffic is un-diverted
        while the record is still on disk (and still live in every other
        process) is the ghost in reverse."""
        from events import model_override

        model_override.set_override(
            provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex",
            replacement_model="gpt-5.6-sol",
            ttl_seconds=3600, set_by="telegram:diego")
        assert model_override.get_override("deepseek", "deepseek-v4-pro")

        monkeypatch.setattr(model_override, "_save_store", lambda store: False)
        bus = _FakeBus()
        ok, reason = model_override.clear_override(
            provider="deepseek", model="deepseek-v4-pro",
            cleared_by="cli:diego", bus=bus)
        assert ok is False
        # N1: a persistence failure must be DISTINGUISHABLE from "nothing
        # was there to remove" -- collapsing both into one signal is
        # exactly the lying-revocation defect this test guards against.
        # Mutation check: swap this for == "not_found" and the assertion
        # still passes on a naive `bool(reason)`-only signal, but fails
        # here because "not_found" != CLEAR_PERSIST_FAILURE_REASON.
        assert reason == model_override.CLEAR_PERSIST_FAILURE_REASON
        assert reason != "not_found"
        assert bus.emitted == []
        # Still there -- which is the truth on disk.
        assert model_override.get_override("deepseek", "deepseek-v4-pro")


class TestStoreStatus:
    """I4: a corrupt override file must not be indistinguishable from
    "no overrides" for callers that REPORT state."""

    def test_readable_store_reports_readable(self, ov):
        from events import model_override

        status = model_override.store_status()
        assert status["readable"] is True
        assert status["path"]

    def test_corrupt_store_reports_unreadable(self, ov):
        from events import model_override

        ov.write_text("{not json at all", encoding="utf-8")
        model_override.reset_cache()

        # Routing callers still fail open -- that part must NOT change.
        assert model_override.get_override("deepseek", "deepseek-v4-pro") is None
        assert model_override.list_overrides() == []
        # ...but a reporting caller can now tell the difference.
        assert model_override.store_status()["readable"] is False

    def test_absent_store_is_readable_not_broken(self, ov):
        """A legitimately missing file is normal empty state, not a fault --
        otherwise every fresh install would warn."""
        from events import model_override

        assert not ov.exists()
        assert model_override.store_status()["readable"] is True

    def test_unreadable_store_warns_once_per_backoff_window(self, ov, monkeypatch, caplog):
        """WARNING, not debug -- but bounded by the existing read backoff, so
        a persistently broken file does not log on every single call."""
        import logging
        from events import model_override

        ov.write_text("{not json at all", encoding="utf-8")
        model_override.reset_cache()

        with caplog.at_level(logging.WARNING, logger="events.model_override"):
            for _ in range(5):
                model_override.get_override("deepseek", "deepseek-v4-pro")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, (
            "expected exactly one warning per backoff window, got %d" % len(warnings))
        assert "could not be read" in warnings[0].getMessage()
