"""Tests for recent-shared-links tracking (``agent.shared_media_tracker``).

A URL pasted in message text has no structured extraction anywhere else in
the pipeline, so it survives only as prose in one historical user turn and
the model loses track of it a few turns later. These cover the extraction
(dedup, limit, recency), the fenced block format, and the durability
property that motivates reading persisted rows: a link shared several turns
back must still reach the block.
"""

from __future__ import annotations

import pytest

from agent.shared_media_tracker import (
    build_recent_links_context_block,
    extract_recent_shared_links,
    link_label,
)
from hermes_state import SessionDB


def _rows(*texts: str) -> list:
    """Persisted user rows, oldest first (the DB read's order)."""
    return [
        {"id": i, "role": "user", "content": t, "timestamp": 1000.0 + i}
        for i, t in enumerate(texts)
    ]


# ---------------------------------------------------------------------------
# extract_recent_shared_links
# ---------------------------------------------------------------------------

class TestExtractRecentSharedLinks:
    def test_no_urls_is_empty(self):
        assert extract_recent_shared_links(_rows("hello", "how are you")) == []

    def test_empty_input_is_empty(self):
        assert extract_recent_shared_links([]) == []
        assert extract_recent_shared_links(None) == []

    def test_single_url(self):
        links = extract_recent_shared_links(_rows("look at https://example.com/a"))
        assert links == [
            {
                "url": "https://example.com/a",
                "label": "example.com/a",
                "turns_ago": 1,
            }
        ]

    def test_multiple_urls_newest_first(self):
        links = extract_recent_shared_links(
            _rows(
                "first https://a.com/1",
                "no link here",
                "second https://b.com/2",
            )
        )
        assert [link["url"] for link in links] == [
            "https://b.com/2",
            "https://a.com/1",
        ]
        # Newest supplied row is 1 turn ago; the first row is 3 rows back.
        assert [link["turns_ago"] for link in links] == [1, 3]

    def test_two_urls_in_one_message(self):
        links = extract_recent_shared_links(
            _rows("compare https://a.com/1 and https://b.com/2")
        )
        assert [link["url"] for link in links] == [
            "https://a.com/1",
            "https://b.com/2",
        ]
        assert all(link["turns_ago"] == 1 for link in links)

    def test_dedup_keeps_most_recent_mention(self):
        links = extract_recent_shared_links(
            _rows(
                "https://example.com/x",
                "unrelated",
                "again https://example.com/x",
            )
        )
        assert len(links) == 1
        assert links[0]["turns_ago"] == 1  # the recent mention, not the old one

    def test_limit_keeps_most_recent_n(self):
        links = extract_recent_shared_links(
            _rows(*[f"link https://site{i}.com/p" for i in range(12)]), limit=3
        )
        assert [link["url"] for link in links] == [
            "https://site11.com/p",
            "https://site10.com/p",
            "https://site9.com/p",
        ]

    def test_default_limit_is_eight(self):
        links = extract_recent_shared_links(
            _rows(*[f"https://site{i}.com/p" for i in range(20)])
        )
        assert len(links) == 8

    def test_non_positive_limit_is_empty(self):
        assert extract_recent_shared_links(_rows("https://a.com/1"), limit=0) == []

    def test_non_user_rows_are_skipped(self):
        rows = [
            {"role": "assistant", "content": "see https://assistant.com/x"},
            {"role": "user", "content": "mine https://user.com/y"},
        ]
        assert [link["url"] for link in extract_recent_shared_links(rows)] == [
            "https://user.com/y"
        ]

    def test_text_field_fallback(self):
        rows = [{"role": "user", "text": "https://example.com/from-text"}]
        assert extract_recent_shared_links(rows)[0]["url"] == (
            "https://example.com/from-text"
        )

    def test_multimodal_content_parts_are_scanned(self):
        rows = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "check https://example.com/img-note"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
        assert extract_recent_shared_links(rows)[0]["url"] == (
            "https://example.com/img-note"
        )

    def test_http_and_https_both_matched(self):
        links = extract_recent_shared_links(
            _rows("http://plain.example.com/a https://secure.example.com/b")
        )
        assert {link["url"] for link in links} == {
            "http://plain.example.com/a",
            "https://secure.example.com/b",
        }

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("see https://example.com/a.", "https://example.com/a"),
            ("see https://example.com/a, then", "https://example.com/a"),
            ("(https://example.com/a)", "https://example.com/a"),
            ("<https://example.com/a>", "https://example.com/a"),
            ("[link](https://example.com/a)", "https://example.com/a"),
        ],
    )
    def test_surrounding_punctuation_is_not_part_of_the_url(self, text, expected):
        assert extract_recent_shared_links(_rows(text))[0]["url"] == expected

    def test_non_dict_rows_are_ignored(self):
        rows = ["junk", None, {"role": "user", "content": "https://a.com/1"}]
        assert len(extract_recent_shared_links(rows)) == 1


