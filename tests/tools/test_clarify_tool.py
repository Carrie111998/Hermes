"""Tests for tools/clarify_tool.py - Interactive clarifying questions."""

import json
from typing import List, Optional


from tools.clarify_tool import (
    ClarifyTimeoutError,
    ClarifyUnavailableError,
    clarify_tool,
    check_clarify_requirements,
    MAX_CHOICES,
    CLARIFY_SCHEMA,
    _flatten_choice,
)


class TestClarifyToolBasics:
    """Basic functionality tests for clarify_tool."""

    def test_simple_question_with_callback(self):
        """Should return user response for simple question."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "What color?"
            assert choices is None
            return "blue"

        result = json.loads(clarify_tool("What color?", callback=mock_callback))
        assert result["question"] == "What color?"
        assert result["choices_offered"] is None
        assert result["user_response"] == "blue"


    def test_no_callback_returns_error(self):
        """Should return error when no callback is provided."""
        result = json.loads(clarify_tool("What do you want?"))
        assert result == {
            "error": "Clarify tool is not available in this execution context."
        }

    def test_no_callback_abort_returns_fail_closed_error(self):
        """An unavailable approval prompt cannot produce implicit consent."""
        result = json.loads(clarify_tool(
            "Approve publishing?",
            on_timeout="abort",
        ))

        assert result["error_type"] == "clarify_unavailable"
        assert result["approved"] is False
        assert result["timed_out"] is False
        assert result["on_timeout"] == "abort"


class TestClarifyToolChoicesValidation:
    """Tests for choices parameter validation."""

    def test_choices_trimmed_to_max(self):
        """Should trim choices to MAX_CHOICES."""
        choices_passed = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_passed.extend(choices or [])
            return "picked"

        many_choices = ["a", "b", "c", "d", "e", "f", "g"]
        clarify_tool("Pick one", choices=many_choices, callback=mock_callback)

        assert len(choices_passed) == MAX_CHOICES


    def test_choices_converted_to_strings(self):
        """Non-string choices should be converted to strings."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=[1, 2, 3], callback=mock_callback)  # type: ignore
        assert choices_received == ["1 (Recommended)", "2", "3"]


