"""Profile-gated scheduler delivery for the physique morning launcher card."""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Final, Protocol
from zoneinfo import ZoneInfo


MORNING_CARD: Final[str] = "physique-checkin-morning"
SOURCE_REVIEW_CARD: Final[str] = "physique-source-review"
NUTRITION_TICK_CARD: Final[str] = "nutrition-coaching-tick"
_KST: Final[ZoneInfo] = ZoneInfo("Asia/Seoul")


class InlineCardTransport(Protocol):
    """Minimal testable transport contract; values never leave the profile."""

    def is_inline_card_enabled(self, card: str) -> bool: ...

    def send_inline_card(self, card: str) -> bool: ...


class _AsyncCardResult(Protocol):
    """The narrow result shape required from the live Telegram adapter."""

    success: bool


class _AsyncCardAdapter(Protocol):
    """Existing adapter capability used by this scheduler path only."""

    def is_inline_card_enabled(self, card: str) -> bool: ...

    def send_inline_card(self, card: str) -> Awaitable[_AsyncCardResult]: ...


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """Observable outcome of a single automatic morning launch attempt."""

    claimed: bool = False
    sent: bool = False
    duplicate: bool = False
    disabled: bool = False


def launch_morning_card(home: Path, transport: InlineCardTransport, now: datetime) -> LaunchResult:
    """Claim one KST day before sending its bounded Start/Resume card once."""
    if not transport.is_inline_card_enabled(MORNING_CARD):
        return LaunchResult(disabled=True)
    claim_path = _claim_path(home, now)
    if not _claim(claim_path):
        return LaunchResult(duplicate=True)
    return LaunchResult(claimed=True, sent=transport.send_inline_card(MORNING_CARD))


def launch_source_review_card(transport: InlineCardTransport) -> LaunchResult:
    """Send one queue-backed review card whenever the profile has a pending candidate."""
    if not transport.is_inline_card_enabled(SOURCE_REVIEW_CARD):
        return LaunchResult(disabled=True)
    return LaunchResult(sent=transport.send_inline_card(SOURCE_REVIEW_CARD))


def launch_scheduled_card(
    home: Path,
    adapter: _AsyncCardAdapter | None,
    loop: asyncio.AbstractEventLoop | None,
    now: datetime,
    card: str = MORNING_CARD,
) -> LaunchResult:
    """Bridge a cron worker to the existing adapter without a second poller."""
    if adapter is None or loop is None:
        return LaunchResult(disabled=True)
    transport = _ScheduledTransport(adapter, loop)
    if card == MORNING_CARD:
        return launch_morning_card(home, transport, now)
    if card == SOURCE_REVIEW_CARD:
        return launch_source_review_card(transport)
    if card == NUTRITION_TICK_CARD:
        if not transport.is_inline_card_enabled(card):
            return LaunchResult(disabled=True)
        return LaunchResult(sent=transport.send_inline_card(card))
    return LaunchResult(disabled=True)


def _claim_path(home: Path, now: datetime) -> Path:
    """Return the owner-only day claim path without embedding target identity."""
    kst_date = now.astimezone(_KST).date().isoformat()
    return home / "data" / "inline-card-launch-claims" / f"{kst_date}.claim"


def _claim(path: Path) -> bool:
    """Atomically create a launch claim before any delivery can be attempted."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("claimed\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return True


class _ScheduledTransport:
    """Synchronously wait for the existing gateway loop's bounded card send."""

    def __init__(self, adapter: _AsyncCardAdapter, loop: asyncio.AbstractEventLoop) -> None:
        self._adapter = adapter
        self._loop = loop

    def is_inline_card_enabled(self, card: str) -> bool:
        return self._adapter.is_inline_card_enabled(card)

    def send_inline_card(self, card: str) -> bool:
        from agent.async_utils import safe_schedule_threadsafe

        future = safe_schedule_threadsafe(self._adapter.send_inline_card(card), self._loop)
        if future is None:
            return False
        try:
            result = future.result(timeout=30)
        except TimeoutError:
            future.cancel()
            return False
        return result.success
