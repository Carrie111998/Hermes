"""Tests for events.subscribers.scribe_realtime — per-event Scribe narration.

Regression focus (2026-07-18 operator report): critic_proposal narrations
were arriving in the critic_proposals Telegram topic clipped mid-sentence.
Real proposal summaries run ~128-135 chars (complete single sentences), but
the template truncated the summary at 120 and the module hard-capped every
rendered line at 160 — so every proposal lost its final clause. Telegram's
own hard limit is 4096, so the cap was a self-imposed narration constraint,
not a transport one. See events/subscribers/scribe_realtime.py.
"""

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.scribe_realtime import ScribeRealtime


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def _critic_event(summary: str, kind: str = "matcher.prompt_edit") -> Event:
    return Event(
        event_id="crit-1",
        event_type=EventType.CRITIC_PROPOSAL,
        source="critic.graph",
        timestamp="2026-07-18T11:31:00Z",
        priority=Priority.NORMAL,
        payload={"kind": kind, "summary": summary},
    )


def _notifications(bus):
    """Every NOTIFICATION mailbox_message emitted onto the bus, in order."""
    return [
        e for e in bus.query(event_type=EventType.MAILBOX_MESSAGE)
        if (e.payload or {}).get("message_type") == "NOTIFICATION"
    ]


def test_critic_proposal_narration_preserves_full_summary(bus):
    # Verbatim from the 2026-07-18 operator report — a 134-char summary that
    # was arriving cut at "...senior technical, data, product, and".
    summary = ("Prevent seniority and broad technical keywords from "
               "over-inflating industry_fit for senior technical, data, "
               "product, and credit roles.")
    assert len(summary) > 120  # guards the fixture against silent shrinkage

    ScribeRealtime(bus).handle(_critic_event(summary))

    notes = _notifications(bus)
    assert len(notes) == 1
    body = notes[0].payload["summary"]
    assert notes[0].payload["to"] == "critic_proposals"
    # The whole sentence — including its final clause — must survive.
    assert summary in body
    assert body.endswith("credit roles.")


def test_scribe_bounds_a_runaway_critic_summary(bus):
    # A pathological model summary must still be bounded well under Telegram's
    # 4096-char hard limit so one proposal can't flood the topic.
    ScribeRealtime(bus).handle(_critic_event("X" * 5000))

    body = _notifications(bus)[0].payload["summary"]
    assert len(body) <= 600


def test_non_critic_narration_stays_scannable(bus):
    # Non-critic narrations remain tight one-liners (≤160) — the scannable
    # feed contract is unchanged for everything except critic_proposal.
    ev = Event(
        event_id="job-1",
        event_type=EventType.JOB_HIGH_SCORE,
        source="matcher",
        timestamp="2026-07-18T11:31:00Z",
        priority=Priority.NORMAL,
        payload={"title": "T" * 200, "company": "C" * 200, "score": 95},
    )

    ScribeRealtime(bus).handle(ev)

    body = _notifications(bus)[0].payload["summary"]
    assert len(body) <= 160
