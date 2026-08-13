"""`choices` holds button labels, never prose.

Models routinely hand ``clarify`` a full sentence per choice because the array
reads like a list of answers. It isn't: every surface renders these as buttons
or numbered rows, and the selected string comes back verbatim as
``user_response``. The guard is reject-and-retry rather than truncation -- a
shortened sentence is still the wrong label and has silently lost the words
that made it meaningful, so the model must move the prose into ``question``
and re-issue.
"""

import json

import pytest

from tools.clarify_tool import (
    CLARIFY_SCHEMA,
    MAX_CHOICE_WORDS,
    _prose_choice_reason,
    clarify_tool,
)


class _RecordingCallback:
    """Callback that fails the test if a rejected call ever reaches the UI."""

    def __init__(self, answer="Approve"):
        self.calls = []
        self._answer = answer

    def __call__(self, question, choices, multi_select=False):
        self.calls.append((question, choices, multi_select))
        return self._answer


def _error_of(result):
    """Return the ``tool_error`` message, or None if this isn't an error.

    ``tools.registry.tool_error`` returns ``{"error": "<message>"}`` as a JSON
    string, so a rejected call is distinguishable from a successful one (which
    carries ``question``/``choices_offered``/``user_response``) by that key.
    """
    payload = json.loads(result)
    return payload.get("error")


class TestProseChoicesRejected:
    def test_full_sentence_choice_is_rejected(self):
        cb = _RecordingCallback()

        result = clarify_tool(
            "What should I do?",
            choices=["Approve this urgent change to the shared config file"],
            callback=cb,
        )

        assert _error_of(result) is not None
        # Reject-and-retry: the user was never prompted.
        assert cb.calls == []

    def test_rejection_names_the_offending_choice_and_the_fix(self):
        result = clarify_tool(
            "What should I do?",
            choices=["Approve this urgent change to the shared config file"],
            callback=_RecordingCallback(),
        )

        message = _error_of(result)
        assert "Approve this urgent change to the shared config file" in message
        # The model needs to be told where the prose goes and that a retry works.
        assert "question" in message
        assert "clarify again" in message

    def test_one_bad_choice_rejects_the_whole_call(self):
        cb = _RecordingCallback()

        result = clarify_tool(
            "Ship it?",
            choices=["Approve", "Hold off until the migration lands next week"],
            callback=cb,
        )

        assert _error_of(result) is not None
        assert cb.calls == []

    def test_colon_explanation_is_rejected(self):
        result = clarify_tool(
            "Which one?",
            choices=["Approve: it only touches the staging config"],
            callback=_RecordingCallback(),
        )
        assert _error_of(result) is not None

    @pytest.mark.parametrize("label", ["Approve.", "Approve!", "Approve?"])
    def test_sentence_terminal_punctuation_is_rejected(self, label):
        result = clarify_tool(
            "Which one?", choices=[label], callback=_RecordingCallback()
        )
        assert _error_of(result) is not None

    def test_word_count_boundary(self):
        """The limit is inclusive: >MAX_CHOICE_WORDS is prose, == is fine."""
        at_limit = " ".join(["word"] * MAX_CHOICE_WORDS)
        over_limit = " ".join(["word"] * (MAX_CHOICE_WORDS + 1))

        assert _prose_choice_reason(at_limit) is None
        assert _prose_choice_reason(over_limit) is not None

    def test_prose_in_a_dict_shaped_choice_is_caught_after_flattening(self):
        """The check runs on the display string, so dict choices can't smuggle."""
        cb = _RecordingCallback()

        result = clarify_tool(
            "Which layout?",
            choices=[{"description": "Tight layout that covers all three points."}],
            callback=cb,
        )

        assert _error_of(result) is not None
        assert cb.calls == []


class TestValidChoicesStillWork:
    def test_bare_labels_reach_the_callback(self):
        cb = _RecordingCallback()

        result = json.loads(
            clarify_tool("Ship it?", choices=["Approve", "Hold off"], callback=cb)
        )

        assert _error_of(json.dumps(result)) is None
        assert result["choices_offered"] == ["Approve", "Hold off"]
        assert result["user_response"] == "Approve"
        assert len(cb.calls) == 1

    @pytest.mark.parametrize(
        "label",
        [
            "Approve",
            "Hold off",
            "Rebase onto main",
            "3:1",          # bare colon, no explanation after it
            "12:30",
            "v1.2 rollout",  # internal period, not terminal
            "Other (type your answer)",
        ],
    )
    def test_realistic_labels_are_accepted(self, label):
        assert _prose_choice_reason(label) is None

    def test_open_ended_question_unaffected(self):
        cb = _RecordingCallback(answer="whatever")

        result = json.loads(clarify_tool("How did that go?", callback=cb))

        assert result["choices_offered"] is None
        assert len(cb.calls) == 1


class TestSchemaDocumentsTheRule:
    def test_choices_description_states_the_constraint(self):
        description = CLARIFY_SCHEMA["parameters"]["properties"]["choices"][
            "description"
        ]
        assert str(MAX_CHOICE_WORDS) in description
        assert "REJECTED" in description
