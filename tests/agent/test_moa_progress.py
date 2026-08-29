"""Tests for the MOA progress indicator added in issue #59546.

The MoA facade (``MoAChatCompletions.create``) emits a sequence of display
events so a TUI / CLI / desktop surface can render progress as references
complete and the phase transitions from ``reference`` to ``aggregator``:

  - ``moa.progress`` — fired once per reference completion with
                       ``refs_done`` and ``refs_total`` (e.g. drives a
                       status-bar ``MOA: 2/3 refs done`` indicator)
  - ``moa.phase``    — announces the complete advisor roster before dispatch,
                       then the aggregator transition after every slot settles

These tests exercise the real callback surface end-to-end through the
display hook (``reference_callback``) — no mocks on the dispatch path.
The LLM is stubbed via ``call_llm`` so the test does not depend on any
real provider.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _response(content="ok"):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="fake")


@pytest.fixture
def moa_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: closed
  presets:
    closed:
      enabled: true
      reference_models:
        - provider: openrouter
          model: anthropic/claude-opus-4.8
        - provider: openrouter
          model: openai/gpt-5.5
        - provider: openrouter
          model: google/gemini-3-pro
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _collect_emits(facade):
    """Pull every event the facade dispatches into a flat list of (event, kwargs)."""
    captured: list[tuple[str, dict]] = []

    def _capture(event: str, **kwargs):
        captured.append((event, kwargs))

    facade.reference_callback = _capture
    return captured




def test_moa_phase_announces_parallel_roster_then_aggregator(moa_config, monkeypatch):
    """The full stable roster is visible before completion events arrive."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    facade = MoAChatCompletions("closed")
    captured = _collect_emits(facade)

    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "plan the migration"}],
    )

    phase_events = [(e, k) for (e, k) in captured if e == "moa.phase"]
    assert [kwargs["phase"] for _event, kwargs in phase_events] == [
        "reference",
        "aggregator",
    ]
    _event, start = phase_events[0]
    assert start["advisors"] == [
        "openrouter:anthropic/claude-opus-4.8",
        "openrouter:openai/gpt-5.5",
        "openrouter:google/gemini-3-pro",
    ]
    assert start["concurrency"] == 3
    assert start["refs_done"] == 0
    assert start["guidance_reused"] is False

    _event, kwargs = phase_events[1]
    assert kwargs["phase"] == "aggregator"
    assert kwargs["aggregator"] == "openrouter:anthropic/claude-opus-4.8"
    # counts match the configured reference count
    assert kwargs["refs_done"] == 3
    assert kwargs["refs_total"] == 3

    progress = sorted(
        (kwargs for event, kwargs in captured if event == "moa.progress"),
        key=lambda item: item["index"],
    )
    assert [item["index"] for item in progress] == [1, 2, 3]
    assert {item["status"] for item in progress} == {"complete"}


def test_moa_cache_reuse_is_explicit(moa_config, monkeypatch):
    from agent.moa_loop import MoAChatCompletions

    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **_kwargs: _response("advice"))
    facade = MoAChatCompletions("closed")
    captured = _collect_emits(facade)
    messages = [{"role": "user", "content": "plan the migration"}]

    facade.create(model="closed", messages=messages)
    captured.clear()
    facade.create(model="closed", messages=messages)

    phases = [kwargs for event, kwargs in captured if event == "moa.phase"]
    assert [phase["phase"] for phase in phases] == ["reference", "aggregator"]
    assert all(phase["guidance_reused"] is True for phase in phases)
    assert not [event for event, _kwargs in captured if event == "moa.progress"]


def test_moa_progress_reports_failed_slot_without_reordering(moa_config, monkeypatch):
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference" and kwargs.get("model") == "openai/gpt-5.5":
            raise RuntimeError("advisor unavailable")
        return _response("advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)
    facade = MoAChatCompletions("closed")
    captured = _collect_emits(facade)

    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "plan the migration"}],
    )

    progress = {kwargs["index"]: kwargs for event, kwargs in captured if event == "moa.progress"}
    assert progress[2]["label"] == "openrouter:openai/gpt-5.5"
    assert progress[2]["status"] == "failed"
    assert progress[1]["status"] == "complete"
    assert progress[3]["status"] == "complete"






def test_moa_progress_callback_none_safe(moa_config, monkeypatch):
    """A missing ``reference_callback`` does not break the fan-out or create()."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    # No callback attached — the facade's _emit is a no-op in that case.
    facade = MoAChatCompletions("closed")
    assert facade.reference_callback is None
    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "noop"}],
    )
    # Turn still resolved cleanly; aggregator slot populated as usual.
    assert facade.last_aggregator_slot is not None
    assert facade.last_aggregator_slot["model"] == "anthropic/claude-opus-4.8"