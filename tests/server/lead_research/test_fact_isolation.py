from __future__ import annotations

from pathlib import Path

from tests.server.lead_research.test_fact_pool import NOW, fact_fixture, fact_repo


def test_tenant_can_reuse_shared_and_own_facts_but_not_another_tenants(fact_repo):
    fact_repo.accept(
        "cmp_a",
        fact_fixture(source_class="customer", visibility="private",
                     mechanically_validated=False, value_en="distributor"),
    )
    fact_repo.accept(
        "cmp_b",
        fact_fixture(organization_id="org_b", evidence_id="ev_b",
                     source_class="customer", visibility="private",
                     mechanically_validated=False, value_en="wholesaler"),
    )

    mine = fact_repo.reusable("cmp_a", "org_a", {"buyer_role"}, NOW)

    assert {fact.value_en for fact in mine} == {"distributor"}


def test_validated_shared_fact_is_reusable_through_resolved_identity(fact_repo):
    fact_repo.accept("cmp_a", fact_fixture())

    facts = fact_repo.reusable("cmp_b", "org_b", {"buyer_role"}, NOW)

    assert {fact.value_en for fact in facts} == {"distributor"}
    assert {fact.pool for fact in facts} == {"shared"}


def test_shared_rows_reveal_no_originating_tenant_or_campaign(fact_repo):
    stored = fact_repo.accept("cmp_a", fact_fixture(campaign_id="rc_private"))

    fact_row = dict(fact_repo.db.one("SELECT * FROM shared_facts WHERE id=?", (stored.id,)))
    evidence_row = dict(fact_repo.db.one("SELECT * FROM shared_evidence_records LIMIT 1"))

    assert "company_id" not in fact_row
    assert "campaign_id" not in fact_row
    assert "company_id" not in evidence_row
    assert "campaign_id" not in evidence_row


def test_shared_consumption_is_tenant_scoped(fact_repo):
    stored = fact_repo.accept("cmp_a", fact_fixture())
    fact_repo.reusable("cmp_b", "org_b", {"buyer_role"}, NOW)

    consumers = fact_repo.db.all(
        "SELECT company_id FROM research_fact_consumers WHERE shared_fact_id=? ORDER BY company_id",
        (stored.id,),
    )

    assert [row["company_id"] for row in consumers] == ["cmp_a", "cmp_b"]


def test_shared_postgres_tables_are_rls_deny_all_to_clients():
    migration = (
        Path(__file__).resolve().parents[3]
        / "server/supabase/migrations/015_shared_research_facts.sql"
    ).read_text(encoding="utf-8").lower()

    for table in (
        "shared_organizations", "shared_evidence_records", "shared_facts",
        "shared_fact_evidence",
    ):
        assert f"alter table {table} enable row level security" in migration
        assert f"create policy {table}" not in migration
