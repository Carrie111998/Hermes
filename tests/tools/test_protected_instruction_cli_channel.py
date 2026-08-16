"""Protected-instruction approval must be answerable on an interactive CLI
session, and must NOT pretend a human channel exists in a headless one-shot
(`hermes chat -q`) session.

Defect (kanban t_9691f08d): every write to a protected agent-instruction file
from a kanban worker failed with

    BLOCKED: ... approval prompt timed out without a user response.
    Silence is not consent.

which reads as "a human ignored the prompt". In fact those workers are spawned
as ``hermes chat -q``, which never builds a prompt_toolkit Application — but
``HermesCLI.__init__`` had already registered ``_approval_callback`` as the
thread-local CLI approval callback. ``_request_protected_instruction_approval``
therefore saw a callback, believed a human channel existed, pushed a modal into
a layout that is never rendered, and blocked on a response queue no key binding
could ever fill until the approval timeout expired.

Two invariants are locked here:

1. Interactive CLI: a registered approval callback that answers ``once`` lets
   the write through, and ``deny`` blocks it. The channel works.
2. Headless one-shot: the modal callbacks are cleared, so the gate fails closed
   *immediately* with the honest "no interactive user or gateway is present"
   message instead of manufacturing a timeout.
"""

import json
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    import tools.file_tools as ft
    monkeypatch.setattr(ft, "_protected_instruction_config", lambda: (True, []))
    yield


@pytest.fixture(autouse=True)
def _clean_callbacks():
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)
    yield
    set_approval_callback(None)


def _write(path, content="content"):
    from tools.file_tools import write_file_tool
    return json.loads(write_file_tool(str(path), content))


class TestInteractiveCliChannelDelivers:
    """An interactive CLI session can actually answer the prompt."""

    def test_interactive_approval_allows_write(self, tmp_path):
        from tools.terminal_tool import set_approval_callback

        seen = []

        def cb(command, description, **kwargs):
            seen.append((command, description, kwargs))
            return "once"

        set_approval_callback(cb)
        target = tmp_path / "SOUL.md"
        res = _write(target, "approved by a live human")

        assert not res.get("error"), res
        assert target.read_text(encoding="utf-8") == "approved by a live human"
        assert len(seen) == 1, "the human channel was never used"
        assert "SOUL.md" in seen[0][0]
        # One-operation only: no session/permanent scope is offered.
        assert seen[0][2].get("allow_permanent") is False

    def test_interactive_denial_blocks_write(self, tmp_path):
        from tools.terminal_tool import set_approval_callback
        set_approval_callback(lambda c, d, **k: "deny")

        target = tmp_path / "SOUL.md"
        res = _write(target)
        assert res.get("error") and "BLOCKED" in res["error"]
        assert "denied by the user" in res["error"]
        assert not target.exists()

    def test_real_cli_approval_modal_round_trip(self, tmp_path):
        """End-to-end through the actual HermesCLI modal, not a stub callback.

        Drives ``HermesCLI._approval_callback`` — the same function the CLI
        registers — from a worker thread, waits for the modal state the TUI
        renders, then answers it exactly as the Enter key binding does. This is
        the path that was unreachable for headless workers.
        """
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        from cli import HermesCLI
        from tools.terminal_tool import set_approval_callback

        cli = HermesCLI.__new__(HermesCLI)
        cli._approval_state = None
        cli._approval_deadline = 0
        cli._approval_lock = threading.Lock()
        cli._invalidate = MagicMock()
        cli._app = SimpleNamespace(invalidate=MagicMock())
        cli._paint_now = MagicMock()
        cli._persist_prompt_summary = MagicMock()

        answered = threading.Event()

        def _answer_when_prompted():
            deadline = time.time() + 5
            while time.time() < deadline:
                state = cli._approval_state
                if state is not None:
                    # The modal really was raised with a decidable choice set.
                    assert "once" in state["choices"]
                    assert "always" not in state["choices"]
                    state["response_queue"].put("once")
                    answered.set()
                    return
                time.sleep(0.01)

        threading.Thread(target=_answer_when_prompted, daemon=True).start()
        set_approval_callback(cli._approval_callback)

        target = tmp_path / "AGENTS.md"
        res = _write(target, "written after a real modal approval")

        assert answered.is_set(), "the approval modal never rendered"
        assert not res.get("error"), res
        assert target.read_text(encoding="utf-8") == "written after a real modal approval"


class TestHeadlessOneShotFailsClosedHonestly:
    """`hermes chat -q` must not claim a human channel it does not have."""

    def test_no_callback_reports_no_human_not_timeout(self, tmp_path):
        target = tmp_path / "SOUL.md"
        res = _write(target)
        assert res.get("error") and "BLOCKED" in res["error"]
        assert "no interactive user or gateway is present" in res["error"]
        assert "timed out" not in res["error"], (
            "headless run reported a timeout, implying a human ignored a "
            "prompt that was never rendered"
        )
        assert not target.exists()

    def test_disable_modal_prompt_callbacks_clears_and_latches(self):
        from unittest.mock import MagicMock

        from cli import HermesCLI
        from tools.terminal_tool import (
            _get_approval_callback,
            _get_sudo_password_callback,
            set_approval_callback,
        )

        cli = HermesCLI.__new__(HermesCLI)
        cli._approval_callback = MagicMock(return_value="once")
        cli._sudo_password_callback = MagicMock()
        cli._secret_capture_callback = MagicMock()
        cli._computer_use_approval_callback = MagicMock()

        set_approval_callback(cli._approval_callback)
        assert _get_approval_callback() is not None

        cli._disable_modal_prompt_callbacks()

        assert _get_approval_callback() is None
        assert _get_sudo_password_callback() is None

        # Latched: a later _install_tool_callbacks() must not silently
        # re-register the unreachable modal channel.
        cli._install_tool_callbacks()
        assert _get_approval_callback() is None