# ---------------------------------------------------------------------------
# link_label — host + truncated path, not the full URL (token budget)
# ---------------------------------------------------------------------------

class TestLinkLabel:
    def test_drops_scheme_and_www(self):
        assert link_label("https://www.example.com/a") == "example.com/a"
        assert link_label("http://example.com/a") == "example.com/a"

    def test_keeps_query_string(self):
        assert link_label("https://youtube.com/watch?v=abc123") == (
            "youtube.com/watch?v=abc123"
        )

    def test_truncates_long_urls(self):
        label = link_label("https://example.com/" + "x" * 500)
        assert len(label) == 48
        assert label.endswith("...")
        assert label.startswith("example.com/xxx")

    def test_trailing_slash_dropped(self):
        assert link_label("https://example.com/") == "example.com"


# ---------------------------------------------------------------------------
# build_recent_links_context_block
# ---------------------------------------------------------------------------

class TestBuildRecentLinksContextBlock:
    def test_empty_when_no_links(self):
        assert build_recent_links_context_block(_rows("hello")) == ""
        assert build_recent_links_context_block([]) == ""

    def test_fenced_block_format(self):
        block = build_recent_links_context_block(
            _rows(
                "pr is https://github.com/org/repo/pull/42",
                "t1",
                "t2",
                "t3",
                "watch https://youtube.com/watch?v=abc123",
                "t5",
                "t6",
            )
        )
        assert block == (
            "<recent-shared-links>\n"
            "- youtube.com/watch?v=abc123 (3 turns ago)\n"
            "- github.com/org/repo/pull/42 (7 turns ago)\n"
            "</recent-shared-links>"
        )

    def test_singular_turn_wording(self):
        block = build_recent_links_context_block(_rows("https://a.com/1"))
        assert "(1 turn ago)" in block
        assert "1 turns ago" not in block

    def test_limit_is_forwarded(self):
        block = build_recent_links_context_block(
            _rows(*[f"https://site{i}.com/p" for i in range(5)]), limit=2
        )
        assert sum(line.startswith("- ") for line in block.splitlines()) == 2

    def test_url_survives_several_intervening_turns(self):
        """The original failure case, at unit level: a URL shared early is
        still labelled in the block many user turns later."""
        rows = _rows(
            "here is the video https://youtube.com/watch?v=abc123",
            *[f"unrelated question {i}" for i in range(9)],
        )
        block = build_recent_links_context_block(rows)
        assert "youtube.com/watch?v=abc123" in block
        assert "(10 turns ago)" in block


# ---------------------------------------------------------------------------
# The persisted-row read the prologue feeds the tracker
# ---------------------------------------------------------------------------

class TestGetRecentUserMessages:
    def _open(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("s1", source="cli")
        return db

    def test_returns_only_user_rows_oldest_first(self, tmp_path):
        db = self._open(tmp_path)
        try:
            db.append_message("s1", "user", content="q1")
            db.append_message("s1", "assistant", content="a1")
            db.append_message("s1", "tool", content="t1")
            db.append_message("s1", "user", content="q2")
            rows = db.get_recent_user_messages("s1")
            assert [r["content"] for r in rows] == ["q1", "q2"]
            assert all(r["role"] == "user" for r in rows)
        finally:
            db.close()

    def test_limit_takes_the_newest(self, tmp_path):
        db = self._open(tmp_path)
        try:
            for i in range(5):
                db.append_message("s1", "user", content=f"q{i}")
            rows = db.get_recent_user_messages("s1", limit=2)
            assert [r["content"] for r in rows] == ["q3", "q4"]
        finally:
            db.close()

    def test_non_positive_limit_is_empty(self, tmp_path):
        db = self._open(tmp_path)
        try:
            db.append_message("s1", "user", content="q1")
            assert db.get_recent_user_messages("s1", limit=0) == []
        finally:
            db.close()

    def test_feeds_the_tracker_end_to_end(self, tmp_path):
        """Persist a URL turn, several turns pass, the block still labels it."""
        db = self._open(tmp_path)
        try:
            db.append_message("s1", "user", content="see https://github.com/org/repo/pull/42")
            for i in range(4):
                db.append_message("s1", "assistant", content=f"a{i}")
                db.append_message("s1", "user", content=f"follow-up {i}")
            block = build_recent_links_context_block(
                db.get_recent_user_messages("s1", limit=50)
            )
            assert block == (
                "<recent-shared-links>\n"
                "- github.com/org/repo/pull/42 (5 turns ago)\n"
                "</recent-shared-links>"
            )
        finally:
            db.close()
