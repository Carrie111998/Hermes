"""Contract tests for the profile-gated scheduled inline-card path."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cron.physique_inline_card import MORNING_CARD, NUTRITION_TICK_CARD, SOURCE_REVIEW_CARD, launch_morning_card, launch_scheduled_card
from cron import scheduler


KST = ZoneInfo("Asia/Seoul")


class FakeCardTransport:
    """A no-network sender that records only the bounded card kind."""

    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.cards: list[str] = []

    def is_inline_card_enabled(self, card: str) -> bool:
        return self.enabled and card == MORNING_CARD

    def send_inline_card(self, card: str) -> bool:
        self.cards.append(card)
        return not self.fail


class AsyncFakeCardTransport:
    """Gateway-loop fake that proves the scheduler does not use stdout delivery."""

    def __init__(self) -> None:
        self.cards: list[str] = []
        self.success = False

    def is_inline_card_enabled(self, card: str) -> bool:
        return card in {MORNING_CARD, SOURCE_REVIEW_CARD, NUTRITION_TICK_CARD}

    async def send_inline_card(self, card: str) -> "AsyncFakeCardTransport":
        self.cards.append(card)
        self.success = True
        return self


def _morning() -> datetime:
    return datetime(2031, 2, 3, 8, 11, tzinfo=KST)


def test_launch_sends_exactly_one_card_after_persisting_daily_claim(tmp_path: Path) -> None:
    """Given enabled fake transport, when the same day launches twice, then one card exists."""
    transport = FakeCardTransport()

    first = launch_morning_card(tmp_path, transport, _morning())
    second = launch_morning_card(tmp_path, transport, _morning())

    assert first.sent is True
    assert second.duplicate is True
    assert transport.cards == [MORNING_CARD]
    assert (tmp_path / "data" / "inline-card-launch-claims" / "2031-02-03.claim").is_file()


def test_launch_claim_survives_sender_failure_without_automatic_retry(tmp_path: Path) -> None:
    """Given a post-claim failure, when cron retries, then no second automatic card is sent."""
    transport = FakeCardTransport(fail=True)

    failed = launch_morning_card(tmp_path, transport, _morning())
    retry = launch_morning_card(tmp_path, transport, _morning())

    assert failed.sent is False and failed.claimed is True
    assert retry.duplicate is True
    assert transport.cards == [MORNING_CARD]


def test_disabled_transport_is_silent_and_does_not_claim(tmp_path: Path) -> None:
    """Given disabled profile feature, when cron fires, then it neither sends nor consumes the day."""
    transport = FakeCardTransport(enabled=False)

    result = launch_morning_card(tmp_path, transport, _morning())

    assert result.disabled is True
    assert transport.cards == []
    assert not (tmp_path / "data" / "inline-card-launch-claims").exists()


def test_scheduler_intercepts_inline_card_before_stdout_delivery(monkeypatch, tmp_path: Path) -> None:
    """Given an inline-card job, when it runs, then the generic script/delivery route is untouched."""
    job = {"id": "card-job", "inline_card": MORNING_CARD}
    seen: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "save_job_output", lambda _job_id, _output: tmp_path / "out.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda _job_id, success, error: seen.append((success, error)))
    monkeypatch.setattr(scheduler, "run_job", lambda _job: (_ for _ in ()).throw(AssertionError("generic path")))

    processed = scheduler.run_one_job(job, adapters={}, loop=None)

    assert processed is True
    assert seen == [(True, None)]


def test_scheduler_uses_existing_gateway_loop_with_fake_inline_transport(tmp_path: Path) -> None:
    """Given a live loop fake, when scheduled, then a keyboard-capable card is sent once."""
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever)
    worker.start()
    transport = AsyncFakeCardTransport()
    try:
        result = launch_scheduled_card(tmp_path, transport, loop, _morning())
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        loop.close()

    assert result.sent is True
    assert transport.cards == [MORNING_CARD]


def test_source_review_scheduler_does_not_consume_the_morning_daily_claim(tmp_path: Path) -> None:
    """Given a review-card schedule, when it fires twice, then each delivery remains eligible."""
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever)
    worker.start()
    transport = AsyncFakeCardTransport()
    try:
        first = launch_scheduled_card(tmp_path, transport, loop, _morning(), SOURCE_REVIEW_CARD)
        second = launch_scheduled_card(tmp_path, transport, loop, _morning(), SOURCE_REVIEW_CARD)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        loop.close()

    assert first.sent is True and second.sent is True
    assert transport.cards == [SOURCE_REVIEW_CARD, SOURCE_REVIEW_CARD]
    assert not (tmp_path / "data" / "inline-card-launch-claims").exists()


def test_nutrition_tick_uses_the_live_adapter_without_a_global_day_claim(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever)
    worker.start()
    transport = AsyncFakeCardTransport()
    try:
        first = launch_scheduled_card(tmp_path, transport, loop, _morning(), NUTRITION_TICK_CARD)
        second = launch_scheduled_card(tmp_path, transport, loop, _morning(), NUTRITION_TICK_CARD)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        loop.close()

    assert first.sent is True and second.sent is True
    assert transport.cards == [NUTRITION_TICK_CARD, NUTRITION_TICK_CARD]
    assert not (tmp_path / "data" / "inline-card-launch-claims").exists()
