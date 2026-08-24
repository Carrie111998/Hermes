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


def test_contract_backfill_migration_is_conservative_and_required():
    migration = (MIGRATIONS / "020_lead_research_contract_backfill.sql").read_text(encoding="utf-8").lower()

    assert "020_lead_research_contract_backfill" in PostgresDatabase.REQUIRED_MIGRATIONS
    assert "insert into shared_facts" not in migration
    assert "verification_tier='red'" in migration
    assert "visibility='service_public'" in migration
    assert "to authenticated" not in migration
