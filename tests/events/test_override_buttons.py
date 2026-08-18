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
