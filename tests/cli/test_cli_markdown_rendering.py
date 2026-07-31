import re
from io import StringIO

from rich.console import Console
from rich.markdown import Markdown

from cli import _render_final_assistant_content


def _render_to_text(renderable) -> str:
    buf = StringIO()
    Console(file=buf, width=80, force_terminal=False, color_system=None).print(renderable)
    return buf.getvalue()


def test_final_assistant_content_uses_markdown_renderable():
    renderable = _render_final_assistant_content("# Title\n\n- one\n- two")

    assert isinstance(renderable, Markdown)
    output = _render_to_text(renderable)
    assert "Title" in output
    assert "one" in output
    assert "two" in output




def test_final_assistant_content_keeps_non_path_markdown_escapes():
    renderable = _render_final_assistant_content(r"1\. Not an ordered list")

    output = _render_to_text(renderable)
    assert "1. Not an ordered list" in output
    assert r"1\." not in output






def test_strip_mode_preserves_lists():
    renderable = _render_final_assistant_content(
        "**Formatting**\n- Ran prettier\n- Files changed\n- Verified clean",
        mode="strip",
    )

    output = _render_to_text(renderable)
    assert "- Ran prettier" in output
    assert "- Files changed" in output
    assert "- Verified clean" in output
    assert "**" not in output




def test_strip_mode_preserves_blockquotes():
    renderable = _render_final_assistant_content(
        "> This is quoted text\n> Another quoted line",
        mode="strip",
    )

    output = _render_to_text(renderable)
    assert "> This is quoted" in output
    assert "> Another quoted" in output






def test_strip_mode_preserves_cron_asterisks_in_plain_text():
    renderable = _render_final_assistant_content("* * * * *", mode="strip")

    output = _render_to_text(renderable)
    assert "* * * * *" in output

    # Still treat the canonical 3-asterisk Markdown horizontal rule as decoration.
    renderable = _render_final_assistant_content("* * *", mode="strip")
    output = _render_to_text(renderable)
    assert "* * *" not in output




def test_strip_mode_preserves_intraword_underscores_in_snake_case_identifiers():
    renderable = _render_final_assistant_content(
        "Let me look at test_case_with_underscores and SOME_CONST "
        "then /tmp/snake_case_dir/file_with_name.py",
        mode="strip",
    )

    output = _render_to_text(renderable)
    assert "test_case_with_underscores" in output
    assert "SOME_CONST" in output
    assert "snake_case_dir" in output
    assert "file_with_name" in output


def test_strip_mode_still_strips_boundary_underscore_emphasis():
    renderable = _render_final_assistant_content(
        "say _hi_ and __bold__ now",
        mode="strip",
    )

    output = _render_to_text(renderable)
    assert "say hi and bold now" in output


# -- Fenced code blocks must survive strip mode literally (issue #73212) ----

def test_strip_mode_preserves_dunder_underscores_inside_fenced_code():
    """The reporter's exact repro: __name__ must not become name."""
    renderable = _render_final_assistant_content(
        "```python\nprint(type(executor).__name__)\n```",
        mode="strip",
    )
    output = _render_to_text(renderable)
    assert "__name__" in output
    # The language tag must not appear as a stray visible line.
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    assert lines[0] != "python"


def test_strip_mode_preserves_dunder_main_guard_inside_fenced_code():
    renderable = _render_final_assistant_content(
        '```python\nif __name__ == "__main__":\n    main()\n```',
        mode="strip",
    )
    output = _render_to_text(renderable)
    assert '__name__ == "__main__"' in output


def test_strip_mode_drops_language_tag_line(monkeypatch):
    """The opening fence's language tag must not be rendered as a plain
    visible line of output (it was previously left behind verbatim once
    the backtick fence markers were stripped)."""
    from cli import _strip_markdown_syntax
    rendered = _strip_markdown_syntax("```python\nx = 1\n```")
    lines = [l for l in rendered.splitlines() if l.strip()]
    assert "python" not in lines


def test_strip_mode_preserves_asterisk_emphasis_markers_inside_fenced_code():
    """Code that happens to contain single/double asterisks (e.g. **kwargs,
    a docstring with *args) must not have them stripped as Markdown
    emphasis -- only prose outside code fences gets that treatment."""
    from cli import _strip_markdown_syntax
    rendered = _strip_markdown_syntax(
        "```python\ndef f(*args, **kwargs):\n    pass\n```"
    )
    assert "def f(*args, **kwargs):" in rendered


