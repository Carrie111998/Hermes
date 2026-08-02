"""Tests for CLI external-editor support."""

import asyncio
import logging
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import DummyInput
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from cli import HermesCLI


class _FakeBuffer:
    def __init__(self, text=""):
        self.calls = []
        self.text = text
        self.cursor_position = len(text)

    def open_in_editor(self, validate_and_handle=False):
        self.calls.append(validate_and_handle)


class _FakeApp:
    def __init__(self):
        self.current_buffer = _FakeBuffer()


def _make_cli(with_app=True):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._app = _FakeApp() if with_app else None
    cli_obj._command_running = False
    cli_obj._command_status = ""
    cli_obj._command_display = ""
    cli_obj._sudo_state = None
    cli_obj._secret_state = None
    cli_obj._approval_state = None
    cli_obj._clarify_state = None
    cli_obj._skip_paste_collapse = False
    return cli_obj


def _make_real_editor_cli(text="private draft"):
    buffer = Buffer()
    buffer.text = text
    app = Application(
        layout=Layout(Window(content=BufferControl(buffer=buffer))),
        input=DummyInput(),
        output=DummyOutput(),
    )
    cli_obj = _make_cli(with_app=False)
    cli_obj._app = app
    cli_obj._agent_running = True
    cli_obj.busy_input_mode = "smart"
    cli_obj._enqueue_smart_cli_input = MagicMock(return_value=True)
    return cli_obj, app, buffer

def test_open_external_editor_uses_prompt_toolkit_buffer_editor():
    cli_obj = _make_cli()

    assert cli_obj._open_external_editor() is True
    assert cli_obj._app.current_buffer.calls == [True]


