"""Tests for the shared session-listing helpers (hermes_cli/session_listing.py)."""

import pytest

from hermes_cli.session_listing import (
    last_active_of,
    parse_session_listing_args,
    query_session_listing,
)


class TestParseSessionListingArgs:
    def test_plain_listing(self):
        assert parse_session_listing_args("") == (False, False, "", None)

    def test_flags(self):
        assert parse_session_listing_args("all full") == (True, True, "", None)

    def test_target_passthrough(self):
        assert parse_session_listing_args("My Cool Session") == (
            False, False, "My Cool Session", None,
        )

    def test_search_query(self):
        assert parse_session_listing_args("search an94") == (False, False, "", "an94")

    def test_find_alias_multiword(self):
        assert parse_session_listing_args("find winton email") == (
            False, False, "", "winton email",
        )

    def test_all_search(self):
        assert parse_session_listing_args("all search cod") == (True, False, "", "cod")

    def test_search_without_query_is_empty_string(self):
        assert parse_session_listing_args("search") == (False, False, "", "")

    def test_search_word_inside_target_is_not_a_flag(self):
        # Flags/keywords only apply before the first positional word.
        assert parse_session_listing_args("deep search notes") == (
            False, False, "deep search notes", None,
        )


class TestQuerySessionListingSearch:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("sess_an94", "telegram", user_id="1", chat_id="2")
        db.set_session_title("sess_an94", "AN-94 Prestige Barrel Build #2")
        db.create_session("sess_winton", "whatsapp", user_id="1", chat_id="2")
        db.set_session_title("sess_winton", "Winton Email Sheet Update #3")
        db.create_session("sess_untitled", "telegram", user_id="1", chat_id="2")
        yield db
        db.close()

    def _ids(self, db, **kw):
        return [r["id"] for r in query_session_listing(db, **kw)]

    def test_title_substring_match(self, db):
        assert self._ids(db, source="telegram", search_query="prestige") == ["sess_an94"]

    def test_punctuation_normalized_match(self, db):
        # "an94" should match the title "AN-94 ..." via compact matching.
        assert self._ids(db, source="telegram", search_query="an94") == ["sess_an94"]

    def test_id_substring_match_includes_unnamed(self, db):
        assert self._ids(db, source="telegram", search_query="untitled") == ["sess_untitled"]

    def test_source_scoping(self, db):
        assert self._ids(db, source="telegram", search_query="winton") == []
        assert self._ids(db, source="whatsapp", search_query="winton") == ["sess_winton"]

    def test_no_match(self, db):
        assert self._ids(db, source="telegram", search_query="zzz-nope") == []

    def test_like_wildcards_are_literal(self, db):
        assert self._ids(db, source="telegram", search_query="%") == []

    def test_search_matches_compression_root_title(self, tmp_path):
        """Searching an old (compressed-away) title surfaces the live tip."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "chain.db")
        db.create_session("root_1", "telegram", user_id="1", chat_id="2")
        db.set_session_title("root_1", "Old Chat")
        db.end_session("root_1", end_reason="compression")
        db.create_session(
            "tip_1", "telegram", user_id="1", chat_id="2", parent_session_id="root_1"
        )
        db.set_session_title("tip_1", "AN-94 Build")
        try:
            for query in ("old chat", "root_1", "an94"):
                rows = query_session_listing(db, source="telegram", search_query=query)
                assert [r["id"] for r in rows] == ["tip_1"], query
        finally:
            db.close()

    def test_plain_listing_still_hides_unnamed(self, db):
        assert self._ids(db, source="telegram") == ["sess_an94"]


class TestLastActiveOf:
    """`last_active_of` must share the listing's Last-column definition:
    the latest message timestamp, never a later `ended_at`.

    Regression coverage for the search/list drift where search rendered
    `COALESCE(ended_at, started_at)` — a session that idled for hours
    after its final message showed a Last in the future relative to its
    own listing row, and the search sort inherited the same wrong key.
    """

    def test_uses_latest_message_timestamp_not_ended_at(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "la.db")
        db.create_session("sess_old", "cli")
        db.append_message("sess_old", "user", "first", timestamp=1_000_000.0)
        db.append_message("sess_old", "assistant", "second", timestamp=1_000_500.0)
        # Closed 2h after its final message: ended_at > last message ts.
        db.end_session("sess_old", end_reason="cli_close")
        try:
            last = last_active_of(db, ["sess_old"])["sess_old"]
            assert last == 1_000_500.0
            # Agrees with the canonical listing column.
            rows = db.list_sessions_rich(source="cli", include_children=False)
            assert {r["id"]: r["last_active"] for r in rows}["sess_old"] == 1_000_500.0
        finally:
            db.close()

    def test_falls_back_to_started_at_when_no_messages(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "empty.db")
        db.create_session("sess_empty", "cli")
        try:
            meta = db.get_session("sess_empty")
            assert last_active_of(db, ["sess_empty"])["sess_empty"] == meta["started_at"]
        finally:
            db.close()

    def test_search_order_matches_listing_order(self, tmp_path):
        """Sessions ordered by `last_active_of` sort identically to the
        canonical listing's last-active ordering — the contract search
        promises when it says it shows the same numbers/order as the list.
        """
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "order.db")
        # A: old messages, closed late (ended_at in the future relative to
        # its last message). B: newer messages, still open.
        db.create_session("sess_a", "cli")
        db.append_message("sess_a", "user", "a1", timestamp=1_000_000.0)
        db.end_session("sess_a", end_reason="cli_close")
        db.create_session("sess_b", "cli")
        db.append_message("sess_b", "user", "b1", timestamp=1_100_000.0)
        try:
            sid_latest = last_active_of(db, ["sess_a", "sess_b"])
            search_order = sorted(
                ["sess_a", "sess_b"],
                key=lambda s: sid_latest.get(s, 0),
                reverse=True,
            )
            listing_order = [
                r["id"]
                for r in db.list_sessions_rich(
                    source="cli", include_children=False, order_by_last_active=True
                )
            ]
            # Both surfaces agree on which session is "most recent" even
            # though sess_a's ended_at is later than sess_b's last message.
            assert search_order == listing_order == ["sess_b", "sess_a"]
        finally:
            db.close()
