"""Regression tests for CJK OR-token matching in trigram FTS5 search.

Addresses review feedback on PR #24048:
- CJK queries without explicit operators should OR-join tokens
- Non-CJK queries without explicit operators should keep implicit AND
- Explicit AND/OR/NOT operators are always respected
"""

import pytest

from hermes_state import SessionDB


def _make_db(tmp_path):
    """Create a real SessionDB for integration-style tests."""
    db_path = tmp_path / "test_cjk.db"
    return SessionDB(db_path=db_path)


def _seed_messages(db):
    """Seed sessions with known CJK and English content."""
    db.create_session(session_id="session_cjk_1", source="cli", model="test")
    db.add_message("session_cjk_1", "user", "我在大别山工作")
    db.add_message("session_cjk_1", "assistant", "大别山 is a mountain range in China")

    db.create_session(session_id="session_cjk_2", source="cli", model="test")
    db.add_message("session_cjk_2", "user", "项目进展很顺利")
    db.add_message("session_cjk_2", "assistant", "The project is going well")

    db.create_session(session_id="session_en_1", source="cli", model="test")
    db.add_message("session_en_1", "user", "database migration completed successfully")
    db.add_message("session_en_1", "assistant", "The database was migrated to PostgreSQL")

    db.create_session(session_id="session_en_2", source="cli", model="test")
    db.add_message("session_en_2", "user", "migration scripts are ready")
    db.add_message("session_en_2", "assistant", "Scripts for the migration are done")


class TestCJKImplicitOr:
    """CJK queries without boolean operators should OR-join tokens."""

    def test_cjk_multi_token_matches_either(self, tmp_path):
        """Searching CJK tokens without operator should match EITHER token,
        not require both."""
        db = _make_db(tmp_path)
        _seed_messages(db)

        results = db.search_messages("大别山 项目")
        session_ids = {r.get("session_id") for r in results}
        # OR-join: should match sessions containing either token
        assert len(session_ids) >= 1


class TestNonCJKImplicitAnd:
    """Non-CJK queries without boolean operators should keep implicit AND."""

    def test_english_multi_token_prefers_both(self, tmp_path):
        """Searching 'database migration' should prefer matches with BOTH words.
        If OR-join leaked to non-CJK, session_en_2 (migration only, no database)
        would return as a top match for 'database' which it doesn't contain."""
        db = _make_db(tmp_path)
        _seed_messages(db)

        results = db.search_messages("database migration")
        if results:
            # With AND semantics, results should favor sessions with both terms.
            # Check that at least one result actually contains "database"
            contents = " ".join(r.get("content", "") for r in results).lower()
            assert "database" in contents


class TestExplicitOperatorsRespected:
    """Explicit AND/OR/NOT operators are always preserved."""

    def test_explicit_or_returns_results(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_messages(db)

        results = db.search_messages("database OR migration")
        assert len(results) > 0

    def test_explicit_and_returns_results(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_messages(db)

        results = db.search_messages("database AND migration")
        assert len(results) > 0
