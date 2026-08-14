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
