"""Regression tests for the CLIModalPromptsMixin extraction.

God-file decomposition Wave 1 (cli.py shard s4, cluster c8): the modal-prompt
methods (approval / clarify / sudo / secret) moved verbatim from
``cli.py``'s ``HermesCLI`` into ``hermes_cli/cli_modal_prompts_mixin.py``.
``HermesCLI`` now inherits ``CLIModalPromptsMixin``, so the behavior is
identical via the MRO.

These tests exercise the mixin through a bare stub host (no HermesCLI, no
prompt_toolkit) and stub the ``cli`` module so the lazy ``from cli import ...``
lines resolve without importing the full CLI (same isolation trick as
``tests/cli/test_cli_extension_hooks.py``).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.cli_modal_prompts_mixin import CLIModalPromptsMixin


class _Stub(CLIModalPromptsMixin):
    """Bare mixin host: each test sets only the attributes it exercises."""


@pytest.fixture(autouse=True)
def _cli_stub():
    """Stub the cli module so lazy imports inside moved methods resolve."""
    cli = MagicMock()
    cli._cprint = lambda *a, **k: None
    cli._DIM = ""
    cli._RST = ""
    cli._ACCENT = ""
    cli._BOLD = ""
    cli.CLI_CONFIG = {}
    with patch.dict(sys.modules, {"cli": cli}):
        yield


# --------------------------------------------------------------------------
# _approval_choices — pure choice-list construction
# --------------------------------------------------------------------------

def test_approval_choices_default():
    stub = _Stub()
    assert stub._approval_choices("rm -rf /") == ["once", "session", "always", "deny"]


def test_approval_choices_no_permanent():
    stub = _Stub()
    assert stub._approval_choices("rm -rf /", allow_permanent=False) == ["once", "session", "deny"]


def test_approval_choices_smart_denied():
    stub = _Stub()
    assert stub._approval_choices("rm -rf /", smart_denied=True) == ["once", "deny"]


def test_approval_choices_long_command_adds_view():
    stub = _Stub()
    long_cmd = "x" * 80
    assert stub._approval_choices(long_cmd) == ["once", "session", "always", "deny", "view"]


# --------------------------------------------------------------------------
# _computer_use_approval_callback — verdict translation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("once", "approve_once"),
        ("session", "approve_session"),
        ("always", "always_approve"),
        ("deny", "deny"),
        ("timeout", "timeout"),
        ("unexpected", "deny"),
    ],
)
def test_computer_use_verdict_mapping(verdict, expected):
    stub = _Stub()
    stub._approval_callback = MagicMock(return_value=verdict)
    result = stub._computer_use_approval_callback("click", {"x": 1}, "click at 10,10")
    assert result == expected
    stub._approval_callback.assert_called_once()
    call = stub._approval_callback.call_args
    assert call.kwargs["command"].startswith("computer_use:")
    assert "click at 10,10" in call.kwargs["command"]


# --------------------------------------------------------------------------
# _get_approval_display_fragments — pure panel rendering
# --------------------------------------------------------------------------

def test_approval_fragments_empty_state():
    stub = _Stub()
    stub._approval_state = None
    assert stub._get_approval_display_fragments() == []


def _state_with(command="rm -rf /", choices=None, selected=0):
    return {
        "command": command,
        "description": "Delete everything",
        "choices": choices if choices is not None else ["once", "session", "always", "deny"],
        "selected": selected,
        "response_queue": MagicMock(),
    }


def _is_blank_separator(frag):
    """A blank separator row is a single fragment with only borders/spaces."""
    return frag.count("│") >= 2 and frag.strip("│ \n") == ""


def test_approval_fragments_renders_full_panel():
    stub = _Stub()
    stub._approval_state = _state_with()
    with patch("shutil.get_terminal_size", return_value=SimpleNamespace(columns=120, lines=40)):
        rendered = stub._get_approval_display_fragments()
    text = "".join(frag for _, frag in rendered)
    assert "╭" in text and "╰" in text
    assert "Dangerous Command" in text
    assert "Allow once" in text
    assert "Add to permanent allowlist" in text
    # full chrome: a blank separator row exists between title and choices
    assert any(_is_blank_separator(frag) for _, frag in rendered)


def test_approval_fragments_compact_chrome_in_short_terminal():
    stub = _Stub()
    stub._approval_state = _state_with()
    with patch("shutil.get_terminal_size", return_value=SimpleNamespace(columns=120, lines=12)):
        rendered = stub._get_approval_display_fragments()
    text = "".join(frag for _, frag in rendered)
    assert "╭" in text
    assert "Allow once" in text
    # compact chrome: no blank separator rows
    assert not any(_is_blank_separator(frag) for _, frag in rendered)


def test_approval_fragments_truncates_overlong_command():
    stub = _Stub()
    stub._approval_state = _state_with(command="z" * 400, choices=["once", "deny"])
    with patch("shutil.get_terminal_size", return_value=SimpleNamespace(columns=100, lines=12)):
        rendered = stub._get_approval_display_fragments()
    text = "".join(frag for _, frag in rendered)
    assert "truncated" in text
    assert "Deny" in text  # choices still render


# --------------------------------------------------------------------------
# _handle_approval_selection — state-machine transitions
# --------------------------------------------------------------------------

def test_handle_approval_selection_no_state_noop():
    stub = _Stub()
    stub._approval_state = None
    stub._invalidate = MagicMock()
    stub._handle_approval_selection()  # must not raise


def test_handle_approval_selection_submits_chosen():
    stub = _Stub()
    queue_mock = MagicMock()
    stub._approval_state = _state_with(choices=["once", "deny"], selected=0)
    stub._approval_state["response_queue"] = queue_mock
    stub._invalidate = MagicMock()
    stub._handle_approval_selection()
    queue_mock.put.assert_called_once_with("once")
    assert stub._approval_state is None
    stub._invalidate.assert_called_once()


def test_handle_approval_selection_view_expands_command():
    stub = _Stub()
    stub._approval_state = _state_with(choices=["once", "session", "deny", "view"], selected=3)
    stub._invalidate = MagicMock()
    stub._handle_approval_selection()
    assert stub._approval_state["show_full"] is True
    assert "view" not in stub._approval_state["choices"]
    assert stub._approval_state["response_queue"].put.call_count == 0
    stub._invalidate.assert_called_once()


def test_handle_approval_selection_out_of_range_noop():
    stub = _Stub()
    stub._approval_state = _state_with(choices=["once", "deny"], selected=7)
    stub._invalidate = MagicMock()
    stub._handle_approval_selection()
    assert stub._approval_state["response_queue"].put.call_count == 0
    assert stub._approval_state is not None


# --------------------------------------------------------------------------
# modal input snapshot / secret capture helpers
# --------------------------------------------------------------------------

def test_capture_restore_modal_input_snapshot_roundtrip():
    stub = _Stub()
    stub._modal_input_snapshot = None
    buf = MagicMock()
    buf.text = "half-typed draft"
    buf.cursor_position = 7
    app = MagicMock()
    app.current_buffer = buf
    stub._app = app

    stub._capture_modal_input_snapshot()
    assert stub._modal_input_snapshot == {"text": "half-typed draft", "cursor_position": 7}
    buf.reset.assert_called_once()

    buf.text = ""
    buf.cursor_position = 0
    stub._restore_modal_input_snapshot()
    assert buf.text == "half-typed draft"
    assert buf.cursor_position == 7


def test_capture_modal_snapshot_skips_without_app():
    stub = _Stub()
    stub._modal_input_snapshot = None
    stub._app = None
    stub._capture_modal_input_snapshot()
    assert stub._modal_input_snapshot is None


def test_restore_modal_snapshot_clears_even_without_app():
    stub = _Stub()
    stub._modal_input_snapshot = {"text": "x", "cursor_position": 0}
    stub._app = None
    stub._restore_modal_input_snapshot()  # must not raise
    assert stub._modal_input_snapshot is None


def test_clear_secret_input_buffer_resets_app_buffer():
    stub = _Stub()
    buf = MagicMock()
    app = MagicMock()
    app.current_buffer = buf
    stub._app = app
    stub._clear_secret_input_buffer()
    buf.reset.assert_called_once()


def test_clear_secret_input_buffer_no_app_noop():
    stub = _Stub()
    stub._app = None
    stub._clear_secret_input_buffer()  # must not raise


def test_secret_capture_callback_forwards_to_prompt_for_secret():
    stub = _Stub()
    with patch(
        "hermes_cli.cli_modal_prompts_mixin.prompt_for_secret",
        return_value={"ok": True},
    ) as pfs:
        result = stub._secret_capture_callback("API_KEY", "Enter key", {"k": 1})
    assert result == {"ok": True}
    pfs.assert_called_once_with(stub, "API_KEY", "Enter key", {"k": 1})


# --------------------------------------------------------------------------
# _clear_active_overlays_for_interrupt — drain every blocked prompt queue
# --------------------------------------------------------------------------

def test_clear_active_overlays_drains_queues_and_nils_state():
    stub = _Stub()
    stub._modal_input_snapshot = None
    stub._app = None
    stub._paint_now = MagicMock()
    approval_q = MagicMock()
    clarify_q = MagicMock()
    sudo_q = MagicMock()
    stub._approval_state = {"response_queue": approval_q}
    stub._clarify_state = {"response_queue": clarify_q}
    stub._clarify_freetext = True
    stub._clarify_multi_base = "x"
    stub._sudo_state = {"response_queue": sudo_q}
    stub._sudo_deadline = 123
    stub._secret_state = None

    stub._clear_active_overlays_for_interrupt()

    approval_q.put.assert_called_once_with("deny")
    clarify_q.put.assert_called_once()
    sudo_q.put.assert_called_once_with("")
    assert stub._approval_state is None
    assert stub._clarify_state is None
    assert stub._clarify_freetext is False
    assert stub._clarify_multi_base is None
    assert stub._sudo_state is None
    assert stub._sudo_deadline == 0


def test_clear_active_overlays_cancels_secret_capture():
    stub = _Stub()
    stub._modal_input_snapshot = None
    stub._app = None
    stub._paint_now = MagicMock()
    secret_q = MagicMock()
    stub._secret_state = {"response_queue": secret_q}
    stub._approval_state = None
    stub._clarify_state = None
    stub._sudo_state = None

    stub._clear_active_overlays_for_interrupt()

    secret_q.put.assert_called_once_with("")
    assert stub._secret_state is None
    assert stub._secret_deadline == 0
