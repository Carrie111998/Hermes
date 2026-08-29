"""Behavior contract for relative MEDIA path normalization.

The central fence: a rewrite happens only when the joined absolute path
actually exists — so "making things worse" is impossible by construction.
These tests pin the three design fences (line-form only / fenced blocks
untouched / missing files untouched).
"""

import os

import pytest

from agent.media_path_normalizer import normalize_relative_media_paths


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A session workdir holding real media files, registered as the live cwd. CJK filenames double as a unicode-path test."""
    (tmp_path / "成片.mp4").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "clip.mp4").write_bytes(b"x")

    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda key: str(tmp_path))
    return tmp_path


class TestRewrites:
    def test_relative_file_becomes_absolute(self, workdir):
        out = normalize_relative_media_paths("done\nMEDIA: 成片.mp4\n", session_key="t1")
        assert f"MEDIA: {workdir / '成片.mp4'}" in out
        assert "MEDIA: 成片.mp4" not in out

    def test_subdir_and_dot_slash(self, workdir):
        out = normalize_relative_media_paths("MEDIA: ./sub/clip.mp4", session_key="t1")
        assert f"MEDIA: {workdir / 'sub' / 'clip.mp4'}" in out

    def test_quoted_path_with_spaces(self, workdir):
        (workdir / "my clip.mp4").write_bytes(b"x")
        out = normalize_relative_media_paths('MEDIA: "my clip.mp4"', session_key="t1")
        assert f"MEDIA: {workdir / 'my clip.mp4'}" in out

    def test_leading_indent_preserved(self, workdir):
        out = normalize_relative_media_paths("  MEDIA: 成片.mp4", session_key="t1")
        assert out.startswith("  MEDIA: ")


class TestFuses:
    def test_missing_file_left_untouched(self, workdir):
        src = "MEDIA: 不存在.mp4"
        assert normalize_relative_media_paths(src, session_key="t1") == src

    def test_absolute_path_never_touched(self, workdir):
        src = f"MEDIA: {workdir / '成片.mp4'}"
        assert normalize_relative_media_paths(src, session_key="t1") == src

    def test_home_url_and_drive_paths_never_touched(self, workdir):
        for src in (
            "MEDIA: ~/videos/a.mp4",
            "MEDIA: https://example.com/a.mp4",
            "MEDIA: C:\\Users\\a\\b.mp4",
        ):
            assert normalize_relative_media_paths(src, session_key="t1") == src

    def test_code_fence_left_untouched(self, workdir):
        src = "example:\n```\nMEDIA: 成片.mp4\n```\nthat was a syntax example"
        assert normalize_relative_media_paths(src, session_key="t1") == src

    def test_inline_media_not_line_form_untouched(self, workdir):
        src = "inline MEDIA: 成片.mp4 inside prose stays untouched"
        assert normalize_relative_media_paths(src, session_key="t1") == src

    def test_no_cwd_available_untouched(self, monkeypatch):
        from tools import terminal_tool

        monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda key: None)
        monkeypatch.setenv("TERMINAL_CWD", "")
        src = "MEDIA: 成片.mp4"
        out = normalize_relative_media_paths(src, session_key="t1")
        # resolve_agent_cwd falls back to the process cwd — no such file there, the existence fence holds
        assert out == src

    def test_non_string_and_no_media_fast_path(self):
        assert normalize_relative_media_paths("no media line here") == "no media line here"
        assert normalize_relative_media_paths(None) is None  # type: ignore[arg-type]


class TestFenceStateMachine:
    def test_media_after_closed_fence_rewritten(self, workdir):
        src = "```\ncode\n```\nMEDIA: 成片.mp4"
        out = normalize_relative_media_paths(src, session_key="t1")
        assert f"MEDIA: {workdir / '成片.mp4'}" in out

    def test_unclosed_fence_tail_untouched(self, workdir):
        src = "```\nMEDIA: 成片.mp4"
        assert normalize_relative_media_paths(src, session_key="t1") == src
