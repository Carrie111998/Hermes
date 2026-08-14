"""Tests for tools/tool_spill.py — config-gated spill of oversized tool outputs.

Coverage: cap applied, locator notice format + byte budget reservation,
best-effort storage failures, read-tool skip, unicode safety, path
traversal safety, deterministic session-scoped paths, and the disabled
(default) no-op.
"""

import re

import pytest

from tools.tool_spill import (
    READ_TOOLS,
    SpillConfig,
    _preview_head_tail,
    _spill_notice,
    _truncate_utf8,
    _utf8_len,
    load_config,
    maybe_spill_tool_result,
    spill_dir,
    spill_path,
)

BIG = "x" * 10_000  # 10_000 UTF-8 bytes


def _enabled(cap: int) -> SpillConfig:
    return SpillConfig(enabled=True, max_inline_bytes=cap)


def _result_path(replaced: str) -> str:
    """Extract the locator path from a spill replacement string."""
    match = re.search(r"Full result stored at: ([^\n)]+)\)$", replaced)
    assert match, f"no locator notice in: {replaced!r}"
    return match.group(1)


# ── config parsing ────────────────────────────────────────────────────

class TestSpillConfig:
    def test_default_disabled(self):
        cfg = SpillConfig()
        assert cfg.enabled is False
        assert cfg.max_inline_bytes == 100_000

    def test_from_raw_none_and_false_are_disabled(self):
        assert SpillConfig.from_raw(None).enabled is False
        assert SpillConfig.from_raw(False).enabled is False

    def test_from_raw_true_enables_with_default_cap(self):
        cfg = SpillConfig.from_raw(True)
        assert cfg.enabled is True
        assert cfg.max_inline_bytes == 100_000

    def test_from_raw_dict(self):
        cfg = SpillConfig.from_raw({"enabled": True, "max_inline_bytes": 500})
        assert cfg.enabled is True
        assert cfg.max_inline_bytes == 500

    def test_from_raw_garbage_falls_back(self):
        cfg = SpillConfig.from_raw({"enabled": "yes", "max_inline_bytes": "lots"})
        assert cfg.enabled is True  # truthy string
        assert cfg.max_inline_bytes == 100_000
        cfg2 = SpillConfig.from_raw([1, 2, 3])
        assert cfg2.enabled is False


# ── byte helpers ──────────────────────────────────────────────────────

class TestByteHelpers:
    def test_truncate_ascii(self):
        assert _truncate_utf8("abcdef", 3) == "abc"
        assert _truncate_utf8("abcdef", 0) == ""
        assert _truncate_utf8("abc", 10) == "abc"

    def test_truncate_never_splits_multibyte(self):
        text = "αβγδε"  # 2 bytes per char
        out = _truncate_utf8(text, 3)
        assert "\ufffd" not in out
        assert _utf8_len(out) <= 3
        assert out == "α"  # byte 2 boundary keeps exactly one char

    def test_truncate_budget_lands_inside_char(self):
        text = "αβγδε"
        out = _truncate_utf8(text, 4)
        assert out == "αβ"
        assert _utf8_len(out) == 4

    def test_truncate_emoji(self):
        text = "🎉🎉🎉"  # 4 bytes each
        out = _truncate_utf8(text, 6)
        assert out == "🎉"
        assert _utf8_len(out) == 4

    def test_preview_head_tail_ascii(self):
        text = "abcdef"
        preview, omitted = _preview_head_tail(text, 4)
        assert preview == "abef"
        assert omitted == 2

    def test_preview_head_tail_fits_whole(self):
        text = "abcdef"
        preview, omitted = _preview_head_tail(text, 6)
        assert preview == text
        assert omitted == 0

    def test_preview_head_tail_unicode_boundaries(self):
        text = "αβγδεζηθ"  # 8 chars x 2 bytes = 16 bytes
        preview, omitted = _preview_head_tail(text, 8)
        assert "\ufffd" not in preview
        assert _utf8_len(preview) <= 8
        assert omitted == 16 - _utf8_len(preview)
        # head and tail are disjoint: head + tail covers the two ends
        assert preview.startswith("αβ")
        assert preview.endswith("ηθ")

    def test_preview_zero_budget(self):
        preview, omitted = _preview_head_tail("abc", 0)
        assert preview == ""
        assert omitted == 3


# ── notice format ─────────────────────────────────────────────────────