class TestClarifyToolCallbackHandling:
    """Tests for callback error handling."""

    def test_callback_exception_returns_error(self):
        """Should return error if callback raises exception."""
        def failing_callback(question: str, choices: Optional[List[str]]) -> str:
            raise RuntimeError("User cancelled")

        result = json.loads(clarify_tool("Question?", callback=failing_callback))
        assert "error" in result
        assert "Failed to get user input" in result["error"]
        assert "User cancelled" in result["error"]


    def test_user_response_stripped(self):
        """User response should be stripped of whitespace."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            return "  response with spaces  \n"

        result = json.loads(clarify_tool("Q?", callback=mock_callback))
        assert result["user_response"] == "response with spaces"


class TestCheckClarifyRequirements:
    """Tests for the requirements check function."""

    def test_always_returns_true(self):
        """clarify tool has no external requirements."""
        assert check_clarify_requirements() is True


class TestClarifyDictChoices:
    """Dict-shaped choices must be unwrapped to user-facing text at the source.

    LLMs sometimes emit [{"description": "..."}] instead of bare strings. The
    naive str(c) coercion leaked the Python dict repr onto every surface (CLI
    panel, Discord buttons, Telegram list) AND returned it verbatim as the
    user's answer. _flatten_choice normalises at the one platform-agnostic
    entry point so the whole class is fixed in one place.
    """

    def test_flatten_unwraps_label_first(self):
        assert _flatten_choice({"label": "Short", "description": "Long"}) == "Short"


    def test_dict_choices_reach_callback_as_clean_text(self):
        """The whole point: the UI callback never sees a dict repr."""
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0]

        result = json.loads(clarify_tool(
            "Pick a layout",
            choices=[
                {"choice": "Tight", "description": "Tight, covers all 3 points"},
                {"description": "Loose layout"},
                {"name": "modelid", "value": "abc"},  # dropped, not leaked
                "A plain string choice",
            ],
            callback=cb,
        ))  # type: ignore
        assert seen == [
            "Tight, covers all 3 points (Recommended)",
            "Loose layout",
            "A plain string choice",
        ]
        # and the resolved answer is clean text, not a dict repr
        assert result["user_response"] == "Tight, covers all 3 points"
        assert "{" not in result["user_response"]
        assert all("{" not in c for c in result["choices_offered"])


class TestClarifySchema:
    """Tests for the OpenAI function-calling schema."""

    def test_schema_name(self):
        """Schema should have correct name."""
        assert CLARIFY_SCHEMA["name"] == "clarify"


    def test_max_choices_is_four(self):
        """MAX_CHOICES constant should be 4."""
        assert MAX_CHOICES == 4


    def test_schema_multi_select_default_false(self):
        """multi_select should default to false (not in required)."""
        # The model should treat it as false when omitted
        assert "multi_select" not in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_on_timeout_is_optional_enum(self):
        """on_timeout exposes the two policies without becoming required."""
        prop = CLARIFY_SCHEMA["parameters"]["properties"]["on_timeout"]

        assert prop["type"] == "string"
        assert prop["enum"] == ["proceed", "abort"]
        assert "on_timeout" not in CLARIFY_SCHEMA["parameters"]["required"]


class TestClarifyToolMultiSelect:
    """Tests for multi_select (checkbox) support added to clarify_tool."""

    def test_multi_select_false_keeps_existing_behavior(self):
        """When multi_select=False, user_response should be a single string."""
        def mock_callback(question, choices):
            return "blue"

        result = json.loads(clarify_tool(
            "What color?",
            choices=["red", "blue", "green"],
            multi_select=False,
            callback=mock_callback,
        ))
        assert result["user_response"] == "blue"
        assert isinstance(result["user_response"], str)

    def test_multi_select_true_returns_list(self):
        """When multi_select=True, user_response should be a list of strings."""
        def mock_callback(question, choices):
            return "red, blue"

        result = json.loads(clarify_tool(
            "Which colors?",
            choices=["red", "blue", "green"],
            multi_select=True,
            callback=mock_callback,
        ))
        assert result["user_response"] == ["red", "blue"]
        assert isinstance(result["user_response"], list)

    def test_multi_select_single_choice_still_list(self):
        """Even a single selection should be a list when multi_select=True."""
        def mock_callback(question, choices):
            return "red"

        result = json.loads(clarify_tool(
            "Which color?",
            choices=["red", "blue"],
            multi_select=True,
            callback=mock_callback,
        ))
        assert result["user_response"] == ["red"]
        assert isinstance(result["user_response"], list)


    def test_multi_select_max_choices_enforced(self):
        """MAX_CHOICES enforcement should still work with multi_select."""
        choices_passed = []

        def mock_callback(question, choices):
            choices_passed.extend(choices or [])
            return "a, b, c, d"

        many_choices = ["a", "b", "c", "d", "e", "f"]
        clarify_tool(
            "Pick some",
            choices=many_choices,
            multi_select=True,
            callback=mock_callback,
        )
        assert len(choices_passed) == MAX_CHOICES


class TestClarifyRecommendedLabel:
    """The first choice is the agent's pick and is labelled as such.

    The schema tells the model to order choices best-first, so the tool tags
    element 0 with "(Recommended)" at the one platform-agnostic entry point —
    CLI, TUI, desktop, and messaging adapters all inherit the same label. The
    label is presentation only: it never appears in the answer the agent reads.
    """

    def test_first_choice_is_labelled(self):
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[1]

        clarify_tool("Pick", choices=["Rebase", "Merge"], callback=cb)
        assert seen == ["Rebase (Recommended)", "Merge"]

    def test_answer_strips_the_label(self):
        """Picking the recommended option returns the bare option text."""
        def cb(question, choices):
            return choices[0]

        result = json.loads(clarify_tool("Pick", choices=["Rebase", "Merge"], callback=cb))
        assert result["user_response"] == "Rebase"
        assert result["choices_offered"] == ["Rebase", "Merge"]

    def test_multi_select_answers_strip_the_label(self):
        def cb(question, choices, multi_select=False):
            return ", ".join(choices[:2])

        result = json.loads(clarify_tool(
            "Pick some",
            choices=["Rebase", "Merge", "Squash"],
            multi_select=True,
            callback=cb,
        ))
        assert result["user_response"] == ["Rebase", "Merge"]

    def test_single_choice_is_not_labelled(self):
        """One option isn't a recommendation — there's nothing to prefer it over."""
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0]

        clarify_tool("Confirm", choices=["Ship it"], callback=cb)
        assert seen == ["Ship it"]

    def test_label_is_not_doubled(self):
        """A model that wrote its own label doesn't get a second one."""
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0]

        clarify_tool("Pick", choices=["Rebase (recommended)", "Merge"], callback=cb)
        assert seen == ["Rebase (recommended)", "Merge"]

    def test_open_ended_unaffected(self):
        def cb(question, choices):
            assert choices is None
            return "whatever"

        result = json.loads(clarify_tool("Thoughts?", callback=cb))
        assert result["choices_offered"] is None
        assert result["user_response"] == "whatever"


