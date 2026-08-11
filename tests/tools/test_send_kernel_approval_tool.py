"""Tests for send_kernel_approval_tool.py's Telegram message construction.

Phase 3 Packet 8: Packet 7A's adversarial audit found this tool interpolates
kernel-ledger content (`reason`, `new_value`) and agent tool-call arguments
(`project`, `proposal_id`) into a `parse_mode="Markdown"` (legacy) message
with NO escaping at all — a `reason` or `project` containing Markdown
metacharacters (backtick, asterisk, underscore, backslash) could break the
message's entity structure, at best mis-rendering and at worst causing
Telegram's API to reject the send outright (an unbalanced-entity error),
which would silently fail to deliver the approval request at all.

These tests never make a real Telegram call — `telegram.Bot.send_message`
is patched to an AsyncMock that captures its arguments. `InlineKeyboardButton`/
`InlineKeyboardMarkup`/`Bot(token=...)` construction is real (the actual,
already-installed python-telegram-bot library) since none of that touches
the network — only `send_message` does.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import telegram  # noqa: E402 - real, already-installed library

from tools.send_kernel_approval_tool import _code_span, _send_approval_request  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _send(project="proj_a", proposal_id="prop_123", field="status",
          new_value="ok", reason="Looks good."):
    """Call _send_approval_request with a mocked bot.send_message, return
    (captured_text, captured_parse_mode, captured_reply_markup)."""
    fake_message = type("M", (), {"message_id": 42})()
    with patch.object(telegram.Bot, "send_message", new=AsyncMock(return_value=fake_message)) as mock_send:
        result = _run(_send_approval_request(
            "fake-token", "12345", project, proposal_id, field, new_value, reason,
        ))
    assert result["success"] is True
    kwargs = mock_send.call_args.kwargs
    return kwargs["text"], kwargs["parse_mode"], kwargs["reply_markup"]


# ---------------------------------------------------------------------------
# _code_span unit tests
# ---------------------------------------------------------------------------

def test_code_span_escapes_backtick():
    assert _code_span("a`b") == r"a\`b"


def test_code_span_escapes_backslash():
    assert _code_span("a\\b") == r"a\\b"


def test_code_span_leaves_asterisk_underscore_unescaped():
    """Inside a MarkdownV2 code span, *and_* are inert — escaping them would
    be WRONG (it would insert a literal backslash into the displayed text)."""
    assert _code_span("fix_operator*bold*") == "fix_operator*bold*"


def test_code_span_leaves_brackets_unescaped():
    assert _code_span("list[0](x)") == "list[0](x)"


def test_code_span_stringifies_non_str():
    assert _code_span(None) == "None"
    assert _code_span(42) == "42"


# ---------------------------------------------------------------------------
# End-to-end message construction (captures the real bot.send_message call)
# ---------------------------------------------------------------------------

def test_ordinary_values_render_with_all_fields_present():
    text, parse_mode, _ = _send(
        project="prj_nex_trends", proposal_id="prop_abc123", field="status",
        new_value="active", reason="Routine update.",
    )
    assert "prj_nex_trends" in text
    assert "prop_abc123" in text
    assert "status" in text
    assert "active" in text
    assert "Routine update." in text


def test_underscore_in_project_safely_preserved():
    text, _, _ = _send(project="prj_nex_trends")
    assert "prj_nex_trends" in text  # not garbled/escaped-looking
    assert "prj\\_nex\\_trends" not in text  # not over-escaped either


def test_asterisk_in_reason_safely_preserved():
    text, _, _ = _send(reason="Approved *urgently* per operator request.")
    assert "Approved *urgently* per operator request." in text


def test_backtick_in_project_regression_fixture():
    """The exact defect class found in Packet 7A: pre-fix, an embedded
    backtick in an interpolated value broke the surrounding code span —
    verified directly against the pre-fix implementation for this exact
    input: it produced `proj_a`b` (a literal, unescaped backtick splitting
    one intended code span into two fragments, 9 total backticks in the
    message with the entity structure broken). Post-fix, the embedded
    backtick is escaped (`\\``) so it stays literal content inside a single,
    intact code span rather than prematurely closing it."""
    text, _, _ = _send(project="proj_a`b")
    assert "*Project:* `proj_a\\`b`\n" in text
    # The escaped backtick must NOT be treated as a delimiter: exactly two
    # UN-escaped backticks (the span's real open/close) surround this field.
    field_line = [ln for ln in text.splitlines() if ln.startswith("*Project:*")][0]
    import re
    unescaped_backticks = len(re.findall(r"(?<!\\)`", field_line))
    assert unescaped_backticks == 2


def test_brackets_in_field_safely_preserved():
    text, _, _ = _send(field="config[env]")
    assert "config[env]" in text


def test_parentheses_link_shaped_reason_safely_preserved():
    text, _, _ = _send(reason="See (https://example.com/doc) for context.")
    assert "(https://example.com/doc)" in text


def test_backslash_in_new_value_safely_preserved():
    """new_value passes through two independent encodings — json.dumps()
    (escapes \\ -> \\\\), then the code-span escaper (escapes \\ -> \\\\
    again) — so a single source backslash legitimately becomes 4 backslash
    characters in the rendered message. That's correct, composed escaping,
    not a bug: each layer must survive the other being applied afterward."""
    text, _, _ = _send(new_value="C:\\path\\to\\file")
    assert _code_span(json.dumps("C:\\path\\to\\file")) in text


def test_multiline_reason_uses_pre_block_and_is_preserved():
    """Explicit contract (Phase 3 Packet 8): reason may contain newlines (a
    multi-sentence kernel-proposal explanation), so it's rendered in a
    triple-backtick pre block rather than a single-backtick inline span,
    which does not reliably support embedded newlines."""
    reason = "First, the config was updated.\nSecond, tests were re-run.\nAll green."
    text, _, _ = _send(reason=reason)
    assert "```\n" + reason + "\n```" in text


def test_mixed_hostile_characters_do_not_escape_their_field():
    """A hostile combination in one field must not corrupt adjacent fields."""
    text, _, _ = _send(
        project="a`*_\\b",
        field="normal_field",
        reason="c`*_\\d",
    )
    escaped_project = _code_span("a`*_\\b")
    escaped_reason = _code_span("c`*_\\d")
    assert f"*Project:* `{escaped_project}`\n" in text
    assert f"```\n{escaped_reason}\n```" in text
    # The untouched middle field is completely unaffected.
    assert "normal_field" in text


def test_callback_buttons_and_data_unchanged():
    _, _, reply_markup = _send(proposal_id="prop_xyz")
    buttons = reply_markup.inline_keyboard[0]
    assert buttons[0].text == "✅ Approve"
    assert buttons[0].callback_data == "kp:approve:prop_xyz"
    assert buttons[1].text == "❌ Reject"
    assert buttons[1].callback_data == "kp:reject:prop_xyz"


def test_callback_data_not_escaped_even_with_hostile_proposal_id():
    """callback_data is opaque Telegram byte data, never Markdown-parsed —
    it must carry the RAW proposal_id, not the escaped display version, or
    the approve/reject button would target the wrong proposal."""
    _, _, reply_markup = _send(proposal_id="prop_`weird`_id")
    buttons = reply_markup.inline_keyboard[0]
    assert buttons[0].callback_data == "kp:approve:prop_`weird`_id"
    assert buttons[1].callback_data == "kp:reject:prop_`weird`_id"


def test_parse_mode_is_markdownv2():
    _, parse_mode, _ = _send()
    assert parse_mode == "MarkdownV2"


def _imported_names(source: str) -> set:
    """Names actually imported by *source* (AST-based, not substring search —
    the module's own docstring names cron_reply/TelegramAdapter/format_message
    in prose explaining why it deliberately does NOT depend on them, which
    would false-positive a plain substring check)."""
    import ast

    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    return names


def test_does_not_route_through_cron_sanitizer():
    import tools.send_kernel_approval_tool as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    imported = _imported_names(source)
    assert "cron_reply" not in imported
    assert "sanitize_cron_reply" not in imported


def test_does_not_route_through_generic_renderer():
    """Packet 7A established this is an intentional static delivery
    exception — it must not gain a dependency on TelegramAdapter/
    format_message merely to fix escaping (Packet 8's explicit scope: fix
    escaping, don't reroute)."""
    import tools.send_kernel_approval_tool as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    imported = _imported_names(source)
    assert "TelegramAdapter" not in imported
    assert "gateway.platforms.telegram" not in imported
    assert "format_message" not in imported


def test_all_required_fields_present_in_message():
    text, _, _ = _send(
        project="prj_x", proposal_id="prop_y", field="f", new_value="v", reason="r",
    )
    for expected in ("prj_x", "prop_y", "f", "v", "r"):
        assert expected in text


# ---------------------------------------------------------------------------
# Bypass-site enumeration (Phase 3 Packet 8, 8.7)
#
# Every direct `Bot(token=...).send_message(...)` construction site in the
# codebase (i.e. every Telegram send that bypasses TelegramAdapter.send())
# must be a KNOWN, explicitly allowlisted exception, not an undiscovered
# one. This is a narrow, stable repo grep, not a general static analyzer —
# if a new site appears, this test fails and forces a conscious decision
# (route it through the adapter, or add it to the allowlist with the same
# "intentional exception" reasoning documented here).
# ---------------------------------------------------------------------------

_KNOWN_DIRECT_BOT_SEND_SITES = {
    "tools/send_kernel_approval_tool.py",  # this tool — INTENTIONAL STATIC DELIVERY EXCEPTION
    "tools/send_message_tool.py",  # pre-existing, Phase 3 investigation (out of this packet's scope)
}


def test_no_undiscovered_direct_bot_send_sites():
    import re
    import subprocess

    hermes_agent_root = Path(__file__).parent.parent.parent
    out = subprocess.run(
        ["grep", "-rl", "-E", r"Bot\(token=", "--include=*.py", "."],
        cwd=str(hermes_agent_root), capture_output=True, text=True,
    ).stdout
    hits = {
        line.lstrip("./") for line in out.splitlines()
        if "/tests/" not in line and "/venv/" not in line and "__pycache__" not in line
    }
    assert hits == _KNOWN_DIRECT_BOT_SEND_SITES, (
        f"direct Bot(token=...) construction sites changed: {hits!r} vs known "
        f"{_KNOWN_DIRECT_BOT_SEND_SITES!r} — a new bypass site was added (or a "
        f"known one removed) without updating this allowlist"
    )