def test_strip_mode_preserves_backtick_fence_markers_would_have_removed():
    """Sanity: without fence-awareness, the bare '(```+|~~~+)' removal
    regex would strip ALL backtick fences anywhere, including a fence
    that legitimately appears inside another fenced block's content
    (e.g. an assistant explaining Markdown syntax). The extracted block's
    raw text must round-trip untouched."""
    from cli import _strip_markdown_syntax
    source = "```text\nUse ``` to start a code fence.\n```"
    rendered = _strip_markdown_syntax(source)
    assert "```" in rendered


def test_strip_mode_prose_dunder_stripping_still_works_outside_code():
    """Regression: this fix must not disable emphasis stripping for
    prose outside code fences -- only protect literal code content."""
    renderable = _render_final_assistant_content(
        "say __bold__ now",
        mode="strip",
    )
    output = _render_to_text(renderable)
    assert "say bold now" in output


def test_strip_mode_multiple_fenced_blocks_each_preserved_independently():
    from cli import _strip_markdown_syntax
    source = (
        "First:\n```python\n__version__ = \"1.0\"\n```\n"
        "Second:\n```python\n__author__ = \"x\"\n```"
    )
    rendered = _strip_markdown_syntax(source)
    assert "__version__" in rendered
    assert "__author__" in rendered


# -- Follow-up fixes per review of #73217 ------------------------------------

def test_strip_mode_preserves_trailing_blank_lines_inside_fenced_block():
    """Regression: the splice-back loop used to code.rstrip("\\n") each
    captured block before re-inserting it, silently dropping trailing
    blank lines that were genuinely part of the fenced content (e.g. a
    file ending in a blank line, or deliberate spacing before a closing
    bracket). The block sits mid-response (followed by more prose) so
    this isolates the splice-back behavior from the unrelated outer
    plain.strip("\\n") cleanup applied to the whole response boundary."""
    from cli import _strip_markdown_syntax
    source = "```python\nx = 1\n\n\n```\nDone."
    rendered = _strip_markdown_syntax(source)
    assert "x = 1\n\n\n" in rendered, (
        f"trailing blank lines inside the fenced block were trimmed: {rendered!r}"
    )


def test_strip_mode_longer_closing_fence_still_recognized():
    """Regression: a closing fence with MORE backticks than the opener
    (valid per CommonMark -- the closer only needs to be at least as long
    as the opener, not exactly equal) must still be recognized as the
    block's end, protecting the content inside."""
    from cli import _strip_markdown_syntax
    source = "```python\nprint(__name__)\n````"
    rendered = _strip_markdown_syntax(source)
    assert "__name__" in rendered


def test_strip_mode_scans_past_invalid_closer_to_a_later_valid_one():
    """Regression (review of #75462): a single-pass regex that rejects an
    invalid candidate closer (wrong type, or shorter than the opener) by
    returning the whole match unchanged has already CONSUMED that match's
    span -- so scanning resumes after it and never reaches a later,
    valid closer for the SAME opener. Here, a stray ``` line (too short
    for the ```` opener, and not meant as this block's closer at all)
    appears before the genuine ```` closer further down. The manual scan
    must skip the invalid candidate and find the later valid one,
    correctly extracting and protecting the whole block -- not fall
    through to the unterminated-fence fallback just because the FIRST
    candidate it found was invalid."""
    from cli import _strip_markdown_syntax
    source = "````python\nprint(__name__)\n```\nmore code with __other__ dunder\n````"
    rendered = _strip_markdown_syntax(source)
    assert "__name__" in rendered, (
        f"must scan past the invalid ``` to find the later valid ```` "
        f"closer, extracting and protecting the whole block: {rendered!r}"
    )
    assert "__other__" in rendered, rendered
    assert "````" not in rendered, (
        "the fence markers themselves should not leak into the rendered "
        f"output once the block is correctly extracted: {rendered!r}"
    )


def test_strip_mode_shorter_closer_with_no_later_valid_one_is_unterminated():
    """Companion to the scan-past-invalid-candidate test above: when
    there is NO later valid closer anywhere (only a too-short ``` and
    nothing else), the block genuinely has no valid closer and falls
    through to the documented unterminated-fence fallback -- __name__ IS
    affected here (becomes name), because ordinary prose-emphasis
    stripping runs on it. This is the known, accepted limitation for the
    undecidable case, distinct from the scan-past-invalid-candidate case
    where a valid closer DOES exist further down."""
    from cli import _strip_markdown_syntax
    source = "````python\nprint(__name__)\n```"
    rendered = _strip_markdown_syntax(source)
    assert "__name__" not in rendered, (
        f"with no valid closer anywhere, the block must not be extracted: {rendered!r}"
    )