class TestInvokeCallbackDispatch:
    """_invoke_callback uses signature inspection, never a TypeError retry."""

    def test_internal_typeerror_not_swallowed_or_retried(self):
        """A compatible callback that raises TypeError internally must be
        invoked exactly once and its error surfaced — not retried with the
        legacy 2-arg form (which would prompt the user twice)."""
        from tools.clarify_tool import _invoke_callback
        calls = []

        def bad_callback(question, choices, multi_select=False):
            calls.append(1)
            raise TypeError("internal bug")

        import pytest
        with pytest.raises(TypeError, match="internal bug"):
            _invoke_callback(bad_callback, "Q?", ["a"], True, "proceed")
        assert len(calls) == 1


    def test_var_keyword_callback_keeps_legacy_proceed_contract(self):
        from tools.clarify_tool import _invoke_callback
        seen = {}

        def kw_cb(question, choices, **kwargs):
            seen.update(kwargs)
            return "ok"

        _invoke_callback(kw_cb, "Q?", ["a"], True, "proceed")
        assert seen.get("multi_select") is True
        assert "on_timeout" not in seen

    def test_var_keyword_callback_cannot_claim_abort_support(self):
        """Accepting arbitrary kwargs does not prove the callback enforces
        the fail-closed policy."""
        from tools.clarify_tool import _invoke_callback

        calls = []

        def kw_cb(question, choices, **kwargs):
            calls.append(kwargs)
            return "ok"

        import pytest
        with pytest.raises(ClarifyUnavailableError):
            _invoke_callback(kw_cb, "Q?", ["a"], True, "abort")
        assert calls == []


class TestClarifyTimeoutPolicy:
    """Timeout policy is config-backed and overridable per invocation."""

    _PROCEED_SENTINEL = (
        "The user did not provide a response within the time limit. "
        "Use your best judgement to make the choice and proceed."
    )

    @staticmethod
    def _set_config(monkeypatch, policy):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: (
                {"agent": {"clarify_on_timeout": policy}}
                if policy is not None
                else {}
            ),
        )

    @staticmethod
    def _timeout_callback(seen):
        def callback(question, choices, multi_select=False, on_timeout="proceed"):
            seen["on_timeout"] = on_timeout
            if on_timeout == "abort":
                raise ClarifyTimeoutError()
            return TestClarifyTimeoutPolicy._PROCEED_SENTINEL

        return callback

    @staticmethod
    def _assert_abort_result(result):
        assert "error" in result
        assert result["error_type"] == "clarify_timeout"
        assert result["approved"] is False
        assert result["timed_out"] is True
        assert result["on_timeout"] == "abort"

    @staticmethod
    def _assert_unavailable_result(result):
        assert "error" in result
        assert result["error_type"] == "clarify_unavailable"
        assert result["approved"] is False
        assert result["timed_out"] is False
        assert result["on_timeout"] == "abort"

    def test_default_proceed_preserves_existing_timeout_sentinel(self, monkeypatch):
        """Unset config + unset argument keeps today's fail-open result."""
        self._set_config(monkeypatch, None)
        seen = {}

        result = json.loads(clarify_tool(
            "Proceed without an answer?",
            callback=self._timeout_callback(seen),
        ))

        assert seen["on_timeout"] == "proceed"
        assert result["user_response"] == self._PROCEED_SENTINEL
        assert "error" not in result

    def test_per_call_abort_returns_timeout_error(self, monkeypatch):
        """An explicit abort turns callback timeout into an error result."""
        self._set_config(monkeypatch, "proceed")
        seen = {}

        result = json.loads(clarify_tool(
            "Approve publishing?",
            on_timeout="abort",
            callback=self._timeout_callback(seen),
        ))

        assert seen["on_timeout"] == "abort"
        self._assert_abort_result(result)

    def test_config_abort_is_honored_when_argument_is_omitted(self, monkeypatch):
        """The fleet default applies when a call does not choose a policy."""
        self._set_config(monkeypatch, "abort")
        seen = {}

        result = json.loads(clarify_tool(
            "Approve publishing?",
            callback=self._timeout_callback(seen),
        ))

        assert seen["on_timeout"] == "abort"
        self._assert_abort_result(result)

    def test_per_call_proceed_overrides_config_abort(self, monkeypatch):
        """An informational prompt can opt back into fail-open behavior."""
        self._set_config(monkeypatch, "abort")
        seen = {}

        result = json.loads(clarify_tool(
            "Which color do you prefer?",
            on_timeout="proceed",
            callback=self._timeout_callback(seen),
        ))

        assert seen["on_timeout"] == "proceed"
        assert result["user_response"] == self._PROCEED_SENTINEL
        assert "error" not in result

    def test_abort_fails_closed_for_legacy_callback(self, monkeypatch):
        """A callback without explicit policy support is unavailable, not approved."""
        self._set_config(monkeypatch, "abort")
        called = []

        def legacy_callback(question, choices, **kwargs):
            called.append(True)
            return "ok"

        result = json.loads(clarify_tool(
            "Approve publishing?",
            callback=legacy_callback,
        ))

        assert called == []
        self._assert_unavailable_result(result)