@pytest.mark.asyncio
async def test_real_prompt_toolkit_cancelled_editor_task_does_not_submit():
    cli_obj, app, buffer = _make_real_editor_cli()

    with (
        create_app_session(input=app.input, output=app.output),
        set_app(app),
        patch("prompt_toolkit.buffer.subprocess.call", return_value=0),
    ):
        assert cli_obj._open_external_editor(buffer=buffer) is True
        tasks = list(app._background_tasks)
        assert len(tasks) == 1
        task = tasks[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    cli_obj._enqueue_smart_cli_input.assert_not_called()
    assert buffer.text == "private draft"


@pytest.mark.asyncio
async def test_real_prompt_toolkit_failed_editor_process_does_not_submit():
    cli_obj, app, buffer = _make_real_editor_cli()

    with (
        create_app_session(input=app.input, output=app.output),
        set_app(app),
        patch("prompt_toolkit.buffer.subprocess.call", return_value=1),
    ):
        assert cli_obj._open_external_editor(buffer=buffer) is True
        tasks = list(app._background_tasks)
        assert len(tasks) == 1
        await tasks[0]
        await asyncio.sleep(0)

    cli_obj._enqueue_smart_cli_input.assert_not_called()
    assert buffer.text == "private draft"


@pytest.mark.asyncio
async def test_real_prompt_toolkit_editor_success_submits_through_smart():
    cli_obj, app, buffer = _make_real_editor_cli()

    def _save_edited_draft(argv):
        Path(argv[-1]).write_text("edited via real buffer\n", encoding="utf-8")
        return 0

    with (
        create_app_session(input=app.input, output=app.output),
        set_app(app),
        patch(
            "prompt_toolkit.buffer.subprocess.call",
            side_effect=_save_edited_draft,
        ),
    ):
        assert cli_obj._open_external_editor(buffer=buffer) is True
        tasks = list(app._background_tasks)
        assert len(tasks) == 1
        await tasks[0]
        await asyncio.sleep(0)

    cli_obj._enqueue_smart_cli_input.assert_called_once_with(
        "edited via real buffer"
    )
    assert buffer.text == ""
    assert buffer.accept_handler is None


@pytest.mark.asyncio
async def test_real_prompt_toolkit_editor_exception_is_private_and_not_submitted(
    caplog,
):
    cli_obj, app, buffer = _make_real_editor_cli()

    with (
        create_app_session(input=app.input, output=app.output),
        set_app(app),
        patch(
            "prompt_toolkit.buffer.subprocess.call",
            side_effect=RuntimeError("private-editor-exception"),
        ),
        patch("cli._cprint") as mock_cprint,
        caplog.at_level(logging.DEBUG),
    ):
        assert cli_obj._open_external_editor(buffer=buffer) is True
        tasks = list(app._background_tasks)
        assert len(tasks) == 1
        await tasks[0]
        await asyncio.sleep(0)

    cli_obj._enqueue_smart_cli_input.assert_not_called()
    assert buffer.text == "private draft"
    diagnostics = caplog.text + str(mock_cprint.call_args_list)
    assert "private draft" not in diagnostics
    assert "private-editor-exception" not in diagnostics
    assert "Traceback" not in diagnostics
    assert "RuntimeError" in diagnostics


def test_open_external_editor_rejects_when_no_tui():
    cli_obj = _make_cli(with_app=False)

    with patch("cli._cprint") as mock_cprint:
        assert cli_obj._open_external_editor() is False

    assert mock_cprint.called
    assert "interactive cli" in str(mock_cprint.call_args).lower()



    assert cli_obj._open_external_editor(buffer=external_buffer) is True
    assert external_buffer.calls == [True]
    assert cli_obj._app.current_buffer.calls == []


def test_expand_paste_references_replaces_opaque_placeholder_with_contents(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli_obj = _make_cli()
    placeholder = cli_obj._store_private_paste_reference(
        "line one\nline two",
        display_index=1,
        line_count=2,
    )
    assert placeholder is not None

    text = f"before {placeholder} after"
    expanded = cli_obj._expand_paste_references(text)

    assert expanded == "before line one\nline two after"


def test_open_external_editor_expands_opaque_paste_before_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli_obj = _make_cli()
    placeholder = cli_obj._store_private_paste_reference(
        "alpha\nbeta",
        display_index=1,
        line_count=2,
    )
    assert placeholder is not None
    buffer = _FakeBuffer(text=placeholder)

    assert cli_obj._open_external_editor(buffer=buffer) is True
    assert buffer.text == "alpha\nbeta"
    assert buffer.cursor_position == len("alpha\nbeta")
    assert buffer.calls == [True]


def test_open_external_editor_sets_skip_collapse_flag_during_opaque_expansion(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli_obj = _make_cli()
    placeholder = cli_obj._store_private_paste_reference(
        "a\nb\nc\nd\ne\nf",
        display_index=1,
        line_count=6,
    )
    assert placeholder is not None
    buffer = _FakeBuffer(text=placeholder)

    # After expansion the flag should have been set (to prevent re-collapse)
    assert cli_obj._open_external_editor(buffer=buffer) is True
    # Flag is consumed by _on_text_changed, but since no handler is attached
    # in tests it stays True until the handler resets it.
    assert cli_obj._skip_paste_collapse is True


def test_inline_pastes_stores_full_content(tmp_path):
    """History should recall the actual pasted text, not the placeholder."""
    cli_obj = _make_cli()
    paste_file = tmp_path / "paste.txt"
    paste_file.write_text("line one\nline two", encoding="utf-8")
    buffer = _FakeBuffer(text=f"[Pasted text #1: 2 lines \u2192 {paste_file}]")

    cli_obj._inline_pastes(buffer)

    assert buffer.text == "line one\nline two"
    assert buffer.cursor_position == len("line one\nline two")
    # Skip flag set so the resulting text-change doesn't re-collapse.
    assert cli_obj._skip_paste_collapse is True




def test_inline_pastes_missing_file_keeps_placeholder(tmp_path):
    """A recalled reference whose file is gone stays as the placeholder."""
    cli_obj = _make_cli()
    placeholder = f"[Pasted text #1: 2 lines \u2192 {tmp_path / 'gone.txt'}]"
    buffer = _FakeBuffer(text=placeholder)

    cli_obj._inline_pastes(buffer)

    assert buffer.text == placeholder
    assert cli_obj._skip_paste_collapse is False
