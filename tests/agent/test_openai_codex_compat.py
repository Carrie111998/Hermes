"""Regression tests for the openai Codex ``output=None`` compatibility guard.

Root cause (2026-05-28, memory ``codex_cron_empty_response_fix`` / R57):

The ChatGPT Codex backend (``chatgpt.com/backend-api/codex``) emits a final
``response.completed`` whose ``response.output`` is ``None`` — the model's real
text arrives via ``response.output_text.delta`` events, and only the final
aggregate snapshot is ``None``. openai SDK 2.31.0 *and current upstream main*
iterate ``for output in response.output`` inside ``parse_response`` with no
None-guard, raising ``TypeError: 'NoneType' object is not iterable`` while the
``responses.stream()`` accumulator processes the completion event. Hermes's own
``ResponsesApiTransport.validate_response`` guards output, but the SDK crashes
first; ``run_agent`` then classifies the ``TypeError`` as a non-retryable
client error and every Codex/gpt-5.5 cron reports "Agent completed but produced
empty response".

``parse_response`` is imported *by value* into two other modules
(``openai.lib.streaming.responses._responses`` — the ``.stream()`` accumulator —
and ``openai.resources.responses.responses``), so a durable monkeypatch must
rebind the source AND both importers, not just the source module.
"""

import importlib
from types import SimpleNamespace

import pytest


# The three modules that hold a binding to ``parse_response``: the source, the
# streaming accumulator (the binding Hermes's ``.stream()`` path actually hits),
# and the resources module used by the non-streaming parse path.
_PARSE_RESPONSE_MODULES = (
    "openai.lib._parsing._responses",
    "openai.lib.streaming.responses._responses",
    "openai.resources.responses.responses",
)


class TestGuardOutputNone:
    """The wrapper coerces ``response.output is None`` to ``[]`` before
    delegating, so the underlying SDK loop never sees ``None``."""

    def test_guard_coerces_none_output_to_empty_list(self):
        from agent.openai_codex_compat import _guard_output_none

        seen = {}

        def fake_parse_response(*, text_format, input_tools, response):
            # Mirrors the real SDK line 61: blindly iterates response.output.
            # Raises TypeError: 'NoneType' object is not iterable if not guarded.
            seen["output"] = list(response.output)
            return SimpleNamespace(output=seen["output"])

        guarded = _guard_output_none(fake_parse_response)
        resp = SimpleNamespace(output=None)

        result = guarded(text_format=None, input_tools=None, response=resp)

        assert seen["output"] == [], "underlying parse_response still saw None"
        assert result.output == []

    def test_guard_passes_real_output_through_untouched(self):
        from agent.openai_codex_compat import _guard_output_none

        sentinel = [SimpleNamespace(type="message")]

        def fake_parse_response(*, text_format, input_tools, response):
            return SimpleNamespace(output=list(response.output))

        guarded = _guard_output_none(fake_parse_response)
        resp = SimpleNamespace(output=sentinel)

        result = guarded(text_format=None, input_tools=None, response=resp)

        assert result.output == sentinel


class TestApplyRebindsAllBindings:
    """``apply_codex_output_none_guard`` must patch the source module AND both
    by-value importers, and must be idempotent."""

    @pytest.fixture(autouse=True)
    def _restore_bindings(self):
        """Snapshot and restore the three module bindings so the guard does not
        leak into sibling tests on the same xdist worker."""
        originals = {
            name: importlib.import_module(name).parse_response
            for name in _PARSE_RESPONSE_MODULES
        }
        yield
        for name, fn in originals.items():
            importlib.import_module(name).parse_response = fn

    def test_apply_guards_all_three_bindings(self):
        from agent.openai_codex_compat import apply_codex_output_none_guard

        apply_codex_output_none_guard(force=True)

        for name in _PARSE_RESPONSE_MODULES:
            fn = importlib.import_module(name).parse_response
            assert getattr(fn, "_hermes_codex_output_none_guard", False), (
                f"{name}.parse_response was not guarded — the streaming "
                f"accumulator binding is the one the codex .stream() path hits"
            )

    def test_apply_is_idempotent(self):
        from agent.openai_codex_compat import apply_codex_output_none_guard

        apply_codex_output_none_guard(force=True)
        streaming = "openai.lib.streaming.responses._responses"
        first = importlib.import_module(streaming).parse_response

        apply_codex_output_none_guard()  # second call, not forced

        second = importlib.import_module(streaming).parse_response
        assert first is second, "second apply() re-wrapped an already-guarded fn"


class TestRealResponseObject:
    """The wrapper mutates ``response.output = []`` on the real pydantic
    ``Response`` model; verify that assignment is actually allowed."""

    def test_real_response_output_is_assignable(self):
        from openai.types.responses import Response

        resp = Response.model_construct(output=None)
        assert resp.output is None

        resp.output = []  # exactly what _guard_output_none does

        assert resp.output == []
