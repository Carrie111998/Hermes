"""Parse and persist one-use Slack interactive reply buttons."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - unusual platform
        msvcrt = None
else:
    msvcrt = None


_MAX_BUTTONS = 25
_MAX_LABEL_LENGTH = 75
_MAX_ACTION_ID_LENGTH = 255
_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIRECTIVE_RE = re.compile(
    r"(?:^|\n)\[\[slack_buttons:\s*(?P<buttons>[^\]\n]+)\]\]\s*$"
)


@dataclass(frozen=True)
class InteractiveButton:
    label: str
    action_id: str


@dataclass(frozen=True)
class InteractiveReply:
    visible_content: str
    buttons: tuple[InteractiveButton, ...]


@dataclass(frozen=True)
class PreparedInteractiveButton:
    label: str
    action_id: str
    token: str


@dataclass(frozen=True)
class PreparedInteractiveReply:
    card_id: str
    buttons: tuple[PreparedInteractiveButton, ...]


@dataclass(frozen=True)
class ConsumedInteractiveAction:
    card_id: str
    action_id: str
    channel_id: str
    thread_ts: str | None
    message_ts: str


def _validate_buttons(buttons: tuple[InteractiveButton, ...]) -> bool:
    if not buttons or len(buttons) > _MAX_BUTTONS:
        return False
    action_ids: set[str] = set()
    for button in buttons:
        label = button.label.strip()
        action_id = button.action_id.strip()
        if not label or len(label) > _MAX_LABEL_LENGTH:
            return False
        if (
            not action_id
            or len(action_id) > _MAX_ACTION_ID_LENGTH
            or not _ACTION_ID_RE.fullmatch(action_id)
            or action_id in action_ids
        ):
            return False
        action_ids.add(action_id)
    return True


def parse_interactive_reply(content: str) -> InteractiveReply | None:
    """Return a validated reply only when the whole directive is well-formed.

    Invalid directives deliberately return ``None`` so callers can send the
    original content literally instead of accidentally dropping agent output.
    """
    remaining = content
    directives: list[tuple[InteractiveButton, ...]] = []
    while match := _DIRECTIVE_RE.search(remaining):
        buttons: list[InteractiveButton] = []
        for item in match.group("buttons").split(","):
            if ":" not in item:
                break
            label, action_id = item.rsplit(":", 1)
            buttons.append(InteractiveButton(label.strip(), action_id.strip()))
        else:
            parsed_buttons = tuple(buttons)
            if _validate_buttons(parsed_buttons):
                directives.insert(0, parsed_buttons)
                remaining = remaining[: match.start()].rstrip()
                continue

        if not directives:
            return None
        break

    if not directives:
        return None
    all_buttons = tuple(button for directive in directives for button in directive)
    if not _validate_buttons(all_buttons):
        return None
    return InteractiveReply(remaining, all_buttons)


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize state changes with fcntl on Unix and msvcrt on Windows."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")
    lock_file = lock_path.open("r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_file.close()


class InteractiveReplyStore:
    """Profile-scoped, file-backed store for unbound and one-use Slack cards."""

    def __init__(self, ttl_seconds: float = 15 * 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._state_path = get_hermes_home() / "slack_interactive_replies.json"
        self._lock_path = self._state_path.with_suffix(".json.lock")

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"cards": {}}
        cards = state.get("cards") if isinstance(state, dict) else None
        return {"cards": cards} if isinstance(cards, dict) else {"cards": {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            dir=self._state_path.parent,
            prefix=f".{self._state_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
                json.dump(state, temporary_file, separators=(",", ":"), sort_keys=True)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._state_path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    @staticmethod
    def _clean_expired(state: dict[str, Any], now: float) -> bool:
        cards = state["cards"]
        expired = [
            card_id
            for card_id, card in cards.items()
            if not isinstance(card, dict) or float(card.get("expires_at", 0)) <= now
        ]
        for card_id in expired:
            del cards[card_id]
        return bool(expired)

    def create_card(
        self,
        channel_id: str,
        thread_ts: str | None,
        buttons: tuple[InteractiveButton, ...],
    ) -> PreparedInteractiveReply:
        if not _validate_buttons(buttons):
            raise ValueError("interactive reply buttons must be valid and unique")
        if not channel_id:
            raise ValueError("channel_id must not be blank")

        prepared_buttons = tuple(
            PreparedInteractiveButton(button.label.strip(), button.action_id.strip(), secrets.token_urlsafe(24))
            for button in buttons
        )
        with _file_lock(self._lock_path):
            state = self._read_state()
            self._clean_expired(state, time.time())
            card_id = secrets.token_urlsafe(24)
            while card_id in state["cards"]:
                card_id = secrets.token_urlsafe(24)
            state["cards"][card_id] = {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "message_ts": None,
                "expires_at": time.time() + self._ttl_seconds,
                "buttons": [
                    {"label": button.label, "action_id": button.action_id, "token": button.token}
                    for button in prepared_buttons
                ],
            }
            self._write_state(state)
        return PreparedInteractiveReply(card_id, prepared_buttons)

    def bind_message(self, card_id: str, message_ts: str) -> bool:
        if not message_ts:
            return False
        with _file_lock(self._lock_path):
            state = self._read_state()
            self._clean_expired(state, time.time())
            card = state["cards"].get(card_id)
            if not isinstance(card, dict) or card.get("message_ts") is not None:
                self._write_state(state)
                return False
            card["message_ts"] = message_ts
            self._write_state(state)
            return True

    def discard(self, card_id: str) -> None:
        with _file_lock(self._lock_path):
            state = self._read_state()
            self._clean_expired(state, time.time())
            state["cards"].pop(card_id, None)
            self._write_state(state)

    def consume(
        self, button_token: str, channel_id: str, message_ts: str
    ) -> ConsumedInteractiveAction | None:
        """Atomically validate and consume one bound token, else return ``None``."""
        with _file_lock(self._lock_path):
            state = self._read_state()
            self._clean_expired(state, time.time())
            for card_id, card in state["cards"].items():
                if not isinstance(card, dict):
                    continue
                if card.get("channel_id") != channel_id or card.get("message_ts") != message_ts:
                    continue
                for button in card.get("buttons", []):
                    if isinstance(button, dict) and button.get("token") == button_token:
                        action = ConsumedInteractiveAction(
                            card_id=card_id,
                            action_id=str(button["action_id"]),
                            channel_id=channel_id,
                            thread_ts=card.get("thread_ts"),
                            message_ts=message_ts,
                        )
                        del state["cards"][card_id]
                        self._write_state(state)
                        return action
            self._write_state(state)
            return None


def append_actions_block(
    blocks: list[dict[str, Any]], prepared: PreparedInteractiveReply
) -> list[dict[str, Any]]:
    """Return a copy of *blocks* with the prepared Slack actions block appended."""
    actions = {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": button.label, "emoji": True},
                "action_id": "hermes_interactive_reply",
                "value": button.token,
            }
            for button in prepared.buttons
        ],
    }
    return [*blocks, actions]
