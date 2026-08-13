"""Unit tests for the live-time plugin."""

from __future__ import annotations

import re
from datetime import datetime
from unittest.mock import MagicMock

from plugins.live_time import _on_pre_llm_call, register


def test_on_pre_llm_call_returns_live_timestamp_context() -> None:
    out = _on_pre_llm_call()
    assert out is not None
    text = out["context"]
    assert "[LIVE-TIME] Now:" in text

    m = re.search(r"Now: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    assert m, f"missing timestamp in {text!r}"
    parsed = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    assert abs((datetime.now() - parsed).total_seconds()) < 30


def test_context_marks_itself_authoritative() -> None:
    text = _on_pre_llm_call()["context"]
    assert "Use THIS as the authoritative current time" in text
    assert "Conversation started" in text


def test_register_hooks_pre_llm_call() -> None:
    ctx = MagicMock()
    register(ctx)
    ctx.register_hook.assert_called_once_with("pre_llm_call", _on_pre_llm_call)