class TestNotice:
    def test_exact_format(self):
        notice = _spill_notice(1_234, "/tmp/spill/s/file.txt")
        assert notice == "(1234 bytes omitted. Full result stored at: /tmp/spill/s/file.txt)"

    def test_spill_notice_present_with_path(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced != BIG
        path = _result_path(replaced)
        assert path.endswith("terminal_call_1.txt")
        assert "bytes omitted. Full result stored at: " in replaced
        # The file exists and holds the FULL original text.
        assert open(path, encoding="utf-8").read() == BIG


# ── maybe_spill_tool_result ───────────────────────────────────────────

class TestMaybeSpillToolResult:
    def test_disabled_by_default_keeps_inline(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=SpillConfig(),  # default: enabled=False
            hermes_home=tmp_path,
        )
        assert replaced == BIG

    def test_under_cap_unchanged(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content="small result",
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(100_000),
            hermes_home=tmp_path,
        )
        assert replaced == "small result"
        assert not (tmp_path / "sessions" / "spill").exists()

    def test_oversized_spilled_with_preview(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced != BIG
        # Head/tail preview is present (both ends of the original); the
        # preview is the part before the "\n\n" notice separator.
        preview_part = replaced.split("\n\n", 1)[0]
        assert preview_part.startswith("x" * 900)
        assert preview_part.endswith("x" * 900)
        assert _utf8_len(replaced) <= 2_000
        # Notice states bytes omitted and the path.
        assert re.search(r"\(\d+ bytes omitted\. Full result stored at: ", replaced)

    def test_replacement_never_exceeds_cap(self, tmp_path):
        """The reservation (notice cost + 2-byte join) guarantees the
        replacement fits inside max_inline_bytes for any oversized input."""
        for cap in (500, 2_000, 10_000):
            content = "y" * (cap * 3)
            replaced = maybe_spill_tool_result(
                content=content,
                tool_name="terminal",
                tool_use_id=f"call_{cap}",
                session_id="sess_1",
                config=_enabled(cap),
                hermes_home=tmp_path,
            )
            assert _utf8_len(replaced) <= cap, f"cap {cap}: replacement {_utf8_len(replaced)} bytes"

    def test_best_effort_write_failure_keeps_inline(self, tmp_path, monkeypatch):
        from tools import tool_spill as mod

        def _fail(content, path):
            return False

        monkeypatch.setattr(mod, "_write_spill_file", _fail)
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced == BIG  # original kept, never an error

    def test_best_effort_real_oserror_keeps_inline(self, tmp_path):
        # Make the sessions dir un-creatable: a FILE in the way.
        (tmp_path / "sessions").write_text("blocking file", encoding="utf-8")
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced == BIG

    def test_read_file_is_skipped(self, tmp_path):
        assert "read_file" in READ_TOOLS
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="read_file",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced == BIG
        assert not (tmp_path / "sessions" / "spill").exists()

    def test_no_session_id_keeps_inline(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        assert replaced == BIG
        assert not (tmp_path / "sessions" / "spill").exists()

    def test_tiny_cap_notice_exceeds_keeps_inline(self, tmp_path):
        """When even the notice alone exceeds the cap, there is no within-cap
        replacement: the original stays inline (the written file is a
        documented harmless orphan)."""
        cap = 50
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(cap),
            hermes_home=tmp_path,
        )
        assert replaced == BIG
        # The orphan file was still written (best-effort write happens first).
        files = list((tmp_path / "sessions" / "spill").rglob("*.txt"))
        assert len(files) == 1

    def test_unicode_content_roundtrip(self, tmp_path):
        content = "αβγδεζηθκλμνξοπρστυφχψω\n" * 400  # multibyte, oversized
        cap = 1_500
        replaced = maybe_spill_tool_result(
            content=content,
            tool_name="terminal",
            tool_use_id="call_uni",
            session_id="sess_1",
            config=_enabled(cap),
            hermes_home=tmp_path,
        )
        assert "\ufffd" not in replaced
        assert _utf8_len(replaced) <= cap
        path = _result_path(replaced)
        assert open(path, encoding="utf-8").read() == content

    def test_omitted_count_is_accurate(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="sess_1",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        m = re.search(r"\((\d+) bytes omitted\.", replaced)
        assert m
        omitted = int(m.group(1))
        kept = _utf8_len(replaced) - _utf8_len(_spill_notice(omitted, _result_path(replaced))) - 2
        assert omitted == 10_000 - kept

    def test_deterministic_path_and_idempotent_write(self, tmp_path):
        p1 = spill_path("sess_1", "terminal", "call_1", hermes_home=tmp_path)
        p2 = spill_path("sess_1", "terminal", "call_1", hermes_home=tmp_path)
        assert p1 == p2
        # Re-processing the same call overwrites the same file: still one file.
        for _ in range(2):
            maybe_spill_tool_result(
                content=BIG,
                tool_name="terminal",
                tool_use_id="call_1",
                session_id="sess_1",
                config=_enabled(2_000),
                hermes_home=tmp_path,
            )
        files = list((tmp_path / "sessions" / "spill").rglob("*.txt"))
        assert len(files) == 1
        assert files[0] == p1


# ── path safety ───────────────────────────────────────────────────────

class TestPathSafety:
    def test_session_scoped_dirs(self, tmp_path):
        d1 = spill_dir("sess_1", hermes_home=tmp_path)
        d2 = spill_dir("sess_2", hermes_home=tmp_path)
        assert d1.parent == d2.parent  # both under sessions/spill/
        assert d1 != d2

    def test_tool_use_id_cannot_traverse(self, tmp_path):
        path = spill_path("sess_1", "terminal", "../outside/$(whoami);x", hermes_home=tmp_path)
        rel = path.relative_to(tmp_path / "sessions" / "spill")
        assert ".." not in rel.parts
        assert "$" not in str(path)
        assert ";" not in str(path)
        assert path.name.startswith("terminal_")

    def test_session_id_cannot_traverse(self, tmp_path):
        path = spill_dir("../../etc/passwd", hermes_home=tmp_path)
        rel = path.relative_to(tmp_path / "sessions" / "spill")
        assert ".." not in rel.parts
        assert len(rel.parts) == 1

    def test_weird_session_id_sanitized(self, tmp_path):
        replaced = maybe_spill_tool_result(
            content=BIG,
            tool_name="terminal",
            tool_use_id="call_1",
            session_id="chat-1/../x",
            config=_enabled(2_000),
            hermes_home=tmp_path,
        )
        path = _result_path(replaced)
        assert "/../" not in path
        assert (tmp_path / "sessions" / "spill") in __import__("pathlib").Path(path).parents


# ── load_config ───────────────────────────────────────────────────────

class TestLoadConfig:
    def test_load_config_returns_disabled_default(self):
        cfg = load_config()
        assert isinstance(cfg, SpillConfig)
        # Defaults in config_defaults.py keep spill off.
        assert cfg.enabled is False
