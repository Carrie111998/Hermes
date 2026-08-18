import pytest
from events.schema import Event, EventType


def _ev(outcome, detector="runtime", fallback="gpt-5.6-sol"):
    return Event.create(
        event_type=EventType.MODEL_RATE_LIMITED, source="matcher",
        payload={"provider": "deepseek", "model": "deepseek-v4-pro",
                 "reason": "rate_limit", "detector": detector,
                 "outcome": outcome, "fallback_provider": "openai-codex",
                 "fallback_model": fallback, "resets_at": "",
                 "diverted_calls": 3, "episode_opened_at": "x"})


def _labels(spec):
    """Flatten a button spec to its labels."""
    return [b["label"] for row in (spec or []) for b in row]


def test_diverted_offers_one_tap_naming_the_absorber():
    from events.override_buttons import buttons_for
    labels = _labels(buttons_for(_ev("diverted")))
    assert any("gpt-5.6-sol" in l for l in labels)
    assert any("Choose model" in l for l in labels)
    assert any("Dismiss" in l for l in labels)


def test_chain_exhausted_has_no_one_tap():
    """Every configured fallback is already limited — a one-tap here would
    divert into another dead model. Ruled 2026-08-14."""
    from events.override_buttons import buttons_for
    labels = _labels(buttons_for(_ev("chain_exhausted")))
    assert not any("Divert" in l for l in labels)
    assert any("Choose model" in l for l in labels)


def test_no_fallback_also_has_no_one_tap():
    from events.override_buttons import buttons_for
    assert not any("Divert" in l for l in _labels(buttons_for(_ev("no_fallback"))))


def test_recovered_has_no_buttons():
    from events.override_buttons import buttons_for
    assert buttons_for(_ev("recovered")) in (None, [])


@pytest.mark.parametrize("detector", ["credential_pool", "nous_guard", "usage_poller"])
def test_unroutable_detectors_get_no_buttons(detector):
    """Their episode keys are not real model slugs, so an override written
    from such a tap could never match. A control that does nothing is worse
    than no control. Ruled 2026-08-14."""
    from events.override_buttons import buttons_for
    assert buttons_for(_ev("diverted", detector=detector)) in (None, [])


def test_callback_data_stays_within_telegrams_64_byte_cap():
    from events.override_buttons import buttons_for
    for row in buttons_for(_ev("diverted")) or []:
        for b in row:
            assert len(b["callback_data"].encode("utf-8")) <= 64


def test_detector_and_outcome_are_case_and_whitespace_normalized():
    """MINOR 3: routing_policy.py and whatsapp_escalator.py both normalize
    payload["detector"]/["outcome"] with ``(x or "").strip().lower()`` before
    comparing. This module compared the raw values, so a producer that ever
    emitted "Runtime" or " diverted " would silently disagree with its
    siblings (they'd treat the event as actionable; this module would return
    no buttons). Align so they can't diverge."""
    from events.override_buttons import buttons_for

    event = _ev("diverted", detector="runtime")
    event.payload["detector"] = "  Runtime  "
    event.payload["outcome"] = " Diverted "
    assert buttons_for(event) is not None


def test_overlong_event_id_does_not_break_the_64_byte_cap():
    """MINOR 4: event_id is always a short uuid4 from Event.create, but
    Event.from_dict accepts whatever id a stored row carries. An unbounded
    token pushes callback_data past Telegram's 64-byte cap; InlineKeyboardButton
    builds fine on an oversized token and only fails later as a BadRequest at
    send_message -- which re-raises in _send_telegram and DROPS THE ENTIRE
    ALERT, not just the buttons. The bound must hold even for a pathological
    (thousands-of-characters) event_id."""
    import dataclasses

    from events.override_buttons import buttons_for

    event = _ev("diverted")
    huge = dataclasses.replace(event, event_id="x" * 5000)

    spec = buttons_for(huge)
    assert spec is not None
    for row in spec:
        for b in row:
            assert len(b["callback_data"].encode("utf-8")) <= 64
