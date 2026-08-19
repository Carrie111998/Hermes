"""C4: metadata candidate router + bounded LIKE fallback (#128 / fork #14/#37/#89).

Routes a metadata search query to the session-metadata FTS lanes (Unicode /
CJK / trigram) and falls back to a bounded canonical LIKE scan when the
routed lane cannot serve or yields zero. ``list_sessions_rich(search_query=...)``
consumes the router candidate-first: FTS hits narrow the compression chain,
and only zero-candidate / unavailable routes run the LIKE lane.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _seed(db, rows):
    def _do(conn):
        for sid, title, dn, src in rows:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, title, display_name) "
                "VALUES (?,?,?,?,?)",
                (sid, src, 1.0, title, dn),
            )

    db._execute_write(_do)


class TestClassify:
    def test_classify_routes(self, db):
        assert db._classify_metadata_query("alpha") == "trigram"  # 5-char literal
        assert db._classify_metadata_query("財務") == "cjk"  # CJK takes precedence
        assert db._classify_metadata_query("a") == "like"  # < 3 chars
        assert db._classify_metadata_query('"exact phrase"') == "unicode"
        assert db._classify_metadata_query("  ") == "none"


class TestRouter:
    def test_unicode_candidates(self, db):
        _seed(db, [("s1", "Arby's Faribault, MN", None, "cli")])
        res = db._metadata_candidate_row_ids("Faribault")
        assert res.row_ids == (1,)

    def test_trigram_compact_candidate(self, db):
        _seed(db, [("s1", "AN-94 Project", None, "cli")])
        res = db._metadata_candidate_row_ids("an94")
        assert 1 in res.row_ids

    def test_like_fallback_on_zero(self, db):
        _seed(db, [("s1", "Alpha", None, "cli")])
        # trigram route matches nothing; the bounded LIKE lane runs once and
        # also finds nothing -> zero (never an unbounded scan).
        res = db._metadata_candidate_row_ids("Alphazzz")
        assert not res.row_ids

    def test_literal_percent_does_not_match_all(self, db):
        _seed(
            db,
            [("s1", "Alpha", None, "cli"), ("s2", "Beta", None, "cli")],
        )
        # A bare "%" is escaped on the LIKE fallback, never a match-all scan.
        res = db._metadata_candidate_row_ids("%")
        assert not res.row_ids

    def test_literal_underscore_does_not_match_all(self, db):
        _seed(
            db,
            [("s1", "AlphaBeta", None, "cli"), ("s2", "Gamma", None, "cli")],
        )
        # A bare "_" is escaped on the LIKE fallback — if it were a wildcard it
        # would match every row; as a literal it matches nothing.
        res = db._metadata_candidate_row_ids("_")
        assert not res.row_ids


class TestListSessionsRichRouter:
    def test_finds_stored_title_via_router(self, db):
        _seed(db, [("s1", "Arby's Faribault, MN", None, "cli")])
        rows = db.list_sessions_rich(
            order_by_last_active=True, search_query="Faribault"
        )
        assert any(r["id"] == "s1" for r in rows)

    def test_finds_display_name_via_router(self, db):
        _seed(
            db,
            [("s2", "Quarterly Budget Review", "Acme Guild / #finance", "gateway")],
        )
        rows = db.list_sessions_rich(
            order_by_last_active=True, search_query="finance"
        )
        assert any(r["id"] == "s2" for r in rows)

    def test_compact_query_finds_punctuated_title(self, db):
        _seed(db, [("s3", "AN-94 Project", None, "cli")])
        rows = db.list_sessions_rich(
            order_by_last_active=True, search_query="an94"
        )
        assert any(r["id"] == "s3" for r in rows)

    def test_no_false_positives_on_unrelated_query(self, db):
        _seed(
            db,
            [("s1", "Alpha", None, "cli"), ("s2", "Beta", None, "cli")],
        )
        rows = db.list_sessions_rich(
            order_by_last_active=True, search_query="zzzznope"
        )
        assert all(r["id"] not in {"s1", "s2"} for r in rows)
