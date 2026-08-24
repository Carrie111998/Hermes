from __future__ import annotations

import pytest

from server.db import Database, now
from server.lead_research.models import ResearchQuery, SearchScope
from server.lead_research.search_cache import SearchAttemptRepository, query_scope


NOW = 2_000_000_000.0
HOUR = 3_600.0


@pytest.fixture()
def search_attempts(tmp_path):
    db = Database(tmp_path / "search-attempts.db")
    stamp = now()
    for company_id in ("cmp_a", "cmp_b"):
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (company_id, company_id, "active", "{}", stamp, stamp),
        )
    return SearchAttemptRepository(db)


def test_private_query_failure_is_not_visible_to_another_tenant(search_attempts):
    own_scope = SearchScope(company_id="cmp_a", shareable=False)
    other_scope = SearchScope(company_id="cmp_b", shareable=False)
    search_attempts.record_failure(
        own_scope, "hash_private", "timeout", NOW + HOUR,
        organization_id="org_1", field="recent_hiring", source_id="public_web",
        attempted_at=NOW,
    )

    assert search_attempts.lookup(own_scope, "hash_private", NOW) is not None
    assert search_attempts.lookup(other_scope, "hash_private", NOW) is None


def test_generic_public_failure_can_be_reused_without_tenant_origin(search_attempts):
    shared = SearchScope(company_id=None, shareable=True)
    saved = search_attempts.record_empty(
        shared, "hash_public", NOW + HOUR,
        organization_id="sorg_1", field="website", source_id="public_web",
        attempted_at=NOW,
    )

    reused = search_attempts.lookup(shared, "hash_public", NOW)

    assert reused is not None
    assert reused.id == saved.id
    assert reused.scope.company_id is None
    assert reused.status == "empty"


def test_expired_negative_cache_does_not_suppress_a_retry(search_attempts):
    scope = SearchScope(company_id="cmp_a", shareable=False)
    search_attempts.record_failure(
        scope, "hash_expired", "rate_limited", NOW + HOUR,
        organization_id="org_1", field="legal_status", source_id="registry",
        attempted_at=NOW,
    )

    assert search_attempts.lookup(scope, "hash_expired", NOW + HOUR + 1) is None


def test_customer_or_licensed_inputs_make_query_tenant_private():
    generic = ResearchQuery(
        company_id="cmp_a", organization_id="org_1", field="website",
        normalized_query_class="organization website",
    )
    customer_specific = generic.model_copy(update={"customer_terms": ["our ideal dealer"]})
    licensed = generic.model_copy(update={"licensed_source_ids": ["licensed_registry"]})

    assert query_scope(generic) == SearchScope(company_id=None, shareable=True)
    assert query_scope(customer_specific) == SearchScope(company_id="cmp_a", shareable=False)
    assert query_scope(licensed) == SearchScope(company_id="cmp_a", shareable=False)


def test_scope_rejects_ambiguous_tenant_semantics():
    with pytest.raises(ValueError):
        SearchScope(company_id="cmp_a", shareable=True)
    with pytest.raises(ValueError):
        SearchScope(company_id=None, shareable=False)