def test_strip_mode_unterminated_fence_documented_fallback_behavior():
    """Regression (review of #73217, cross-referencing #73315): an
    unterminated fence (opening marker with no matching close anywhere in
    the text -- e.g. a response cut off mid-code-block) cannot be
    protected as a literal block, since there's no way to know where it
    was meant to end. This documents the current, intentional fallback:
    _extract_fenced_blocks() simply finds no valid closer (the scan
    reaches the end of the text), so the
    content is never extracted/protected at all -- the bare fence-marker
    removal and ordinary prose-emphasis stripping run on it like any
    other text. A dunder like __name__ IS still affected in this specific
    corner case (it structurally matches the double-underscore emphasis
    pattern), unlike a properly-closed block. This is a known, accepted
    limitation -- not a crash, not silent data loss of the rest of the
    response, just the pre-#73212 behavior for the one case that can't be
    disambiguated without a closing fence. Must not raise."""
    from cli import _strip_markdown_syntax
    source = "```python\nprint(__name__)"  # no closing fence anywhere
    rendered = _strip_markdown_syntax(source)  # must not raise
    assert "```" not in rendered
    assert "name" in rendered  # __name__ -> name, the known limitation


class TestStreamingStripModeFenceState:
    """Regression tests for issue #73217/#75188's review: strip mode's
    per-line STREAMING formatter (HermesCLI._emit_stream_text) is a
    completely separate code path from _strip_markdown_syntax()'s
    whole-response handling -- it processes one line at a time as chunks
    arrive and has no visibility into the full response, so the
    fence-awareness fix didn't cover it at all. A fenced __name__
    streamed line-by-line remained corrupted even after #73212's fix
    landed.

    Drives the REAL _emit_stream_text()/_reset_stream_state() methods
    (not a hand-rolled duplicate of the fence-detection conditional), so
    production-only ordering -- e.g. table-row buffering happening before
    fence detection -- is actually covered.
    """

    def _make_cli(self):
        """Minimal HermesCLI instance with just enough state for
        _emit_stream_text() to run without a full construction."""
        import cli as cli_mod
        obj = object.__new__(cli_mod.HermesCLI)
        obj.final_response_markdown = "strip"
        obj._stream_buf = ""
        obj._stream_started = True
        obj._stream_box_opened = True
        obj._stream_table_buf = []
        obj._in_stream_table = False
        obj._in_stream_code_fence = False
        obj._stream_code_fence_char = ""
        obj._stream_code_fence_len = 0
        obj._reasoning_preview_buf = ""
        obj._reasoning_box_opened = False
        obj._reasoning_buf = ""
        obj._deferred_content = ""
        obj.show_reasoning = False
        obj.show_timestamps = False
        return obj

    def _feed_and_capture(self, monkeypatch, obj, chunks):
        """Feed `chunks` through the real _emit_stream_text(), capturing
        every printed line via a patched cli._cprint. Strips the padding/
        ANSI wrapper _emit_one() adds so assertions can check line content
        directly."""
        import cli as cli_mod

        printed = []
        monkeypatch.setattr(cli_mod, "_cprint", lambda text: printed.append(text))

        for chunk in chunks:
            obj._emit_stream_text(chunk)

        lines = []
        for text in printed:
            # _emit_one() wraps each line as f"{_STREAM_PAD}{...}{_RST}" or
            # f"{_STREAM_PAD}{_tc}{...}{_RST}" -- strip the pad prefix and
            # any trailing ANSI reset/color codes to get the raw line.
            stripped = text
            if stripped.startswith(cli_mod._STREAM_PAD):
                stripped = stripped[len(cli_mod._STREAM_PAD):]
            stripped = re.sub(r"\x1b\[[0-9;]*m", "", stripped)
            lines.append(stripped)
        return lines

    def test_dunder_survives_when_fence_and_code_arrive_in_separate_chunks(
        self, monkeypatch
    ):
        """The exact scenario the review flagged: streamed content is
        split at each newline and each line stripped independently, with
        no memory of a fence opened on a PREVIOUS chunk."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(
            monkeypatch, obj,
            ["```python\n", "print(type(x).__name__)\n", "```\n"],
        )
        assert any("__name__" in l for l in emitted), emitted
        assert not any(l.strip() == "__main__" for l in emitted)

    def test_fence_state_persists_across_multiple_stream_chunks(self, monkeypatch):
        """A single line of code split mid-token across two separate
        streamed deltas (the buffer only flushes complete lines, so this
        exercises accumulation) must still be protected once the line is
        complete, and fence state must still be correctly open."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(monkeypatch, obj, ["```python\n"])
        assert obj._in_stream_code_fence is True
        assert obj._stream_code_fence_char == "`"
        assert obj._stream_code_fence_len == 3

        emitted += self._feed_and_capture(
            monkeypatch, obj, ['if __name__ == "__main__":\n']
        )
        assert obj._in_stream_code_fence is True
        assert any('__name__ == "__main__"' in l for l in emitted), emitted

        self._feed_and_capture(monkeypatch, obj, ["```\n"])
        assert obj._in_stream_code_fence is False

    def test_prose_outside_stream_fence_still_stripped(self, monkeypatch):
        """Sanity: this fix must not disable stripping for ordinary
        streamed prose lines outside any fence."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(monkeypatch, obj, ["say __bold__ now\n"])
        assert emitted == ["say bold now"]

    def test_stream_fence_state_resets_between_responses(self, monkeypatch):
        """A fence left open (e.g. a bug elsewhere, or an unterminated
        block) must not leak into the NEXT response's streaming state.
        Drives the REAL _reset_stream_state() method (per review of
        #75188), not a hand-simulated copy of its reset assignments."""
        obj = self._make_cli()
        self._feed_and_capture(monkeypatch, obj, ["```python\n"])
        assert obj._in_stream_code_fence is True

        obj._reset_stream_state()

        assert obj._in_stream_code_fence is False
        assert obj._stream_code_fence_char == ""
        assert obj._stream_code_fence_len == 0

    def test_shorter_closer_does_not_close_longer_opener_while_streaming(
        self, monkeypatch
    ):
        """Regression (review of #75188): the streaming fence state only
        tracked the marker CHARACTER, not its length, so a 3-backtick
        closer incorrectly ended a 4-backtick-opened block. A genuine
        3-backtick line appearing mid-block (e.g. the assistant explaining
        Markdown fence syntax inside a real code block) must not be
        mistaken for the closer, and __name__ after it must stay
        protected."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(
            monkeypatch, obj,
            [
                "````python\n",
                "# use ``` to start a fence\n",
                "print(__name__)\n",
                "````\n",
            ],
        )
        assert obj._in_stream_code_fence is False  # correctly closed by ````
        assert any("__name__" in l for l in emitted), emitted
        assert not any(l.strip() == "__main__" for l in emitted)

    def test_fenced_pipe_line_not_swallowed_by_table_buffering(self, monkeypatch):
        """Regression (review of #75188): table-row buffering used to run
        BEFORE fence-state detection, so a fenced line shaped like a
        table row (e.g. a Markdown table example inside a code fence)
        got swallowed into table buffering instead of being recognized
        as fenced content -- and table-buffer flushing invokes markdown
        stripping, which would corrupt a dunder the same way as before."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(
            monkeypatch, obj,
            [
                "```text\n",
                "| __name__ | value |\n",
                "|---|---|\n",
                "```\n",
            ],
        )
        assert obj._in_stream_table is False, (
            "the pipe-shaped fenced line must not have been buffered as a table row"
        )
        assert any("__name__" in l for l in emitted), emitted
        assert not any(l.strip() == "__main__" for l in emitted)

    def test_info_suffixed_line_inside_fence_does_not_close_it(self, monkeypatch):
        """Regression (review of #75462): a line matching the fence
        MARKER pattern but carrying an info string after it (e.g.
        "```python" appearing while ALREADY inside an open fence -- the
        assistant's own reply showing an example fence-within-a-fence)
        is not a valid closer per CommonMark, which requires nothing but
        whitespace after a closing marker. The streaming detector used
        the same permissive open-line regex for both opening and closing
        checks, so this line incorrectly closed the block early,
        de-protecting everything meant to still be inside it."""
        obj = self._make_cli()
        emitted = self._feed_and_capture(
            monkeypatch, obj,
            [
                "```markdown\n",
                "```python is how you'd start a Python fence\n",
                "print(__name__)\n",
                "```\n",
            ],
        )
        assert obj._in_stream_code_fence is False  # correctly closed by the real ```
        assert any("__name__" in l for l in emitted), emitted
        assert not any(l.strip() == "__main__" for l in emitted)