class TestRegistryMultiSelectPassThrough:
    """The registered tool handler must forward multi_select from tool args."""

    def test_handler_passes_multi_select(self):
        from tools.registry import registry
        entry = registry.get_entry("clarify")
        seen = {}

        def cb(question, choices, multi_select=False):
            seen["multi"] = multi_select
            return "a, b"

        result = json.loads(entry.handler(
            {"question": "Pick", "choices": ["a", "b"], "multi_select": True},
            callback=cb,
        ))
        assert seen["multi"] is True
        assert result["user_response"] == ["a", "b"]

    def test_handler_default_single_select(self):
        from tools.registry import registry
        entry = registry.get_entry("clarify")
        seen = {}

        def cb(question, choices, multi_select=False):
            seen["multi"] = multi_select
            return "a"

        result = json.loads(entry.handler(
            {"question": "Pick", "choices": ["a", "b"]},
            callback=cb,
        ))
        assert seen["multi"] is False
        assert result["user_response"] == "a"

    def test_handler_forwards_on_timeout(self, monkeypatch):
        """The registered handler forwards an explicit timeout policy."""
        from tools.registry import registry

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"agent": {"clarify_on_timeout": "proceed"}},
        )
        entry = registry.get_entry("clarify")
        seen = {}

        def cb(question, choices, multi_select=False, on_timeout="proceed"):
            seen["on_timeout"] = on_timeout
            raise ClarifyTimeoutError()

        result = json.loads(entry.handler(
            {
                "question": "Approve publishing?",
                "choices": ["Approve", "Reject"],
                "on_timeout": "abort",
            },
            callback=cb,
        ))

        assert seen["on_timeout"] == "abort"
        assert result["error_type"] == "clarify_timeout"
        assert result["approved"] is False

    def test_handler_omits_policy_for_config_resolution(self, monkeypatch):
        """Omitting on_timeout does not force proceed ahead of config."""
        from tools.registry import registry

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"agent": {"clarify_on_timeout": "abort"}},
        )
        entry = registry.get_entry("clarify")
        seen = {}

        def cb(question, choices, multi_select=False, on_timeout="proceed"):
            seen["on_timeout"] = on_timeout
            raise ClarifyTimeoutError()

        result = json.loads(entry.handler(
            {"question": "Approve publishing?", "choices": ["Approve", "Reject"]},
            callback=cb,
        ))

        assert seen["on_timeout"] == "abort"
        assert result["error_type"] == "clarify_timeout"
        assert result["timed_out"] is True
