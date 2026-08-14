"""Tests for the M5 Discord poll read-projection model."""

import pytest

from plugins.platforms.discord.poll_model import (
    ANSWER_TEXT_MAX,
    ANSWERS_MAX,
    ANSWERS_MIN,
    DURATION_MAX,
    DURATION_MIN,
    QUESTION_TEXT_MAX,
    PollAnswer,
    PollData,
    PollError,
    PollQuestion,
    project_poll,
)


def _valid_payload(**overrides):
    payload = {
        "question": {"text": "What's your favorite color?"},
        "answers": [
            {"option_id": 1, "text": "Red"},
            {"option_id": 2, "text": "Green"},
        ],
        "duration_seconds": 3600,
        "layout_type": 1,
    }
    payload.update(overrides)
    return payload


# --- valid projection --------------------------------------------------------


def test_valid_projection():
    poll = project_poll(_valid_payload())

    assert poll == PollData(
        question=PollQuestion(text="What's your favorite color?"),
        answers=[
            PollAnswer(option_id=1, text="Red"),
            PollAnswer(option_id=2, text="Green"),
        ],
        duration_seconds=3600,
        layout_type=1,
    )
    assert isinstance(poll.question, PollQuestion)
    assert all(isinstance(a, PollAnswer) for a in poll.answers)


def test_question_text_boundary_max():
    poll = project_poll(_valid_payload(question={"text": "q" * QUESTION_TEXT_MAX}))
    assert len(poll.question.text) == QUESTION_TEXT_MAX


def test_answer_count_minimum():
    answers = [
        {"option_id": i, "text": f"option {i}"}
        for i in range(1, ANSWERS_MIN + 1)
    ]
    poll = project_poll(_valid_payload(answers=answers))
    assert len(poll.answers) == ANSWERS_MIN


def test_answer_count_maximum():
    answers = [
        {"option_id": i, "text": f"option {i}"}
        for i in range(1, ANSWERS_MAX + 1)
    ]
    poll = project_poll(_valid_payload(answers=answers))
    assert len(poll.answers) == ANSWERS_MAX


def test_answer_text_boundary_max():
    answers = [
        {"option_id": 1, "text": "a" * ANSWER_TEXT_MAX},
        {"option_id": 2, "text": "Green"},
    ]
    poll = project_poll(_valid_payload(answers=answers))
    assert len(poll.answers[0].text) == ANSWER_TEXT_MAX


# --- question validation -----------------------------------------------------


def test_missing_question():
    payload = _valid_payload()
    del payload["question"]
    with pytest.raises(PollError):
        project_poll(payload)


def test_question_missing_text():
    with pytest.raises(PollError):
        project_poll(_valid_payload(question={}))


def test_question_empty_text():
    with pytest.raises(PollError):
        project_poll(_valid_payload(question={"text": ""}))


def test_question_whitespace_text():
    with pytest.raises(PollError):
        project_poll(_valid_payload(question={"text": "   "}))


def test_question_too_long():
    with pytest.raises(PollError):
        project_poll(
            _valid_payload(question={"text": "q" * (QUESTION_TEXT_MAX + 1)})
        )


# --- answer count / content validation ---------------------------------------


def test_answer_count_too_few():
    answers = [{"option_id": 1, "text": "only one"}]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_answer_count_too_many():
    answers = [
        {"option_id": i, "text": f"option {i}"}
        for i in range(1, ANSWERS_MAX + 2)  # 16 answers
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_empty_answer_text():
    answers = [
        {"option_id": 1, "text": ""},
        {"option_id": 2, "text": "Green"},
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_whitespace_answer_text():
    answers = [
        {"option_id": 1, "text": "   "},
        {"option_id": 2, "text": "Green"},
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_answer_text_too_long():
    answers = [
        {"option_id": 1, "text": "a" * (ANSWER_TEXT_MAX + 1)},
        {"option_id": 2, "text": "Green"},
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_answer_option_id_not_integer():
    answers = [
        {"option_id": "1", "text": "Red"},
        {"option_id": 2, "text": "Green"},
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


def test_answer_missing_option_id():
    answers = [
        {"text": "Red"},
        {"option_id": 2, "text": "Green"},
    ]
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=answers))


# --- duration validation -----------------------------------------------------


@pytest.mark.parametrize("duration", [0, -1, DURATION_MAX + 1])
def test_duration_out_of_bounds(duration):
    with pytest.raises(PollError):
        project_poll(_valid_payload(duration_seconds=duration))


def test_duration_minimum_ok():
    poll = project_poll(_valid_payload(duration_seconds=DURATION_MIN))
    assert poll.duration_seconds == DURATION_MIN


def test_duration_maximum_ok():
    poll = project_poll(_valid_payload(duration_seconds=DURATION_MAX))
    assert poll.duration_seconds == DURATION_MAX


def test_duration_not_integer():
    with pytest.raises(PollError):
        project_poll(_valid_payload(duration_seconds="3600"))


def test_duration_explicit_null():
    poll = project_poll(_valid_payload(duration_seconds=None))
    assert poll.duration_seconds is None


def test_duration_missing_defaults_to_none():
    payload = _valid_payload()
    del payload["duration_seconds"]
    poll = project_poll(payload)
    assert poll.duration_seconds is None


# --- layout validation -------------------------------------------------------


def test_layout_default():
    payload = _valid_payload()
    del payload["layout_type"]
    poll = project_poll(payload)
    assert poll.layout_type == 1


def test_layout_override():
    poll = project_poll(_valid_payload(layout_type=2))
    assert poll.layout_type == 2


def test_layout_not_integer():
    with pytest.raises(PollError):
        project_poll(_valid_payload(layout_type="1"))


# --- payload shape validation ------------------------------------------------


def test_payload_not_dict():
    with pytest.raises(PollError):
        project_poll("not a dict")  # type: ignore[arg-type]


def test_answers_not_list():
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers={}))


def test_answer_not_object():
    with pytest.raises(PollError):
        project_poll(_valid_payload(answers=[1, 2]))
