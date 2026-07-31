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


class TestChainTokenTotals:
    """Tok(ΣIn/ΣOut) shows the compression-chain total, not the root's or
    the tip's single-generation counts.

    Regression coverage for the projection surfacing the root's historical
    token counts on a projected tip row — for a long conversation the root
    figure can differ from the live tip by an order of magnitude.
    """

    def test_listing_shows_chain_total_not_root_or_tip(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "tok.db")
        db.create_session("tok_root", "cli")
        db.end_session("tok_root", end_reason="compression")
        db.create_session("tok_mid", "cli", parent_session_id="tok_root")
        db.end_session("tok_mid", end_reason="compression")
        db.create_session("tok_tip", "cli", parent_session_id="tok_mid")
        conn = db._conn
        conn.execute(
            "UPDATE sessions SET input_tokens=100, output_tokens=10 WHERE id='tok_root'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=200, output_tokens=20 WHERE id='tok_mid'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=300, output_tokens=30 WHERE id='tok_tip'"
        )
        conn.commit()
        try:
            rows = db.list_sessions_rich(source="cli", include_children=False)
            assert [r["id"] for r in rows] == ["tok_tip"]
            row = rows[0]
            assert row["input_tokens"] == 600, row["input_tokens"]
            assert row["output_tokens"] == 60, row["output_tokens"]
            # chain_token_totals resolves any generation of the chain.
            assert db.chain_token_totals(["tok_root", "tok_mid", "tok_tip"]) == {
                "tok_root": (600, 60),
                "tok_mid": (600, 60),
                "tok_tip": (600, 60),
            }
        finally:
            db.close()

    def test_branch_delegate_tool_children_not_counted(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "tok_excl.db")
        db.create_session("tok_root", "cli")
        db.end_session("tok_root", end_reason="compression")
        db.create_session("tok_tip", "cli", parent_session_id="tok_root")
        db.create_session("tok_branch", "cli", parent_session_id="tok_root")
        db.create_session("tok_delegate", "cli", parent_session_id="tok_root")
        db.create_session("tok_tool", "tool", parent_session_id="tok_root")
        conn = db._conn
        conn.execute(
            "UPDATE sessions SET input_tokens=100, output_tokens=10 WHERE id='tok_root'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=300, output_tokens=30 WHERE id='tok_tip'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=999, output_tokens=99 WHERE id='tok_branch'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=888, output_tokens=88 WHERE id='tok_delegate'"
        )
        conn.execute(
            "UPDATE sessions SET input_tokens=777, output_tokens=77 WHERE id='tok_tool'"
        )
        conn.execute(
            "UPDATE sessions SET model_config='{\"_branched_from\": \"tok_root\"}' "
            "WHERE id='tok_branch'"
        )
        conn.execute(
            "UPDATE sessions SET model_config='{\"_delegate_from\": \"tok_root\"}' "
            "WHERE id='tok_delegate'"
        )
        conn.commit()
        try:
            rows = db.list_sessions_rich(source="cli", include_children=False)
            # Branch children stay visible as their own rows; the projected
            # chain entry is the tip, summed over root + tip only.
            ids = [r["id"] for r in rows]
            assert "tok_tip" in ids and "tok_branch" in ids
            tip_row = next(r for r in rows if r["id"] == "tok_tip")
            assert (tip_row["input_tokens"], tip_row["output_tokens"]) == (400, 40)
            assert db.chain_token_totals(["tok_tip"]) == {"tok_tip": (400, 40)}
        finally:
            db.close()

    def test_standalone_session_keeps_own_tokens(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "tok_standalone.db")
        db.create_session("tok_standalone", "cli")
        conn = db._conn
        conn.execute(
            "UPDATE sessions SET input_tokens=50, output_tokens=5 WHERE id='tok_standalone'"
        )
        conn.commit()
        try:
            rows = db.list_sessions_rich(source="cli", include_children=False)
            assert [r["id"] for r in rows] == ["tok_standalone"]
            assert rows[0]["input_tokens"] == 50
            assert rows[0]["output_tokens"] == 5
            assert db.chain_token_totals(["tok_standalone"]) == {
                "tok_standalone": (50, 5)
            }
        finally:
            db.close()


class TestRenderPreviewLineage:
    """Projected compression rows must preview the chain root's first user
    message — never the tip's own first message, which is the compaction
    banner on a continuation with no real user turns.

    Regression coverage for the plain listing leaking
    `[CONTEXT COMPACTION — REFERENCE ONLY]` into the Preview column: the
    projection keeps the root's parent_session_id (None), so the renderer's
    parent-walk fallback never fired and the tip's banner preview won.
    """

    def _render(self, db, rows):
        lines = []
        from hermes_cli.session_listing import render_sessions_table

        render_sessions_table(rows, out=lines.append, db=db)
        return "\n".join(lines)

    def test_projected_tip_previews_root_first_user_message(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "pv.db")
        db.create_session("pv_root", "cli")
        db.append_message("pv_root", "user", "Original conversation opener", timestamp=1.0)
        db.end_session("pv_root", end_reason="compression")
        db.create_session("pv_tip", "cli", parent_session_id="pv_root")
        db.append_message(
            "pv_tip", "user", "[CONTEXT COMPACTION — REFERENCE ONLY]", timestamp=2.0
        )
        try:
            rows = db.list_sessions_rich(source="cli", include_children=False)
            assert [r["id"] for r in rows] == ["pv_tip"]
            output = self._render(db, rows)
            assert "Original conversation opener" in output
            assert "COMPACTION" not in output
        finally:
            db.close()

    def test_standalone_row_previews_own_first_user_message(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "pv2.db")
        db.create_session("pv_standalone", "cli")
        db.append_message("pv_standalone", "user", "Standalone opener", timestamp=1.0)
        try:
            rows = db.list_sessions_rich(source="cli", include_children=False)
            output = self._render(db, rows)
            assert "Standalone opener" in output
        finally:
            db.close()
