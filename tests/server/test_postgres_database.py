"""Shared candidate-corpus Postgres parity contracts."""
from pathlib import Path

from server.postgres import PostgresDatabase


MIGRATIONS = Path(__file__).resolve().parents[2] / "server" / "supabase" / "migrations"


def test_candidate_corpus_migration_is_required_and_direct_client_access_is_denied():
    """A policy for a shared corpus could expose it through tenant client keys."""
    migration = (MIGRATIONS / "008_candidate_corpus.sql").read_text(encoding="utf-8")

    assert "008_candidate_corpus" in PostgresDatabase.REQUIRED_MIGRATIONS
    for table in ("candidate_datasets", "candidate_records"):
        assert f"alter table {table} enable row level security" in migration
        assert f"create policy {table}" not in migration
    assert "to anon" not in migration.lower()
    assert "to authenticated" not in migration.lower()
