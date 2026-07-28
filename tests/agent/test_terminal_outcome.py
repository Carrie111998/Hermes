from __future__ import annotations

import pytest

from agent.terminal_outcome import (
    TerminalOutcomeKind,
    normalize_terminal_outcome,
)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({}, TerminalOutcomeKind.COMPLETED),
        ({"status": "completed"}, TerminalOutcomeKind.COMPLETED),
        ({"completed": True}, TerminalOutcomeKind.COMPLETED),
        ({"status": "failed"}, TerminalOutcomeKind.FAILED),
        ({"error": "boom"}, TerminalOutcomeKind.FAILED),
        ({"status": "interrupted"}, TerminalOutcomeKind.INTERRUPTED),
        ({"status": "incomplete"}, TerminalOutcomeKind.PARTIAL),
        ({"completed": False}, TerminalOutcomeKind.PARTIAL),
    ],
)
def test_normalize_terminal_outcome(result, expected):
    assert normalize_terminal_outcome(result).kind is expected


@pytest.mark.parametrize(
    "result",
    [
        {
            "completed": False,
            "partial": True,
            "failed": True,
            "error": "iteration_budget_exhausted",
        },
        {"completed": True, "status": "failed"},
        {"partial": True, "status": "failed"},
        {"interrupted": True, "failed": True},
    ],
)
def test_failure_precedes_all_contradictory_terminal_signals(result):
    outcome = normalize_terminal_outcome(result)

    assert outcome.kind is TerminalOutcomeKind.FAILED
    assert outcome.failed is True
    assert outcome.completed is False
    assert outcome.partial is False
    assert outcome.interrupted is False
    assert outcome.incomplete is True
    assert outcome.contradictory is True


def test_interrupted_precedes_partial_and_completed():
    outcome = normalize_terminal_outcome(
        {"interrupted": True, "partial": True, "completed": True}
    )

    assert outcome.kind is TerminalOutcomeKind.INTERRUPTED
    assert outcome.contradictory is True


def test_partial_precedes_completed():
    outcome = normalize_terminal_outcome(
        {"partial": True, "completed": True}
    )

    assert outcome.kind is TerminalOutcomeKind.PARTIAL
    assert outcome.contradictory is True


@pytest.mark.parametrize(
    "result",
    [
        {
            "status": "failed",
            "completed": False,
            "partial": False,
            "interrupted": False,
            "failed": True,
            "incomplete": True,
        },
        {
            "status": "interrupted",
            "completed": False,
            "partial": False,
            "interrupted": True,
            "failed": False,
            "incomplete": True,
        },
    ],
)
def test_shared_non_success_envelope_is_not_self_contradictory(result):
    outcome = normalize_terminal_outcome(result)

    assert outcome.incomplete is True
    assert outcome.contradictory is False


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"completed": "yes"},
        {"status": 7},
        {"status": None},
        {"status": ""},
        {"status": "   "},
        {"status": "mystery"},
    ],
)
def test_malformed_or_unknown_terminal_contract_fails_closed(result):
    outcome = normalize_terminal_outcome(result)

    assert outcome.kind is TerminalOutcomeKind.FAILED
    assert outcome.valid is False
    assert outcome.incomplete is True
