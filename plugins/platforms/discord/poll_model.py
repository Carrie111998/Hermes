"""Read-only projection of Discord poll payloads into a typed model.

Aligned with the Discord REST API v10 ``resources/poll.mdx`` schema. This is a
pure, network-free parser: it projects a poll payload ``dict`` into typed
dataclasses and raises :class:`PollError` (a :class:`ValueError` subclass) on
any schema violation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "PollError",
    "PollQuestion",
    "PollAnswer",
    "PollData",
    "project_poll",
    "QUESTION_TEXT_MAX",
    "ANSWERS_MIN",
    "ANSWERS_MAX",
    "ANSWER_TEXT_MAX",
    "DURATION_MIN",
    "DURATION_MAX",
    "DEFAULT_LAYOUT_TYPE",
]

QUESTION_TEXT_MAX = 300
ANSWERS_MIN = 2
ANSWERS_MAX = 15
ANSWER_TEXT_MAX = 55
DURATION_MIN = 1
DURATION_MAX = 10080  # seconds == 7 days
DEFAULT_LAYOUT_TYPE = 1


class PollError(ValueError):
    """Raised when a poll payload violates the Discord poll schema."""


@dataclass(frozen=True)
class PollQuestion:
    """The poll question (Discord: ``question.text``)."""

    text: str


@dataclass(frozen=True)
class PollAnswer:
    """A single poll answer option."""

    option_id: int
    text: str


@dataclass(frozen=True)
class PollData:
    """Typed read-only projection of a Discord poll."""

    question: PollQuestion
    answers: list[PollAnswer]
    duration_seconds: Optional[int]
    layout_type: int = DEFAULT_LAYOUT_TYPE


def _is_int(value: Any) -> bool:
    """True for real ints; excludes bool (a bool is an int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def project_poll(payload: dict) -> PollData:
    """Project a Discord poll payload ``dict`` into a typed :class:`PollData`.

    Validation summary:

    * question text: required, non-empty, at most ``QUESTION_TEXT_MAX`` (300).
    * answers: between ``ANSWERS_MIN`` (2) and ``ANSWERS_MAX`` (15) entries,
      each with an integer ``option_id`` and non-empty text of at most
      ``ANSWER_TEXT_MAX`` (55) characters.
    * ``duration_seconds``: ``None`` or an int within
      ``DURATION_MIN``..``DURATION_MAX`` (1..10080 seconds, i.e. 7 days).
    * ``layout_type``: int, defaulting to ``DEFAULT_LAYOUT_TYPE`` (1).

    Raises:
        PollError: on any violation of the above.
    """
    if not isinstance(payload, dict):
        raise PollError(
            f"poll payload must be a dict, got {type(payload).__name__}"
        )

    # --- question -----------------------------------------------------
    question_raw = payload.get("question")
    if not isinstance(question_raw, dict):
        raise PollError("poll 'question' must be an object")
    question_text = question_raw.get("text")
    if not isinstance(question_text, str) or not question_text.strip():
        raise PollError("poll question text is required and must be non-empty")
    if len(question_text) > QUESTION_TEXT_MAX:
        raise PollError(
            f"poll question text must be at most {QUESTION_TEXT_MAX} characters, "
            f"got {len(question_text)}"
        )
    question = PollQuestion(text=question_text)

    # --- answers ------------------------------------------------------
    answers_raw = payload.get("answers")
    if not isinstance(answers_raw, list):
        raise PollError("poll 'answers' must be a list")
    if not (ANSWERS_MIN <= len(answers_raw) <= ANSWERS_MAX):
        raise PollError(
            f"poll must have between {ANSWERS_MIN} and {ANSWERS_MAX} answers, "
            f"got {len(answers_raw)}"
        )
    answers: list[PollAnswer] = []
    for index, answer_raw in enumerate(answers_raw):
        if not isinstance(answer_raw, dict):
            raise PollError(f"poll answer #{index} must be an object")
        option_id = answer_raw.get("option_id")
        if not _is_int(option_id):
            raise PollError(
                f"poll answer #{index} 'option_id' must be an integer"
            )
        answer_text = answer_raw.get("text")
        if not isinstance(answer_text, str) or not answer_text.strip():
            raise PollError(
                f"poll answer #{index} text is required and must be non-empty"
            )
        if len(answer_text) > ANSWER_TEXT_MAX:
            raise PollError(
                f"poll answer #{index} text must be at most {ANSWER_TEXT_MAX} "
                f"characters, got {len(answer_text)}"
            )
        answers.append(PollAnswer(option_id=option_id, text=answer_text))

    # --- duration -----------------------------------------------------
    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is not None:
        if not _is_int(duration_seconds):
            raise PollError(
                "poll 'duration_seconds' must be an integer or null"
            )
        if not (DURATION_MIN <= duration_seconds <= DURATION_MAX):
            raise PollError(
                f"poll 'duration_seconds' must be between {DURATION_MIN} and "
                f"{DURATION_MAX}, got {duration_seconds}"
            )

    # --- layout -------------------------------------------------------
    layout_type = payload.get("layout_type", DEFAULT_LAYOUT_TYPE)
    if not _is_int(layout_type):
        raise PollError("poll 'layout_type' must be an integer")

    return PollData(
        question=question,
        answers=answers,
        duration_seconds=duration_seconds,
        layout_type=layout_type,
    )
