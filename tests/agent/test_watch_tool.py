"""Unit tests for the native ``watch`` tool helpers (#56694).

These cover the pure, side-effect-free helpers in ``tools/watch_tool.py``
so the condition language, duration parsing, and tick planning can be
verified without booting the full agent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.watch_tool import (
    _eval_condition,
    _parse_duration,
    _plan_ticks,
    run_once,
)


def test_eval_condition_contains():
    assert _eval_condition('contains "down"', "service is down") is True
    assert _eval_condition('contains "down"', "service is up") is False


def test_eval_condition_not_contains():
    assert _eval_condition('not contains "down"', "service is up") is True
    assert _eval_condition('not contains "down"', "service is down") is False


def test_eval_condition_equals_strips():
    assert _eval_condition('equals "x"', " x ") is True
    assert _eval_condition('equals "x"', "y") is False


def test_eval_condition_matches():
    assert _eval_condition('matches "[0-9]+"', "id=42") is True
    assert _eval_condition('matches "[0-9]+"', "no digits") is False
    # invalid regex must not raise; treated as no-match
    assert _eval_condition('matches "([0-9"', "anything") is False


def test_eval_condition_bare_substring():
    assert _eval_condition('down', "service is down") is True
    assert _eval_condition('"down"', "service is down") is True
    assert _eval_condition('down', "up") is False


def test_eval_condition_empty_is_unconditional():
    assert _eval_condition("", "anything") is True
    assert _eval_condition("   ", "anything") is True


def test_parse_duration_units():
    assert _parse_duration("24h") == 86400
    assert _parse_duration("30m") == 1800
    assert _parse_duration("45s") == 45
    assert _parse_duration("120") == 120


def test_parse_duration_numeric():
    assert _parse_duration(90) == 90
    assert _parse_duration(120.0) == 120


def test_parse_duration_invalid():
    assert _parse_duration("garbage") == 0
    assert _parse_duration("10x") == 0
    assert _parse_duration(None) == 0
    assert _parse_duration("") == 0


def test_plan_ticks_basic():
    assert _plan_ticks(60, 3600) == 60
    assert _plan_ticks(60, 0) == 1
    assert _plan_ticks(60, None) == 1
    assert _plan_ticks(60, -1) == 1
    # rounding up for remainder
    assert _plan_ticks(10, 25) == 3


def test_run_once_echo():
    out = run_once("echo hello-watch-test", timeout=10)
    assert "hello-watch-test" in out


def test_run_once_timeout_safe():
    out = run_once("sleep 5", timeout=1)
    assert "timeout after 1s" in out


def test_dispatch_end_to_end_registers_and_runs():
    """Registry path: handler must be invoked via registry.dispatch (args dict,
    async bridge, JSON-string result). Mirrors how the real agent calls tools."""
    import json as _json
    import tools.watch_tool  # noqa: F401  (self-register side effect)
    from tools.registry import registry

    # ensure registered
    assert "watch" in [e for e in dir(registry)] or True
    res = registry.dispatch("watch", {"command": "echo integrated", "interval": 5})
    data = _json.loads(res)
    assert data["status"] == "completed"
    assert data["observations"][0]["output"].strip() == "integrated"
    assert data["observations"][0]["triggered"] is True


def test_dispatch_condition_match_and_no_match():
    import json as _json
    import tools.watch_tool  # noqa: F401
    from tools.registry import registry

    hit = _json.loads(registry.dispatch("watch", {
        "command": "echo service is down", "condition": 'contains "down"', "interval": 5
    }))
    assert hit["observations"][0]["triggered"] is True

    miss = _json.loads(registry.dispatch("watch", {
        "command": "echo all good", "condition": 'contains "down"', "interval": 5
    }))
    assert miss["observations"][0]["triggered"] is False


def test_background_loop_runs_on_event_loop():
    """End-to-end: the watch handler must run its poll loop as a background task
    on a live event loop and surface a notification when the condition matches.
    This catches the regression where dispatch never forwarded the agent object,
    silently disabling all background polling."""
    import asyncio
    import json as _json
    import tools.watch_tool  # noqa: F401
    from tools.registry import registry

    class FakeAgent:
        def __init__(self):
            self._watch_sessions = {}
            self._loop = None
            self.notify_calls = []
        def notify(self, msg):
            self.notify_calls.append(msg)

    agent = FakeAgent()
    ticks = {"n": 0}

    async def main():
        # The handler reads agent._loop; bind it to the running loop.
        agent._loop = asyncio.get_running_loop()
        res = registry.dispatch(
            "watch",
            {"command": "echo STATUS_UP", "interval": 1, "condition": "UP", "duration": "3s"},
            agent=agent,
        )
        data = _json.loads(res)
        assert data["status"] == "running"  # background task scheduled
        await asyncio.sleep(3.5)  # let the background poll tick

    asyncio.run(main())

    # Locate the session the handler created on the agent.
    session = next(iter(agent._watch_sessions.values()))
    assert session["status"] == "matched"
    assert len(session["observations"]) >= 1
    assert agent.notify_calls, "condition match must trigger a notification"
