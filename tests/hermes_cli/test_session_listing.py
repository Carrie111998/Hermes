"""Tests for the shared session-listing helpers (hermes_cli/session_listing.py)."""

import pytest

from hermes_cli.session_listing import (
    parse_session_listing_args,
    query_session_listing,
)


class TestParseSessionListingArgs:
    def test_plain_listing(self):
        assert parse_session_listing_args("") == (False, False, "", None)




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



    def test_source_scoping(self, db):
        assert self._ids(db, source="telegram", search_query="winton") == []
        assert self._ids(db, source="whatsapp", search_query="winton") == ["sess_winton"]


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


class TestQuerySessionListingLaneScope:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        lane_key = "agent:main:telegram:dm:lane"
        db.create_session(
            "lane_current", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_current", "Current lane")
        db.create_session(
            "lane_named", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_named", "Needle lane")
        db.create_session(
            "lane_unnamed", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        for i in range(60):
            db.create_session(
                f"foreign_{i}", "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(f"foreign_{i}", f"Needle foreign {i}")
        yield db, lane_key
        db.close()

    def test_exact_lane_precedes_limit_and_current_session_exclusion(self, db):
        session_db, lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            current_session_id="lane_current",
            limit=1,
        )

        assert [row["id"] for row in rows] == ["lane_named"]

    def test_exact_lane_preserves_full_and_search_modes(self, db):
        session_db, lane_key = db

        full_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            include_unnamed=True,
            limit=10,
        )
        search_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            search_query="needle",
            limit=10,
        )

        assert {row["id"] for row in full_rows} == {
            "lane_current", "lane_named", "lane_unnamed",
        }
        assert [row["id"] for row in search_rows] == ["lane_named"]

    def test_omitted_session_key_keeps_source_scope(self, db):
        session_db, _lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            search_query="needle foreign 59",
            limit=10,
        )

        assert [row["id"] for row in rows] == ["foreign_59"]


class TestQuerySessionListingDisplayName:
    """`/sessions search` finds gateway sessions by persisted display_name.

    display_name is the gateway's peer/chat presentation identity (issue
    #9006): a channel like `#finance` is how a user refers to a conversation,
    so the shared listing seam must match it alongside title/id. This is the
    focused prior art from upstream #71912.
    """

    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "budget_review",
            "telegram",
            user_id="1",
            chat_id="100",
            display_name="Acme Guild / #finance",
        )
        db.set_session_title("budget_review", "Quarterly Budget Review")
        db.create_session(
            "ops_channel",
            "discord",
            user_id="1",
            chat_id="200",
            display_name="#an-94-ops",
        )
        db.set_session_title("ops_channel", "Build Ops")
        yield db
        db.close()

    def test_display_name_match_via_listing_seam(self, db):
        # Title ("Quarterly Budget Review") and id ("budget_review") do NOT
        # contain "finance"; only the gateway display_name does.
        rows = query_session_listing(db, source="telegram", search_query="finance")
        assert [row["id"] for row in rows] == ["budget_review"]

    def test_display_name_compact_variant(self, db):
        # Compact query: "an94" matches display_name "#an-94-ops" even though
        # the title ("Build Ops") and id ("ops_channel") don't contain it.
        rows = query_session_listing(db, source="discord", search_query="an94")
        assert [row["id"] for row in rows] == ["ops_channel"]

    def test_literal_wildcards_do_not_widen(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "literal.db")
        db.create_session(
            "under", "telegram", user_id="1", chat_id="1", display_name="under_score"
        )
        try:
            # "%" is escaped: a bare "%" query must not act as a
            # match-everything wildcard (it should not match "under_score").
            rows = query_session_listing(db, source="telegram", search_query="%")
            assert rows == []
            # "_" is escaped: the display_name "under_score" is matched
            # literally, not widened into a one-character wildcard.
            rows = query_session_listing(
                db, source="telegram", search_query="under_score"
            )
            assert [row["id"] for row in rows] == ["under"]
        finally:
            db.close()
