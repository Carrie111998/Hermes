"""Slack-visible specialist delegation transcript behavior."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.run import TurnRunner
from gateway.turn_context import TurnContext


def _runner():
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1")
    ctx = TurnContext(
        source=source,
        _run_still_current=lambda: False,
        delegation_progress_enabled=True,
        delegation_lock=threading.Lock(),
    )
    owner = MagicMock()
    runner = TurnRunner(owner, ctx)
    runner._schedule_delegation_message = MagicMock()
    return runner, ctx


def test_profile_child_start_and_final_text_are_visible_after_parent_turn_ends():
    runner, _ = _runner()

    common = {
        "profile_name": "nami",
        "child_session_id": "child-1",
    }
    runner.progress_callback("subagent.start", preview="work", **common)
    runner.progress_callback("subagent.text", preview="재고 ", **common)
    runner.progress_callback("subagent.text", preview="확인 완료", **common)
    runner.progress_callback(
        "subagent.complete", status="completed", summary="ignored", **common
    )

    calls = runner._schedule_delegation_message.call_args_list
    assert calls[0].args[0] == "nami"
    assert "시작" in calls[0].args[1]
    assert "work" in calls[0].args[1]
    assert calls[1].args == ("nami", "재고 확인 완료")


def test_failed_child_marks_streamed_text_as_partial_failure():
    runner, _ = _runner()
    common = {"profile_name": "chopper", "child_session_id": "child-2"}
    runner.progress_callback("subagent.start", preview="verify", **common)
    runner.progress_callback("subagent.text", preview="partial", **common)
    runner.progress_callback("subagent.complete", status="failed", **common)
    final = runner._schedule_delegation_message.call_args_list[-1].args[1]
    assert final.startswith("⚠️")
    assert "failed" in final
    assert "partial" in final


def test_unnamed_subagents_stay_hidden():
    runner, _ = _runner()
    runner.progress_callback("subagent.start", preview="anonymous")
    runner.progress_callback("subagent.text", preview="secret scratch")
    runner.progress_callback("subagent.complete", summary="done")
    runner._schedule_delegation_message.assert_not_called()
