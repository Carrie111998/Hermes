from __future__ import annotations

from server.lead_research.facts import FreshnessPolicy

from tests.server.lead_research.test_fact_pool import NOW, fact_fixture, fact_repo


DAY = 86_400.0


def test_reuse_is_decided_per_fact_not_per_bundle(fact_repo):
    fact_repo.accept(
        "cmp_a",
        fact_fixture(field="founded_year", value_en=1986, expires_at=NOW + DAY),
    )
    fact_repo.accept(
        "cmp_a",
        fact_fixture(
            evidence_id="ev_hiring",
            field="recent_hiring",
            value_en="12 open roles",
            expires_at=NOW - 1,
        ),
    )

    reused = fact_repo.reusable(
        "cmp_a", "org_a", {"founded_year", "recent_hiring"}, NOW,
    )

    assert {fact.field for fact in reused} == {"founded_year"}


def test_freshness_policy_uses_field_specific_shelf_lives():
    policy = FreshnessPolicy()

    founded = policy.expires_at("founded_year", "official", NOW, NOW)
    hiring = policy.expires_at("recent_hiring", "public", NOW, NOW)
    procurement = policy.expires_at("procurement_signal", "public", NOW, NOW)

    assert founded == NOW + 3650 * DAY
    assert hiring == NOW + 30 * DAY
    assert procurement == NOW + 90 * DAY


def test_archive_or_older_observation_controls_fact_expiry():
    policy = FreshnessPolicy()
    observed = NOW - 20 * DAY

    assert policy.expires_at("legal_status", "registry", observed, NOW) == (
        observed + 30 * DAY
    )


def test_missing_observation_falls_back_to_retrieval_time():
    policy = FreshnessPolicy(default_ttl_days=180)

    assert policy.expires_at("unmapped_field", "public", None, NOW) == NOW + 180 * DAY
