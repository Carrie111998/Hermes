# Lead Research Contract Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make lead research implement `lead-research-idea.md` end to end, including versioned company inputs, tenant-safe candidate supply, reusable validated facts, criterion-aware agentic gap research, honest scoring, evidence-rich lead display, and outreach-safe contacts.

**Architecture:** Preserve the current campaign engine and HTTP surface, but add focused repositories around its missing boundaries. Campaigns freeze a versioned company profile; candidate discovery reads an explicit public/private visibility union; research reuses field-level facts before structured or agentic acquisition; scoring emits fit and uncertainty separately; customer and admin views consume immutable campaign result snapshots. SQLite remains the development source of truth and every schema change ships with a required, RLS-safe Postgres migration.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLite, PostgreSQL/Supabase SQL, pytest, vanilla JavaScript modules, Node's built-in test runner, existing `AgentRunService`, existing lead-research skill and scheduler.

**Spec:** `docs/superpowers/specs/2026-08-24-lead-research-contract-completion-design.md`

## Global Constraints

- `lead-research-idea.md` is the product authority when older behavior disagrees.
- Do not add a Hermes core model tool; agentic gaps run through the existing durable `AgentRunService` and lead-research skill.
- Resolve every customer request from the authenticated `Principal` plus `company_scope`; never trust a body-supplied tenant identifier.
- A campaign always references one immutable `company_profile_versions.id` and stores the resolved research scope snapshot.
- Existing candidate datasets migrate to `service_public`; authenticated uploads are `tenant_private` and readable only by their owner company.
- Only mechanically validated official-site or registry facts may enter the shared pool; licensed, customer, inferred, and unvalidated facts remain tenant-private.
- Store canonical fact values in English, retain exact original-language spans, and translate for display without replacing source text.
- Research all nonzero weighted criteria even when a structured provider cannot emit them; unsupported dimensions route to agentic gap research.
- Fit and evidence confidence are separate outputs; every score exposes `known_weight`, `unknown_weight`, and weighted unknown dimensions.
- Materialize leads only for `strong_fit` and `review`; terminal rejects and ineligible companies remain auditable campaign results.
- Never send verification email. Yellow/red contacts never enter CC, red contacts are never auto-primary, and generic addresses rank last.
- Every SQLite table or index has a required Postgres migration; tenant tables have RLS, and shared tables remain service-role only.
- Write behavior and invariant tests, not snapshots of mutable catalog counts or version literals.
- Each new test module defines the local fixtures named in its examples by composing the existing `Database`, FastAPI app, and `tests/server/lead_research/fakes.py` helpers.
- Run the targeted test named in each task before its commit; do not combine failing and passing states across commits.

## File Responsibility Map

- `server/lead_research/profiles.py`: immutable company research profiles, normalization, version creation, and current-version lookup.
- `server/lead_research/discovery.py`: candidate-source union, cheap-gate decisions, and excluded-count accounting.
- `server/lead_research/facts.py`: shared/tenant fact persistence, field-level reuse, correction propagation, and consumer tracking.
- `server/lead_research/languages.py`: canonical-English storage, market-term expansion, UI vocabularies, and cached display translation.
- `server/lead_research/gaps.py`: one per-company research gap plan covering profile and playbook requirements.
- `server/lead_research/agentic.py`: durable agent-run request/result contract and accepted-page batching.
- `server/lead_research/quotes.py`: exact-span validation, archive semantics, and evidence acceptance.
- `server/lead_research/labels.py`: hidden-label assignment history and admin-only outcome aggregation.
- `server/lead_research/contacts.py`: mechanical contact tiers, contact kind, ranking, and outreach eligibility.
- Existing `models.py`, `candidates.py`, `service.py`, `scoring.py`, `storage.py`, `registry.py`, and routes remain orchestrators or compatibility adapters rather than absorbing all new logic.

---

### Task 1: Freeze Company Research Profiles

**Files:**
- Create: `server/lead_research/profiles.py`
- Create: `server/supabase/migrations/012_company_profile_versions.sql`
- Modify: `server/db.py`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Modify: `server/lead_research/models.py`
- Test: `tests/server/lead_research/test_profile_versions.py`
- Test: `tests/server/test_postgres_parity.py`

**Interfaces:**
- Consumes: existing `Database`, `new_id(prefix)`, `now()`, and JSON helpers from `server.db`.
- Produces: `CompanyResearchProfile`, `CompanyProfileVersion`, `ProfileRepository.create_version(company_id: str, actor_id: str, profile: CompanyResearchProfile) -> CompanyProfileVersion`; `ProfileRepository.get(company_id: str, profile_id: str) -> CompanyProfileVersion | None`; `ProfileRepository.current(company_id: str) -> CompanyProfileVersion | None`.

- [ ] **Step 1: Write the failing profile-version and migration tests**

```python
def test_profile_versions_are_immutable_and_tenant_scoped(db):
    repo = ProfileRepository(db)
    first = repo.create_version("cmp_a", "usr_a", CompanyResearchProfile(
        identity={"name": "Acme", "website": "https://acme.test"},
        seller_countries=["TR"],
        products=[{"id": "prd_valve", "name": "Vana", "english_name": "Valve", "hs_codes": ["8481"], "sector_ids": ["industrial-machinery"], "emphasis": 1.0}],
        market_preferences={"target_countries": ["DE"], "languages": ["de", "en"]},
        hidden_label_ids=["lbl_export_ready"],
        playbook_versions={"industrial-machinery": "1"},
    ))
    second = repo.create_version("cmp_a", "usr_a", first.profile.model_copy(update={"seller_countries": ["TR", "DE"]}))
    assert first.id != second.id
    assert repo.get("cmp_a", first.id).profile.seller_countries == ["TR"]
    assert repo.current("cmp_a").id == second.id
    assert repo.get("cmp_b", first.id) is None
```

- [ ] **Step 2: Run the focused tests and confirm the missing repository/schema failure**

Run: `scripts/run_tests.sh tests/server/lead_research/test_profile_versions.py tests/server/test_postgres_parity.py -q`

Expected: FAIL because `server.lead_research.profiles` and migration `012_company_profile_versions` do not exist.

- [ ] **Step 3: Add the immutable model, repository, SQLite schema, and RLS-safe migration**

```python
class CompanyResearchProfile(ApiModel):
    identity: dict[str, str | None]
    seller_countries: list[str]
    products: list[dict[str, Any]]
    market_preferences: dict[str, Any]
    research_exclusions: dict[str, Any] = Field(default_factory=dict)
    hidden_label_ids: list[str] = Field(default_factory=list)
    hidden_label_provenance: dict[str, str] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confirmations: dict[str, bool] = Field(default_factory=dict)
    playbook_versions: dict[str, str] = Field(default_factory=dict)

class CompanyProfileVersion(ApiModel):
    id: str
    company_id: str
    version: int
    status: Literal["draft", "confirmed", "superseded"]
    profile: CompanyResearchProfile
    created_by: str
    confirmed_by: str | None
    created_at: float
    confirmed_at: float | None
    superseded_at: float | None

class ProfileRepository:
    def create_version(self, company_id: str, actor_id: str, profile: CompanyResearchProfile) -> CompanyProfileVersion:
        row = self.db.one("SELECT COALESCE(MAX(version),0)+1 AS next_version FROM company_profile_versions WHERE company_id=?", (company_id,))
        version = int(row["next_version"])
        profile_id, stamp = new_id("cpv"), now()
        self.db.execute(
            "INSERT INTO company_profile_versions(id,company_id,version,status,profile_json,created_by,confirmed_by,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (profile_id, company_id, version, "confirmed", json_dump(profile.model_dump(mode="json")), actor_id, actor_id, stamp, stamp),
        )
        return self.get(company_id, profile_id)
```

Normalization resolves the official domain without translating names/addresses, validates at least one seller country and product/scope source, and preserves source/evidence IDs. Confirmed rows are append-only; creating a new confirmed version marks the prior row superseded without rewriting its profile. Add `012_company_profile_versions` to `PostgresDatabase.REQUIRED_MIGRATIONS`. The SQL migration creates the table, unique `(company_id, version)`, current-version index, campaign `profile_version_id`/`created_by`/`updated_by` columns, tenant RLS policy using the existing company claim helper, and records itself in `schema_migrations`.

- [ ] **Step 4: Run the focused tests and confirm profile immutability and parity**

Run: `scripts/run_tests.sh tests/server/lead_research/test_profile_versions.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the profile-version slice**

```bash
git add server/lead_research/profiles.py server/lead_research/models.py server/db.py server/postgres.py server/supabase/migrations/012_company_profile_versions.sql server/supabase/verify.sql tests/server/lead_research/test_profile_versions.py
git commit -m "feat(interfaze): version company research profiles"
```

### Task 2: Derive and Confirm Profiles During Company Onboarding

**Files:**
- Modify: `server/routes/onboarding.py`
- Modify: `server/routes/company.py`
- Modify: `server/agent_service.py`
- Modify: `server/run_types.py`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/pages/setup.js`
- Test: `tests/server/test_company_research_profile.py`
- Create: `tests/server/webui/test_research_onboarding.mjs`

**Interfaces:**
- Consumes: `ProfileRepository` and `CompanyResearchProfile` from Task 1; existing durable `AgentRunService.create(company_id, run_type, payload)`.
- Produces: `GET /company/research-profile`, `PUT /company/research-profile`, `POST /onboarding/research-profile`, and onboarding run type `company_profile_research` whose quote-validated output is returned only as an editable suggestion.

- [ ] **Step 1: Write failing API and setup-page tests**

```python
def test_confirmed_profile_uses_principal_scope_and_versions_changes(client, user_headers, db):
    body = {"identity": {"name": "Acme", "website": "https://acme.test"}, "seller_countries": ["TR"], "products": [{"id": "prd_1", "name": "Vana", "english_name": "Valve", "hs_codes": ["8481"], "sector_ids": ["industrial-machinery"], "emphasis": 1}], "market_preferences": {"target_countries": ["DE"], "languages": ["de", "en"]}, "hidden_label_ids": [], "playbook_versions": {"industrial-machinery": "1"}}
    first = client.put("/company/research-profile", headers=user_headers, json=body)
    second = client.put("/company/research-profile", headers=user_headers, json={**body, "seller_countries": ["TR", "DE"]})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert db.one("SELECT COUNT(*) AS n FROM company_profile_versions WHERE company_id=?", (user_headers["X-Company-Id"],))["n"] == 2
```

```javascript
test('setup requires explicit confirmation of derived products and emphasis', () => {
  const view = researchProfileStep({ derived: true, confirmed: false, products: [{ name: 'Vana', english_name: 'Valve', emphasis: 1 }] });
  assert.match(view, /Confirm research profile/);
  assert.match(view, /Valve/);
  assert.match(view, /emphasis/i);
});
```

- [ ] **Step 2: Run the focused Python and Node tests and confirm route/view failures**

Run: `scripts/run_tests.sh tests/server/test_company_research_profile.py -q`

Run: `node --test tests/server/webui/test_research_onboarding.mjs`

Expected: both FAIL because the profile endpoints, run type, and confirmation view are absent.

- [ ] **Step 3: Implement bounded profile research and explicit confirmation**

```python
class OfficialWebsiteInput(BaseModel):
    official_website: HttpUrl

@router.get("/research-profile")
def get_research_profile(request: Request, principal: Principal = Depends(current_principal), x_company_id: str | None = Header(default=None)):
    profile = ProfileRepository(request.app.state.db).current(_scope(principal, x_company_id))
    if profile is None:
        raise HTTPException(404, "Confirmed research profile not found")
    return profile

@router.put("/research-profile")
def put_research_profile(body: CompanyResearchProfile, request: Request, principal: Principal = Depends(current_principal), x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return ProfileRepository(request.app.state.db).create_version(company_id, principal.id, body)

@router.post("/research-profile", status_code=202)
def research_profile(body: OfficialWebsiteInput, request: Request, principal: Principal = Depends(current_principal), x_company_id: str | None = Header(default=None)):
    company_id = _company(principal, x_company_id)
    return request.app.state.runs.create(company_id, "company_profile_research", {"official_website": str(body.official_website), "max_pages": 8, "max_seconds": 120})

def _company_profile_research(company, payload, context=None):
    return _ctx(company, context) + _p(payload) + "\n\nResearch only the supplied official website and linked official product pages. Return JSON with identity, seller_countries, products{name,english_name,hs_codes,sector_ids,emphasis}, market_preferences, and cited source spans. Treat emphasis and hidden labels as suggestions requiring user confirmation."
```

Register `company_profile_research` as a read-only run type, validate every accepted product against a source span, and render derived values as editable suggestions. The save action calls `PUT /company/research-profile`; it never silently confirms agent output.

- [ ] **Step 4: Run onboarding, company, and agent-run tests**

Run: `scripts/run_tests.sh tests/server/test_company_research_profile.py tests/server/test_run_harness.py tests/server/test_api_mvp.py -q`

Run: `node --test tests/server/webui/test_research_onboarding.mjs`

Expected: PASS.

- [ ] **Step 5: Commit onboarding integration**

```bash
git add server/routes/onboarding.py server/routes/company.py server/agent_service.py server/run_types.py server/webui/js/api.js server/webui/js/pages/setup.js tests/server/test_company_research_profile.py tests/server/webui/test_research_onboarding.mjs
git commit -m "feat(interfaze): confirm research profiles in onboarding"
```

### Task 3: Freeze Campaign Scope and Accept Plain Product Names

**Files:**
- Modify: `server/lead_research/models.py`
- Modify: `server/lead_research/service.py`
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/webui/js/pages/research-brief.js`
- Modify: `server/webui/js/pages/research.js`
- Modify: `server/webui/js/pages/research-editor.js`
- Modify: `server/webui/js/pages/research-source-picker.js`
- Modify: `tests/server/lead_research/test_campaign_dispatch.py`
- Modify: `tests/server/lead_research/test_query_shape.py`
- Modify: `tests/server/webui/test_research_brief.mjs`

**Interfaces:**
- Consumes: `ProfileRepository.current(company_id)` from Task 1.
- Produces: `CampaignConfig.product_terms: list[str]`; `ResearchReadiness(ready: bool, missing: list[str])`; `LeadResearchService.validate_readiness(company_id: str, config: CampaignConfig) -> ResearchReadiness`; persisted campaign fields `profile_version_id`, `scope_snapshot`, `created_by`, `updated_by`; `DiscoveryQuery.product_terms` populated with canonical names.

- [ ] **Step 1: Add failing scope-freeze and plain-name tests**

```python
def test_campaign_freezes_profile_and_plain_product_terms(service, profile_repo):
    profile = profile_repo.create_version("cmp_a", "usr_a", profile_fixture())
    campaign = service.create_campaign("cmp_a", {"name": "German valves", "config": {"product_terms": ["industrial valve"], "target_countries": ["DE"]}}, actor_id="usr_a")
    assert campaign["profile_version_id"] == profile.id
    assert campaign["scope_snapshot"]["product_terms"] == ["industrial valve"]
    profile_repo.create_version("cmp_a", "usr_a", profile.profile.model_copy(update={"products": []}))
    query = service.discovery_query(campaign["id"], "cmp_a")
    assert "industrial valve" in query.product_terms

def test_estimate_and_clone_keep_customer_meaning(service, runnable_source):
    campaign = service.create_campaign("cmp_a", campaign_body(weights_in_five_point_steps=True), actor_id="usr_a")
    estimate = service.estimate("cmp_a", campaign["id"])
    assert set(estimate) >= {"indexed_candidates", "discoverable_candidates", "unavailable_sources", "unmapped_market_terms"}
    clone = service.clone_campaign("cmp_a", campaign["id"], actor_id="usr_b")
    assert clone["id"] != campaign["id"]
    assert clone["config"]["scoring"] == campaign["config"]["scoring"]
    assert clone["profile_version_id"] == campaign["profile_version_id"]

def test_campaign_readiness_names_every_missing_requirement(service, unconfirmed_profile):
    readiness = service.validate_readiness("cmp_a", campaign_config(target_countries=[], enabled_source_ids=[]))
    assert readiness.ready is False
    assert set(readiness.missing) == {"confirmed_profile", "identity_or_admin_exception", "seller_country", "product_scope", "target_market", "runnable_candidate_source"}
```

```javascript
test('brief accepts a product name without sector or HS selection', () => {
  const brief = readResearchBrief({ productTerms: ['industrial valve'], sectorIds: [], hsCodes: [], productIds: [] });
  assert.deepEqual(brief.product_terms, ['industrial valve']);
});
```

- [ ] **Step 2: Run focused tests and confirm validation/storage failures**

Run: `scripts/run_tests.sh tests/server/lead_research/test_campaign_dispatch.py tests/server/lead_research/test_query_shape.py -q`

Run: `node --test tests/server/webui/test_research_brief.mjs`

Expected: FAIL because plain product terms and immutable profile references are not part of the campaign contract.

- [ ] **Step 3: Extend the campaign model, route, storage, and two editors**

```python
class CampaignConfig(ApiModel):
    sector_ids: list[str] = Field(default_factory=list)
    hs_codes: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    product_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_product_scope(self):
        if not (self.sector_ids or self.hs_codes or self.product_ids or self.product_terms):
            raise ValueError("select a sector, HS code, company product, or enter a product name")
        return self
```

At create time resolve company product IDs to their versioned `english_name`, merge them with normalized `product_terms`, and save that immutable list in `scope_snapshot`. Readiness requires a confirmed profile; official identity/website or explicit admin identity exception; seller country; product name/record/sector/HS scope; target market; runnable named-company source or visible candidate corpus; and a five-point scoring profile totaling 100. Both simple and advanced editors expose a company-product picker plus free-text product chips and send the same API shape. The simple brief asks which runnable sources to use and never aborts merely because `corpus_candidates` is zero when a discoverable source is selected. Preserve cloning with independent editable scope/weights and estimates split into indexed candidates, discoverable candidates, unavailable sources, and unmapped local terms.

- [ ] **Step 4: Run campaign API and editor tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_campaign_dispatch.py tests/server/lead_research/test_query_shape.py tests/server/test_research_webui.py -q`

Run: `node --test tests/server/webui/test_research_brief.mjs`

Expected: PASS.

- [ ] **Step 5: Commit the frozen campaign-scope slice**

```bash
git add server/lead_research/models.py server/lead_research/service.py server/routes/research_campaigns.py server/webui/js/pages/research-brief.js server/webui/js/pages/research.js server/webui/js/pages/research-editor.js server/webui/js/pages/research-source-picker.js tests/server/lead_research/test_campaign_dispatch.py tests/server/lead_research/test_query_shape.py tests/server/webui/test_research_brief.mjs
git commit -m "feat(interfaze): freeze lead research campaign scope"
```

### Task 4: Enforce Candidate Dataset Ownership and Visibility

**Files:**
- Modify: `server/lead_research/candidates.py`
- Modify: `server/lead_research/providers/corpus.py`
- Create: `server/supabase/migrations/013_candidate_visibility.sql`
- Modify: `server/db.py`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/__main__.py`
- Test: `tests/server/lead_research/test_candidate_visibility.py`
- Modify: `tests/server/lead_research/test_candidate_corpus.py`

**Interfaces:**
- Consumes: principal/company scoping used by research routes.
- Produces: `CandidateVisibility = Literal["service_public", "tenant_private", "licensed_private"]`; `CandidateRepository.import_file(dataset_id: str, version: str, filename: str, content: bytes, *, owner_company_id: str | None, visibility: CandidateVisibility) -> CandidateImportReport`; `CandidateRepository.select(*, company_id: str, countries: list[str], product_terms: list[str], limit: int, exclude: set[tuple[str, str]] | None = None) -> list[CandidateRecord]`.

- [ ] **Step 1: Write the failing two-tenant visibility tests**

```python
def test_candidate_search_unions_public_and_owner_private_only(candidate_repo):
    candidate_repo.import_file("pub", "1", "public.csv", PUBLIC_CSV, owner_company_id=None, visibility="service_public")
    candidate_repo.import_file("a", "1", "a.csv", A_CSV, owner_company_id="cmp_a", visibility="tenant_private")
    candidate_repo.import_file("b", "1", "b.csv", B_CSV, owner_company_id="cmp_b", visibility="tenant_private")
    names = {row.normalized_name for row in candidate_repo.select(company_id="cmp_a", countries=[], product_terms=["valve"], limit=20)}
    assert names == {"public valve gmbh", "a private valve as"}

def test_existing_unowned_datasets_backfill_as_service_public(migrated_db):
    row = migrated_db.one("SELECT visibility,owner_company_id FROM candidate_datasets WHERE id='legacy'")
    assert row["visibility"] == "service_public"
    assert row["owner_company_id"] is None
```

- [ ] **Step 2: Run candidate and parity tests and confirm the cross-tenant leak**

Run: `scripts/run_tests.sh tests/server/lead_research/test_candidate_visibility.py tests/server/lead_research/test_candidate_corpus.py tests/server/test_postgres_parity.py -q`

Expected: FAIL because candidate datasets lack ownership/visibility and searches read the global corpus.

- [ ] **Step 3: Add ownership-aware import, search, upload route, and CLI flags**

```python
CandidateVisibility = Literal["service_public", "tenant_private", "licensed_private"]

def _visibility_sql(company_id: str) -> tuple[str, Sequence[str]]:
    return "(d.visibility='service_public' OR (d.visibility IN ('tenant_private','licensed_private') AND d.owner_company_id=?))", (company_id,)

@router.post("/candidate-datasets", status_code=201)
async def upload_candidate_dataset(request: Request, file: UploadFile, principal: Principal = Depends(current_principal), x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    filename = file.filename or "upload.csv"
    dataset_id = f"tenant-{company_id}-{new_id('dataset')}"
    return CandidateRepository(request.app.state.db).import_file(dataset_id, "1", filename, await file.read(), owner_company_id=company_id, visibility="tenant_private")
```

The CLI requires an explicit `--visibility service-public` or `--owner-company-id CMP`; reject ambiguous imports. Migration 013 adds columns and constraints, backfills existing datasets, adds ownership indexes, applies tenant RLS to private rows, and remains service-role writable.

- [ ] **Step 4: Run candidate, route, CLI, and Postgres tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_candidate_visibility.py tests/server/lead_research/test_candidate_corpus.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit candidate isolation**

```bash
git add server/lead_research/candidates.py server/lead_research/providers/corpus.py server/routes/research_campaigns.py server/__main__.py server/db.py server/postgres.py server/supabase/migrations/013_candidate_visibility.sql server/supabase/verify.sql tests/server/lead_research/test_candidate_visibility.py tests/server/lead_research/test_candidate_corpus.py
git commit -m "fix(interfaze): isolate private candidate datasets"
```

### Task 5: Separate Candidate Discovery from Field Research

**Files:**
- Modify: `server/lead_research/models.py`
- Modify: `server/lead_research/providers/base.py`
- Modify: `server/lead_research/registry.py`
- Create: `server/lead_research/discovery.py`
- Modify: `server/lead_research/acquisition.py`
- Modify: `server/lead_research/providers/corpus.py`
- Modify: `server/lead_research/providers/bright_data.py`
- Test: `tests/server/lead_research/test_candidate_discovery.py`
- Modify: `tests/server/lead_research/test_foundation.py`

**Interfaces:**
- Consumes: `DiscoveryQuery`, `CandidateRepository.select(*, company_id, countries, product_terms, limit, exclude)` from Task 4.
- Produces: `SourceCapability`; runtime-checkable `CandidateSource.discover_candidates(query: DiscoveryQuery, cursor: str | None = None) -> RawPage`; `StructuredFactSource.research_fields(company: CandidateRecord, fields: frozenset[str], query: DiscoveryQuery) -> VerificationBundle`; `CandidateDiscoveryService.supply(company_id: str, query: DiscoveryQuery, limit: int) -> CandidateSupply` with named source/exclusion counts.

- [ ] **Step 1: Write failing provider-capability and no-corpus discovery tests**

```python
def test_public_source_can_supply_candidates_when_corpus_is_empty(db, public_source):
    registry = ProviderRegistry([public_source])
    query = DiscoveryQuery(campaign_id="rc_1", seller_countries=["TR"], target_countries=["DE"], product_terms=["industrial valve"], max_records=20)
    supply = CandidateDiscoveryService(db, registry).supply("cmp_a", query, 20)
    assert [candidate.company_name for candidate in supply.candidates] == ["Neue Ventil GmbH"]
    assert supply.counts["public_source_discovered"] == 1

def test_customer_catalog_exposes_only_runnable_sources(registry):
    assert all(item["runnable"] for item in registry.customer_catalog())
    assert {item["id"] for item in registry.admin_setup_catalog()} >= {"customer-list-corpus", "bright-data"}
```

- [ ] **Step 2: Run discovery/foundation tests and confirm the corpus precondition failure**

Run: `scripts/run_tests.sh tests/server/lead_research/test_candidate_discovery.py tests/server/lead_research/test_foundation.py -q`

Expected: FAIL because provider capability is monolithic and campaigns refuse an empty corpus before external discovery.

- [ ] **Step 3: Add explicit provider capabilities and candidate-source union**

```python
class SourceCapability(ApiModel):
    source_id: str
    candidate_discovery: bool
    emitted_fields: frozenset[str]
    countries: frozenset[str]
    sector_ids: frozenset[str]
    access_class: Literal["public", "customer_upload", "licensed", "credentialed"]
    freshness_days: dict[str, int]
    max_concurrency: int = Field(ge=1)
    authority: Literal["official", "registry", "credible", "customer", "licensed"]
    redistributable: bool
    executable: bool

@runtime_checkable
class CandidateSource(Protocol):
    definition: DatasetDefinition
    def discover_candidates(self, query: DiscoveryQuery, cursor: str | None = None) -> RawPage:
        raise NotImplementedError

@runtime_checkable
class StructuredFactSource(Protocol):
    definition: DatasetDefinition
    def research_fields(self, company: CandidateRecord, fields: frozenset[str], query: DiscoveryQuery) -> VerificationBundle:
        raise NotImplementedError

class CandidateSupply(ApiModel):
    candidates: list[CandidateRecord]
    counts: dict[str, int]
```

`CandidateDiscoveryService.supply` unions tenant upload, service corpus, public authoritative sources, and licensed sources; resolves duplicates through the existing identity resolver; records how many spellings/rows collapse into each company; and does not require a corpus hit. Market aggregates and event-only directories cannot implement `CandidateSource`. Customer catalog filters to configured runnable adapters. Disabled runnable adapters may appear as disabled; catalog-only/setup entries stay admin-only.

- [ ] **Step 4: Run provider, discovery, and campaign dispatch tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_candidate_discovery.py tests/server/lead_research/test_foundation.py tests/server/lead_research/test_campaign_dispatch.py -q`

Expected: PASS.

- [ ] **Step 5: Commit provider separation**

```bash
git add server/lead_research/models.py server/lead_research/providers/base.py server/lead_research/registry.py server/lead_research/discovery.py server/lead_research/acquisition.py server/lead_research/providers/corpus.py server/lead_research/providers/bright_data.py tests/server/lead_research/test_candidate_discovery.py tests/server/lead_research/test_foundation.py
git commit -m "refactor(interfaze): separate lead discovery from research"
```

### Task 6: Add Local-Language Market Terms and Canonical Translation

**Files:**
- Create: `server/lead_research/languages.py`
- Modify: `server/lead_research/sectors.py`
- Modify: `server/lead_research/models.py`
- Modify: `skills/sales/lead-research/references/sectors.yaml`
- Modify: `skills/sales/lead-research/references/feature-playbooks.yaml`
- Modify: `server/db.py`
- Modify: `server/postgres.py`
- Create: `server/supabase/migrations/014_research_translations.sql`
- Modify: `server/supabase/verify.sql`
- Test: `tests/server/lead_research/test_market_languages.py`
- Modify: `tests/server/lead_research/test_query_shape.py`

**Interfaces:**
- Consumes: frozen campaign scope from Task 3 and target countries/languages from its profile.
- Produces: `MarketTermSet(canonical: list[str], by_language: dict[str, list[str]], unmapped_markets: list[str])`; `build_market_terms(scope: dict, profile: CompanyResearchProfile) -> MarketTermSet`; `TranslationCache.evidence_value(*, company_id: str, fact_key: str, value_en: str, original: str, language: str, locale: Literal["en","tr"]) -> dict[str, str]`.

- [ ] **Step 1: Write failing Turkish/German query and canonical-value tests**

```python
def test_market_terms_expand_product_and_buyer_language_without_changing_canonical_value():
    terms = build_market_terms({"product_terms": ["industrial valve"], "sector_ids": ["industrial-machinery"]}, profile_fixture(target_countries=["DE"], languages=["de", "en"]))
    assert terms.canonical == ["industrial valve"]
    assert "Industriearmatur" in terms.by_language["de"]
    assert "Einkaufsleiter" in terms.by_language["de"]

def test_display_translation_retains_original_and_canonical(cache):
    rendered = cache.evidence_value(company_id="cmp_a", fact_key="tf_1", value_en="public procurement award", original="kamu ihalesi", language="tr", locale="en")
    assert rendered == {"canonical": "public procurement award", "original": "kamu ihalesi", "display": "public procurement award", "source_language": "tr"}
```

- [ ] **Step 2: Run language and query-shape tests and confirm missing term expansion**

Run: `scripts/run_tests.sh tests/server/lead_research/test_market_languages.py tests/server/lead_research/test_query_shape.py -q`

Expected: FAIL because sector playbooks contain no market-language term map and query shape omits it.

- [ ] **Step 3: Implement normalized language expansion and display cache**

```python
class MarketTermSet(ApiModel):
    canonical: list[str]
    by_language: dict[str, list[str]]
    unmapped_markets: list[str]

def build_market_terms(scope: dict, profile: CompanyResearchProfile) -> MarketTermSet:
    languages = ordered_languages(profile.market_preferences)
    canonical = dedupe_clean(scope.get("product_terms", []))
    expanded = {language: dedupe_clean(canonical + playbook_terms(scope.get("sector_ids", []), language)) for language in languages}
    mapped_markets = mapped_target_markets(scope.get("sector_ids", []), profile.market_preferences)
    targets = profile.market_preferences.get("target_countries", [])
    return MarketTermSet(canonical=canonical, by_language=expanded, unmapped_markets=[country for country in targets if country not in mapped_markets])

def evidence_value(self, *, company_id: str, fact_key: str, value_en: str, original: str, language: str, locale: Literal["en", "tr"]) -> dict[str, str]:
    display = value_en if locale == "en" else self.get_or_generate(company_id, fact_key, value_en, locale)
    return {"canonical": value_en, "original": original, "display": display, "source_language": language}
```

Add explicit `market_terms` maps to each shipped sector/playbook for supported target markets, including product synonyms and buyer-role vocabulary. Fixed UI vocabulary is dictionary-backed for English/Turkish; free text uses the existing model path once and caches by company/fact/content hash/source language/display locale. Migration 014 creates the tenant-scoped translation cache with RLS, records itself, and is required at startup; cached text never replaces canonical English or the original span.

- [ ] **Step 4: Run sector generation, language, and query tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_market_languages.py tests/server/lead_research/test_query_shape.py tests/server/lead_research/test_foundation.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py -q`

Expected: PASS and sector reference generation remains deterministic.

- [ ] **Step 5: Commit market-language support**

```bash
git add server/lead_research/languages.py server/lead_research/sectors.py server/lead_research/models.py skills/sales/lead-research/references/sectors.yaml skills/sales/lead-research/references/feature-playbooks.yaml server/db.py server/postgres.py server/supabase/migrations/014_research_translations.sql server/supabase/verify.sql tests/server/lead_research/test_market_languages.py tests/server/lead_research/test_query_shape.py
git commit -m "feat(interfaze): expand research in market languages"
```

### Task 7: Persist Shared and Tenant Fact Pools

**Files:**
- Create: `server/lead_research/facts.py`
- Create: `server/supabase/migrations/015_shared_research_facts.sql`
- Modify: `server/db.py`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Modify: `server/lead_research/models.py`
- Modify: `server/lead_research/identity.py`
- Test: `tests/server/lead_research/test_fact_pool.py`
- Test: `tests/server/lead_research/test_fact_isolation.py`

**Interfaces:**
- Consumes: existing identity resolver and evidence IDs.
- Produces: `ResolvedIdentity`, `EvidenceSpan`, `ResearchFact`, `FactRepository.accept(company_id: str, fact: ResearchFact) -> StoredFact`; `FactRepository.reusable(company_id: str, organization_id: str, fields: set[str], at: float) -> list[StoredFact]`; tenant-free `shared_evidence_records` and `shared_fact_evidence` links for promoted facts.

- [ ] **Step 1: Write failing promotion, sharing, and tenant-isolation tests**

```python
def test_only_validated_public_authority_promotes_to_shared(fact_repo):
    shared = fact_repo.accept("cmp_a", fact_fixture(source_class="official", mechanically_validated=True, visibility="public"))
    private = fact_repo.accept("cmp_a", fact_fixture(source_class="licensed", mechanically_validated=True, visibility="licensed"))
    inferred = fact_repo.accept("cmp_a", fact_fixture(source_class="official", mechanically_validated=False, visibility="public"))
    assert shared.pool == "shared"
    assert private.pool == inferred.pool == "tenant"

def test_tenant_can_reuse_shared_and_own_facts_but_not_another_tenants(fact_repo):
    fact_repo.accept("cmp_a", fact_fixture(field="buyer_role", value="distributor", source_class="customer", mechanically_validated=False))
    fact_repo.accept("cmp_b", fact_fixture(field="buyer_role", value="wholesaler", source_class="customer", mechanically_validated=False))
    values = {fact.value_en for fact in fact_repo.reusable("cmp_a", "org_1", {"buyer_role"}, NOW)}
    assert values == {"distributor"}

def test_shared_rows_reveal_no_originating_tenant_or_campaign(fact_repo):
    stored = fact_repo.accept("cmp_a", fact_fixture(source_class="registry", mechanically_validated=True, visibility="public"))
    row = dict(fact_repo.db.one("SELECT * FROM shared_facts WHERE id=?", (stored.id,)))
    assert "company_id" not in row
    assert "campaign_id" not in row
```

- [ ] **Step 2: Run fact-pool and Postgres parity tests and confirm missing storage boundary**

Run: `scripts/run_tests.sh tests/server/lead_research/test_fact_pool.py tests/server/lead_research/test_fact_isolation.py tests/server/test_postgres_parity.py -q`

Expected: FAIL because all current evidence/claims are tenant-scoped and no promotion rule exists.

- [ ] **Step 3: Implement identity/fact models, promotion policy, schema, and migration**

```python
class EvidenceSpan(ApiModel):
    original: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)

class ResearchFact(ApiModel):
    organization_id: str
    field: str
    value_en: Any
    original_text: str
    source_language: str
    derivation_kind: Literal["observed", "translated", "calculated"]
    period: str | None = None
    unit: str | None = None
    currency: str | None = None
    status: Literal["observed", "unknown", "conflicted", "withdrawn"] = "observed"
    confidence: float = Field(ge=0, le=1)
    validation_basis: str
    evidence_id: str
    span: EvidenceSpan
    source_class: Literal["official", "registry", "public", "licensed", "customer"]
    visibility: Literal["public", "licensed", "private"]
    mechanically_validated: bool
    observed_at: float | None
    retrieved_at: float
    expires_at: float

def _pool_for(fact: ResearchFact) -> Literal["shared", "tenant"]:
    if fact.visibility == "public" and fact.source_class in {"official", "registry"} and fact.mechanically_validated:
        return "shared"
    return "tenant"
```

Migration 015 creates `shared_organizations`, `shared_evidence_records`, `shared_facts`, `shared_fact_evidence`, `tenant_facts`, and `research_fact_consumers`; shared tables contain no originating tenant/campaign identifier and have RLS enabled with no anon/authenticated policies, tenant tables use company RLS, unique fact identity includes organization/field/value/evidence, and indexes support reusable field lookup. Existing tenant organizations/evidence/claims remain compatibility materialization and gain optional shared identity/fact references for dual-write migration.

- [ ] **Step 4: Run fact, identity, parity, and existing evidence tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_fact_pool.py tests/server/lead_research/test_fact_isolation.py tests/server/lead_research/test_identity_name_matching.py tests/server/lead_research/test_evidence_reuse.py tests/server/test_postgres_parity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit shared fact infrastructure**

```bash
git add server/lead_research/facts.py server/lead_research/models.py server/lead_research/identity.py server/db.py server/postgres.py server/supabase/migrations/015_shared_research_facts.sql server/supabase/verify.sql tests/server/lead_research/test_fact_pool.py tests/server/lead_research/test_fact_isolation.py
git commit -m "feat(interfaze): add validated shared research facts"
```

### Task 8: Store Exact Source Spans and Validate Evidence Acceptance

**Files:**
- Create: `server/lead_research/quotes.py`
- Modify: `server/lead_research/models.py`
- Modify: `server/lead_research/storage.py`
- Modify: `server/lead_research/providers/bright_data.py`
- Modify: `server/lead_research/providers/ted.py`
- Test: `tests/server/lead_research/test_quote_validation.py`
- Modify: `tests/server/lead_research/test_validated_evidence.py`

**Interfaces:**
- Consumes: `EvidenceSpan`, `ResearchFact`, and `FactRepository.accept` from Task 7; existing immutable snapshots.
- Produces: `validate_span(content: str, span: EvidenceSpan) -> SpanValidation`; `accept_fact(envelope: EvidenceEnvelope, proposed: ResearchFact) -> ResearchFact` which rejects non-substrings and invalid archive semantics.

- [ ] **Step 1: Write failing exact-span and archive-date tests**

```python
def test_quote_must_be_exact_substring_of_immutable_snapshot():
    page = "Şirket 2024 yılında Almanya'da yeni bir dağıtım merkezi açtı."
    accepted = validate_span(page, EvidenceSpan(original="Almanya'da yeni bir dağıtım merkezi", start=20, end=55))
    rejected = validate_span(page, EvidenceSpan(original="opened a new distribution center in Germany", start=0, end=43))
    assert accepted.valid is True
    assert rejected.valid is False

def test_archive_snapshot_does_not_claim_current_observation():
    fact = accept_fact(archive_envelope(snapshot_at=JAN_2022, retrieved_at=AUG_2026), proposed_fact(observed_at=AUG_2026))
    assert fact.observed_at == JAN_2022
    assert fact.retrieved_at == AUG_2026
```

- [ ] **Step 2: Run evidence tests and confirm paraphrases can currently pass**

Run: `scripts/run_tests.sh tests/server/lead_research/test_quote_validation.py tests/server/lead_research/test_validated_evidence.py -q`

Expected: FAIL because evidence has no exact original span or archive-date validator.

- [ ] **Step 3: Add source-span fields and a single acceptance gate**

```python
def validate_span(content: str, span: EvidenceSpan) -> SpanValidation:
    exact = content[span.start:span.end]
    return SpanValidation(valid=exact == span.original and span.end <= len(content), exact=exact)

def accept_fact(envelope: EvidenceEnvelope, proposed: ResearchFact) -> ResearchFact:
    validation = validate_span(envelope.snapshot_content, proposed.span)
    if not validation.valid:
        raise EvidenceRejected("source span is not an exact snapshot substring")
    if proposed.derivation_kind == "observed" and not literal_value_present(proposed.value_en, proposed.span.original):
        raise EvidenceRejected("observed value is absent from its source span")
    if proposed.field in {"company_name", "registry_id", "domain"} and not identity_token_preserved(proposed.value_en, proposed.span.original):
        raise EvidenceRejected("identity tokens must remain unchanged")
    observed = min_non_null(proposed.observed_at, envelope.archive_snapshot_at)
    return proposed.model_copy(update={"observed_at": observed, "retrieved_at": envelope.retrieved_at})
```

All provider and agent outputs enter facts through this gate. Translated or calculated values must declare that derivation explicitly; names and identifiers remain byte-preserved apart from surrounding whitespace. Persist snapshot content/hash, canonical URL, exact original span, English value, source language, `source_observed_at`, `retrieved_at`, archive snapshot date, validation result, and field expiry. Rejected evidence creates a named campaign issue and no fact/score. Cached display translation is separate metadata.

- [ ] **Step 4: Run quote, provider, storage, and validation tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_quote_validation.py tests/server/lead_research/test_validated_evidence.py tests/server/lead_research/test_bright_data.py tests/server/lead_research/test_ted.py -q`

Expected: PASS.

- [ ] **Step 5: Commit evidence-span validation**

```bash
git add server/lead_research/quotes.py server/lead_research/models.py server/lead_research/storage.py server/lead_research/providers/bright_data.py server/lead_research/providers/ted.py tests/server/lead_research/test_quote_validation.py tests/server/lead_research/test_validated_evidence.py
git commit -m "feat(interfaze): validate exact lead evidence spans"
```

### Task 9: Add Field-Level Freshness and Negative Search Cache

**Files:**
- Modify: `server/lead_research/facts.py`
- Create: `server/lead_research/search_cache.py`
- Modify: `server/lead_research/models.py`
- Modify: `server/db.py`
- Create: `server/supabase/migrations/016_research_search_attempts.sql`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Test: `tests/server/lead_research/test_field_freshness.py`
- Create: `tests/server/lead_research/test_search_cache.py`

**Interfaces:**
- Consumes: accepted facts from Tasks 7–8.
- Produces: `ResearchQuery`, `FreshnessPolicy.expires_at(field: str, source_class: str, observed_at: float, retrieved_at: float) -> float`; `SearchAttemptRepository.lookup(scope: SearchScope, query_hash: str, at: float) -> SearchAttempt | None`; `SearchAttemptRepository.record_empty(scope: SearchScope, query_hash: str, retry_after: float) -> SearchAttempt`; `SearchAttemptRepository.record_failure(scope: SearchScope, query_hash: str, reason: str, retry_after: float) -> SearchAttempt`.

- [ ] **Step 1: Write failing mixed-freshness and negative-cache isolation tests**

```python
def test_reuse_is_decided_per_fact_not_per_bundle(fact_repo):
    fact_repo.accept("cmp_a", fact_fixture(field="founded_year", expires_at=FUTURE))
    fact_repo.accept("cmp_a", fact_fixture(field="recent_hiring", expires_at=PAST))
    reused = fact_repo.reusable("cmp_a", "org_1", {"founded_year", "recent_hiring"}, NOW)
    assert {fact.field for fact in reused} == {"founded_year"}

def test_private_query_failure_is_not_visible_to_another_tenant(search_attempts):
    search_attempts.record_failure(SearchScope(company_id="cmp_a", shareable=False), "hash_private", "timeout", NOW + HOUR)
    assert search_attempts.lookup(SearchScope(company_id="cmp_a", shareable=False), "hash_private", NOW) is not None
    assert search_attempts.lookup(SearchScope(company_id="cmp_b", shareable=False), "hash_private", NOW) is None
```

- [ ] **Step 2: Run freshness/cache tests and confirm bundle expiry and repeated failure**

Run: `scripts/run_tests.sh tests/server/lead_research/test_field_freshness.py tests/server/lead_research/test_search_cache.py tests/server/lead_research/test_evidence_reuse.py -q`

Expected: FAIL because reusable evidence expires as a source bundle and failed searches are not persisted.

- [ ] **Step 3: Implement per-field expiry and shareability-aware attempts**

```python
FIELD_TTL_DAYS = {"legal_status": 30, "recent_hiring": 30, "procurement_signal": 90, "founded_year": 3650, "website": 365}

class ResearchQuery(ApiModel):
    company_id: str
    organization_id: str
    field: str
    normalized_query_class: str
    customer_terms: list[str] = Field(default_factory=list)
    hidden_label_ids: list[str] = Field(default_factory=list)
    licensed_source_ids: list[str] = Field(default_factory=list)

class SearchScope(ApiModel):
    company_id: str | None
    shareable: bool

class SearchAttempt(ApiModel):
    id: str
    scope: SearchScope
    organization_id: str
    field: str
    query_hash: str
    source_id: str
    status: Literal["empty", "failed", "succeeded"]
    reason: str | None
    request_count: int
    attempted_at: float
    retry_after: float

def query_scope(query: ResearchQuery) -> SearchScope:
    shareable = not (query.customer_terms or query.hidden_label_ids or query.licensed_source_ids)
    return SearchScope(company_id=None if shareable else query.company_id, shareable=shareable)
```

Migration 016 stores `research_search_attempts` with query hash, company scope, status (`empty`, `failed`, `succeeded`), reason, retry-after, and timestamps, applies RLS so tenant-private attempts stay scoped, and records itself in the migration ledger. Generic public failures may be shared; queries containing customer terms, private labels, or licensed data are tenant-private. Reuse selects only fields whose `expires_at > at`.

- [ ] **Step 4: Run freshness, cache, isolation, and existing reuse tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_field_freshness.py tests/server/lead_research/test_search_cache.py tests/server/lead_research/test_evidence_reuse.py tests/server/lead_research/test_fact_isolation.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit caching semantics**

```bash
git add server/lead_research/facts.py server/lead_research/search_cache.py server/lead_research/models.py server/db.py server/postgres.py server/supabase/migrations/016_research_search_attempts.sql server/supabase/verify.sql tests/server/lead_research/test_field_freshness.py tests/server/lead_research/test_search_cache.py
git commit -m "feat(interfaze): cache research freshness per field"
```

### Task 10: Implement the Cheap Gate and Named Exclusion Accounting

**Files:**
- Modify: `server/lead_research/discovery.py`
- Modify: `server/lead_research/candidates.py`
- Modify: `server/lead_research/metrics.py`
- Modify: `server/lead_research/acquisition.py`
- Test: `tests/server/lead_research/test_cheap_gate.py`
- Modify: `tests/server/lead_research/test_incremental_selection.py`
- Modify: `tests/server/lead_research/test_term_matching.py`

**Interfaces:**
- Consumes: candidate union from Task 5, canonical/local terms from Task 6, shared facts from Task 7.
- Produces: `CheapGateDecision(passed: bool, reason: Literal["shared_relevance","corpus_term","cheap_verification","excluded_by_range","cheap_verification_no_scope_signal"], evidence_ids: list[str])`; `CheapGate.evaluate(company_id: str, candidate: CandidateRecord, scope: ResearchScope) -> CheapGateDecision`.

- [ ] **Step 1: Write failing positive-path and exclusion-count tests**

```python
@pytest.mark.parametrize("signal,reason", [
    ({"shared_relevance": True}, "shared_relevance"),
    ({"search_text": "industrial valve distributor"}, "corpus_term"),
    ({"cheap_verification": True}, "cheap_verification"),
])
def test_gate_accepts_exactly_three_allowed_signal_classes(gate, signal, reason):
    decision = gate.evaluate("cmp_a", candidate_fixture(**signal), scope_fixture(product_terms=["industrial valve"]))
    assert decision.passed is True
    assert decision.reason == reason

def test_gate_reports_named_excluded_count(runner):
    result = runner.select([candidate_fixture(search_text="unrelated bakery")], scope_fixture(product_terms=["industrial valve"]))
    assert result.counts == {"supplied": 1, "cheap_verification_no_scope_signal": 1, "passed_cheap_gate": 0}
```

- [ ] **Step 2: Run gate and term tests and confirm selection semantics differ**

Run: `scripts/run_tests.sh tests/server/lead_research/test_cheap_gate.py tests/server/lead_research/test_incremental_selection.py tests/server/lead_research/test_term_matching.py -q`

Expected: FAIL because selection has no explicit three-path gate or named exclusion result.

- [ ] **Step 3: Implement deterministic gate ordering and word-boundary matching**

```python
def explicit_range_exclusion(candidate: CandidateRecord, scope: ResearchScope) -> bool:
    ranges = set(candidate.data.get("explicit_product_ranges", []))
    return bool(ranges) and ranges.isdisjoint(scope.all_terms)

def evaluate(self, company_id: str, candidate: CandidateRecord, scope: ResearchScope) -> CheapGateDecision:
    shared = self.facts.relevance(company_id, candidate.organization_id, scope.product_terms)
    if shared:
        return CheapGateDecision(passed=True, reason="shared_relevance", evidence_ids=shared)
    candidate_text = search_text(candidate.normalized_name, candidate.data)
    if any(matches_term(term, candidate_text) for term in scope.all_terms):
        return CheapGateDecision(passed=True, reason="corpus_term", evidence_ids=[])
    if explicit_range_exclusion(candidate, scope):
        return CheapGateDecision(passed=False, reason="excluded_by_range", evidence_ids=[])
    verified = self.cheap_verifier.verify(candidate, scope.all_terms)
    return CheapGateDecision(passed=verified.matched, reason="cheap_verification" if verified.matched else "cheap_verification_no_scope_signal", evidence_ids=verified.evidence_ids)
```

Run identity resolution after the gate, keep exact counts for every exclusion reason, and meter the cheap verification request through the existing campaign request meter.

- [ ] **Step 4: Run gate, selection, request-meter, and matching tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_cheap_gate.py tests/server/lead_research/test_incremental_selection.py tests/server/lead_research/test_term_matching.py tests/server/lead_research/test_request_metering.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the cheap-gate slice**

```bash
git add server/lead_research/discovery.py server/lead_research/candidates.py server/lead_research/metrics.py server/lead_research/acquisition.py tests/server/lead_research/test_cheap_gate.py tests/server/lead_research/test_incremental_selection.py tests/server/lead_research/test_term_matching.py
git commit -m "feat(interfaze): gate lead candidates by evidence"
```

### Task 11: Plan Criterion-Aware Research Gaps Once per Company

**Files:**
- Create: `server/lead_research/gaps.py`
- Modify: `server/lead_research/enrichment.py`
- Modify: `server/lead_research/models.py`
- Modify: `skills/sales/lead-research/references/feature-playbooks.yaml`
- Test: `tests/server/lead_research/test_gap_planner.py`
- Modify: `tests/server/lead_research/test_enrichment_pass.py`

**Interfaces:**
- Consumes: reusable fields from Task 9, `ScoringProfile.weights`, enrichment profile, structured provider capabilities, and sector playbook requirements.
- Produces: `ResearchGapPlan`; `GapPlanner.plan(profile_version: CompanyProfileVersion, campaign: CampaignConfig, candidate: LeadCandidate, reusable_facts: list[StoredFact], capabilities: list[SourceCapability]) -> ResearchGapPlan`.

- [ ] **Step 1: Write failing coverage and batching tests**

```python
def test_plan_covers_every_nonzero_weight_even_when_no_structured_source_emits_it(planner):
    plan = planner.plan(profile_version(), campaign(weights={"product_sector_fit": 40, "buyer_channel_fit": 30, "commercial_scale": 30}), candidate(), reusable_facts=[], capabilities=[capability(fields={"product_sector_fit"})])
    assert {gap.dimension for gap in plan.gaps} == {"product_sector_fit", "buyer_channel_fit", "commercial_scale"}
    assert plan.for_dimension("product_sector_fit").route == "structured"
    assert plan.for_dimension("buyer_channel_fit").route == "agentic"
    assert plan.for_dimension("commercial_scale").route == "agentic"

def test_plan_batches_fields_that_can_be_read_from_one_page(planner):
    plan = planner.plan(profile_version(), campaign(), candidate(domain="acme.test"), reusable_facts=[], capabilities=[])
    official = [batch for batch in plan.batches if batch.source_hint == "official_site"]
    assert set(official[0].fields) >= {"product_range", "company_size", "buyer_role"}
```

- [ ] **Step 2: Run gap/enrichment tests and confirm unsupported dimensions disappear**

Run: `scripts/run_tests.sh tests/server/lead_research/test_gap_planner.py tests/server/lead_research/test_enrichment_pass.py -q`

Expected: FAIL because current planning follows provider-emitted fields and re-runs sector terms rather than filling criteria.

- [ ] **Step 3: Implement a deterministic gap plan with required/useful fields**

```python
class ResearchGap(ApiModel):
    dimension: str
    weight: int
    fields: list[str]
    route: Literal["reuse", "structured", "agentic"]
    required: bool

class ResearchGapPlan(ApiModel):
    organization_id: str
    gaps: list[ResearchGap]
    batches: list[ResearchBatch]

def weighted_dimensions(weights: ScoringWeights) -> list[tuple[str, int]]:
    return [(name, int(value)) for name, value in weights.model_dump().items() if int(value) > 0]
```

For each of the fixed dimensions (`product_sector_fit`, `buyer_channel_fit`, `buying_intent`, `market_coverage`, `commercial_scale`, `trade_activity`, `contactability`) map its accepted fact fields, mark playbook `required` and `useful` fields, subtract fresh reusable facts and fresh negative-cache attempts, route remaining fields to structured sources when possible, and route every remainder to agentic research. Batch fields by page/source hint so a page is fetched once and stored wide.

- [ ] **Step 4: Run gap, enrichment, and scoring-attainability tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_gap_planner.py tests/server/lead_research/test_enrichment_pass.py tests/server/lead_research/test_fit_scoring.py -q`

Expected: PASS.

- [ ] **Step 5: Commit criterion-aware gap planning**

```bash
git add server/lead_research/gaps.py server/lead_research/enrichment.py server/lead_research/models.py skills/sales/lead-research/references/feature-playbooks.yaml tests/server/lead_research/test_gap_planner.py tests/server/lead_research/test_enrichment_pass.py
git commit -m "feat(interfaze): plan weighted lead research gaps"
```

### Task 12: Execute Agentic Gaps Through Durable Agent Runs

**Files:**
- Create: `server/lead_research/agentic.py`
- Modify: `server/lead_research/models.py`
- Modify: `server/run_types.py`
- Modify: `server/agent_service.py`
- Modify: `server/lead_research/enrichment.py`
- Modify: `skills/sales/lead-research/SKILL.md`
- Test: `tests/server/lead_research/test_agentic_research.py`
- Modify: `tests/server/test_run_harness.py`

**Interfaces:**
- Consumes: `ResearchGapPlan` from Task 11; exact evidence acceptance from Task 8; existing `AgentRunService` state machine.
- Produces: `AgentRunRef`; `AgenticResearchRequest`; `AgenticResearchResult`; `AgenticResearchSource.research_gaps(request: AgenticResearchRequest) -> AgenticResearchResult`; `AgenticResearchService.enqueue(company_id: str, campaign_id: str, candidate: LeadCandidate, plan: ResearchGapPlan, decision_model: str, extractor_model: str | None) -> AgentRunRef`; `enqueue_if_needed(company_id: str, campaign_id: str, candidate: LeadCandidate, plan: ResearchGapPlan) -> AgentRunRef | None`; `accept_result(company_id: str, run_id: str) -> list[StoredFact]`.

- [ ] **Step 1: Write failing durable-run, model-routing, and incidental-fact tests**

```python
def test_agentic_gap_run_is_durable_and_uses_configured_models(agentic, runs):
    ref = agentic.enqueue("cmp_a", "rc_1", candidate(), gap_plan(), decision_model="model-decision", extractor_model="model-cheap")
    row = runs.get("cmp_a", ref.run_id)
    assert row["run_type"] == "lead_research_gap"
    assert row["payload"]["decision_model"] == "model-decision"
    assert row["payload"]["extractor_model"] == "model-cheap"

def test_accepted_page_stores_incidental_schema_known_facts(agentic, completed_gap_run):
    facts = agentic.accept_result("cmp_a", completed_gap_run.id)
    assert {fact.field for fact in facts} >= {"buyer_role", "company_size", "website"}

def test_terminal_veto_skips_agentic_run(agentic, terminal_candidate):
    assert agentic.enqueue_if_needed("cmp_a", "rc_1", terminal_candidate, gap_plan()) is None

def test_low_current_fit_does_not_prune_missing_weighted_research(agentic, low_fit_candidate):
    ref = agentic.enqueue_if_needed("cmp_a", "rc_1", low_fit_candidate, gap_plan())
    assert ref.run_id.startswith("run_")

def test_page_request_time_and_token_limits_stop_new_work(agentic, exhausted_budget):
    result = agentic.execute(request_fixture(), exhausted_budget)
    assert result.stop_reason in {"page_limit", "request_limit", "time_limit", "token_limit"}
    assert result.requests_started == 0
```

- [ ] **Step 2: Run agentic and agent-run tests and confirm no campaign gap run exists**

Run: `scripts/run_tests.sh tests/server/lead_research/test_agentic_research.py tests/server/test_run_harness.py -q`

Expected: FAIL because `lead_research_gap` and its validated result contract are absent.

- [ ] **Step 3: Implement durable agentic requests and strict result acceptance**

```python
class AgentRunRef(ApiModel):
    run_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]

class AgenticResearchRequest(ApiModel):
    campaign_id: str
    organization_id: str
    company_name: str
    canonical_domain: str | None
    batches: list[ResearchBatch]
    market_terms: dict[str, list[str]]
    decision_model: str
    extractor_model: str | None

class AgenticResearchResult(ApiModel):
    pages: list[AcceptedResearchPage]
    facts: list[ProposedFact]
    unresolved_fields: list[str]

class AgenticResearchSource(Protocol):
    definition: DatasetDefinition
    def research_gaps(self, request: AgenticResearchRequest) -> AgenticResearchResult:
        raise NotImplementedError
```

Register `lead_research_gap` as read-only. Preserve `EnrichmentProfile.model_profile` as the decision model and add optional `extractor_model_profile`; both live in the campaign's tenant configuration snapshot, never environment variables. The skill prompt requires original spans, canonical English, source language, URLs, observation/archive dates, and all schema-known facts found on accepted pages. The cheaper extractor handles clear extraction only; ambiguity or source disagreement sets `requires_decision_model=true`. Application code validates spans, identity, dates, expiry, visibility, and promotion before persistence. Stop on terminal veto, cancellation, required coverage, source exhaustion, configured budget, or top band with no required gap; never stop solely because current fit is low.

- [ ] **Step 4: Run agentic, quote, fact, and agent lifecycle tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_agentic_research.py tests/server/lead_research/test_quote_validation.py tests/server/lead_research/test_fact_pool.py tests/server/test_run_harness.py -q`

Expected: PASS.

- [ ] **Step 5: Commit agentic gap execution**

```bash
git add server/lead_research/agentic.py server/lead_research/models.py server/run_types.py server/agent_service.py server/lead_research/enrichment.py skills/sales/lead-research/SKILL.md tests/server/lead_research/test_agentic_research.py tests/server/test_run_harness.py
git commit -m "feat(interfaze): research lead gaps with durable agents"
```

### Task 13: Integrate Discovery, Reuse, Structured Research, and Agentic Gaps

**Files:**
- Modify: `server/lead_research/service.py`
- Modify: `server/lead_research/acquisition.py`
- Modify: `server/lead_research/enrichment.py`
- Modify: `server/lead_research/metrics.py`
- Modify: `server/routes/research_campaigns.py`
- Test: `tests/server/lead_research/test_research_pipeline.py`
- Modify: `tests/server/lead_research/test_verification_concurrency.py`
- Modify: `tests/server/lead_research/test_campaign_dispatch.py`

**Interfaces:**
- Consumes: discovery supply (Task 5), cheap gate (Task 10), facts/cache (Tasks 7–9), gap plan (Task 11), and agentic service (Task 12).
- Produces: one resumable campaign state machine with persisted per-candidate stage, bounded concurrency, cancellation, partial progress, and metrics.

- [ ] **Step 1: Write failing pipeline-order and cancellation tests**

```python
def test_pipeline_reuses_before_structured_and_agentic_research(pipeline, spies):
    pipeline.process("cmp_a", campaign(), candidate())
    assert spies.calls == ["identity", "eligibility", "reuse", "structured_missing", "agentic_remaining", "score"]

def test_cancellation_stops_new_requests_but_persists_completed_facts(pipeline, cancel_after_first, fact_repo):
    result = pipeline.run("cmp_a", campaign_with_three_candidates(), cancel=cancel_after_first)
    assert result.status == "cancelled"
    assert result.completed_candidates == 1
    assert fact_repo.count_for_campaign("cmp_a", result.campaign_id) > 0
    assert result.requests_started < 3
```

- [ ] **Step 2: Run integration/concurrency tests and confirm current enrichment order differs**

Run: `scripts/run_tests.sh tests/server/lead_research/test_research_pipeline.py tests/server/lead_research/test_verification_concurrency.py tests/server/lead_research/test_campaign_dispatch.py -q`

Expected: FAIL because campaign enrichment does not run criterion-aware reuse/structured/agentic phases.

- [ ] **Step 3: Replace the campaign loop with explicit persisted stages**

```python
STAGES = ("supplied", "gated", "identified", "eligible", "reused", "structured", "agentic", "scored", "materialized")

def process_candidate(self, context: CampaignContext, candidate: CandidateRecord) -> CandidateOutcome:
    gate = self.cheap_gate.evaluate(context.company_id, candidate, context.scope)
    if not gate.passed:
        return self.reject(candidate, "no_relevance")
    resolved = self.identity.resolve(candidate)
    eligibility = self.eligibility.evaluate(resolved, context.scope)
    if not eligibility.eligible:
        return self.reject(candidate, eligibility.reason)
    reusable = self.facts.reusable(context.company_id, resolved.id, context.required_fields, now())
    plan = self.gaps.plan(context.profile_version, context.config, resolved, reusable, self.registry.capabilities())
    structured = self.enrichment.research_structured(context, resolved, plan)
    agentic = self.enrichment.research_agentic(context, resolved, plan.remaining_after(structured))
    return self.score_and_materialize(context, resolved, reusable + structured + agentic)
```

Persist stage checkpoints and request counts before/after external calls. Respect `EnrichmentProfile.enabled`, model profile, completeness target, page/time/token limits, source policy, global/tenant concurrency, and cancellation. Retry resumes incomplete stages and never repeats accepted fresh work.

- [ ] **Step 4: Run pipeline, dispatch, concurrency, metrics, and vertical-slice tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_research_pipeline.py tests/server/lead_research/test_verification_concurrency.py tests/server/lead_research/test_request_metering.py tests/server/lead_research/test_campaign_dispatch.py tests/server/lead_research/test_vertical_slice.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the integrated research pipeline**

```bash
git add server/lead_research/service.py server/lead_research/acquisition.py server/lead_research/enrichment.py server/lead_research/metrics.py server/routes/research_campaigns.py tests/server/lead_research/test_research_pipeline.py tests/server/lead_research/test_verification_concurrency.py tests/server/lead_research/test_campaign_dispatch.py
git commit -m "feat(interfaze): integrate criterion-aware lead research"
```

### Task 14: Make Fit, Unknown Weight, and Confidence Honest

**Files:**
- Modify: `server/lead_research/models.py`
- Modify: `server/lead_research/scoring.py`
- Modify: `server/lead_research/verdicts.py`
- Modify: `server/lead_research/service.py`
- Modify: `tests/server/lead_research/test_fit_scoring.py`
- Create: `tests/server/lead_research/test_unknown_weight.py`
- Modify: `tests/server/lead_research/test_verdicts.py`

**Interfaces:**
- Consumes: accepted facts from Task 13 and fixed seven-dimension `ScoringWeights`.
- Produces: backward-compatible `LeadScore(fit_score, evidence_confidence, priority_band, known_weight, unknown_weight, unknown_dimensions, not_applicable_dimensions, dimensions, dimension_evidence_ids, confidence_factors)`; extended `score_lead(candidate, claims: Iterable[Claim], profile: ScoringProfile, attainable: set[str] | None = None, at: float | None = None, not_applicable: set[str] | None = None) -> LeadScore`.

- [ ] **Step 1: Write failing denominator, monotonicity, and confidence-separation tests**

```python
def test_unknown_weight_is_reported_and_not_silently_removed():
    score = score_lead(candidate_fixture(), [validated_claim("product_sector_fit", 1.0)], scoring_profile(weights(product_sector_fit=40, buyer_channel_fit=30, trade_activity=30)), at=NOW)
    assert score.fit_score == 100.0
    assert score.known_weight == 40
    assert score.unknown_weight == 60
    assert score.unknown_dimensions == {"buyer_channel_fit": 30, "trade_activity": 30}

def test_more_support_cannot_reduce_a_dimension_score():
    one = derive_dimension_scores([validated_claim("product_sector_fit", 0.7)])["product_sector_fit"]
    two = derive_dimension_scores([validated_claim("product_sector_fit", 0.7), validated_claim("product_sector_fit", 0.8)])["product_sector_fit"]
    assert two >= one

def test_confidence_falls_with_stale_single_source_evidence_without_changing_fit():
    profile = scoring_profile(weights(product_sector_fit=100))
    fresh = score_lead(candidate_fixture(), [claim_fixture(field="product_sector_fit", value=0.8, authority=1, observed_at=NOW)], profile, at=NOW)
    stale = score_lead(candidate_fixture(), [claim_fixture(field="product_sector_fit", value=0.8, authority=1, observed_at=OLD)], profile, at=NOW)
    assert stale.fit_score == fresh.fit_score
    assert stale.evidence_confidence < fresh.evidence_confidence
```

- [ ] **Step 2: Run score/verdict tests and confirm unknown dimensions are omitted**

Run: `scripts/run_tests.sh tests/server/lead_research/test_fit_scoring.py tests/server/lead_research/test_unknown_weight.py tests/server/lead_research/test_verdicts.py -q`

Expected: FAIL because unsupported dimensions shrink the attainable denominator and unknown weight is not first-class.

- [ ] **Step 3: Implement the fixed scoring contract**

```python
class LeadScore(ApiModel):
    fit_score: int = Field(ge=0, le=100)
    evidence_confidence: float = Field(ge=0, le=1)
    priority_band: Literal["A", "B", "C", "Rejected"]
    known_weight: int
    unknown_weight: int
    unknown_dimensions: dict[str, int]
    not_applicable_dimensions: dict[str, int]
    dimensions: dict[str, float | None]
    dimension_evidence_ids: dict[str, list[str]]
    confidence_factors: dict[str, float]

def _weighted_fit(dimensions: dict[str, float | None], weights: dict[str, int], not_applicable: set[str]) -> tuple[int, int, dict[str, int], dict[str, int]]:
    na = {name: weight for name, weight in weights.items() if weight > 0 and name in not_applicable}
    unknown = {name: weight for name, weight in weights.items() if weight > 0 and name not in not_applicable and dimensions.get(name) is None}
    known = sum(weight for name, weight in weights.items() if weight > 0 and name not in not_applicable and dimensions.get(name) is not None)
    numerator = sum(dimensions[name] * weight for name, weight in weights.items() if weight > 0 and name not in not_applicable and dimensions.get(name) is not None)
    return (int(round(100 * numerator / known)) if known else 0, known, unknown, na)
```

Keep the validated-evidence anchor; combine corroborating support monotonically; cap unvalidated-only dimensions below the validated threshold. Unknown and explicitly not-applicable dimensions remain distinct and neither becomes zero. Confidence combines weighted applicable coverage, authority, corroboration, freshness, conflict penalties, and estimate penalties. Verdict thresholds consume fit and confidence separately and preserve terminal eligibility rejects.

- [ ] **Step 4: Run all scoring, evidence, and verdict tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_fit_scoring.py tests/server/lead_research/test_unknown_weight.py tests/server/lead_research/test_validated_evidence.py tests/server/lead_research/test_verdicts.py tests/server/lead_research/test_verdict_authority.py -q`

Expected: PASS.

- [ ] **Step 5: Commit honest scoring**

```bash
git add server/lead_research/models.py server/lead_research/scoring.py server/lead_research/verdicts.py server/lead_research/service.py tests/server/lead_research/test_fit_scoring.py tests/server/lead_research/test_unknown_weight.py tests/server/lead_research/test_verdicts.py
git commit -m "fix(interfaze): expose lead score uncertainty"
```

### Task 15: Snapshot Results and Materialize Only Actionable Leads

**Files:**
- Modify: `server/lead_research/service.py`
- Modify: `server/lead_research/models.py`
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/db.py`
- Create: `server/supabase/migrations/017_research_result_contract.sql`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Test: `tests/server/lead_research/test_result_snapshots.py`
- Modify: `tests/server/lead_research/test_vertical_slice.py`

**Interfaces:**
- Consumes: `LeadScore` from Task 14 and campaign profile/scope snapshots from Task 3.
- Produces: immutable `ResearchResultData` with score/evidence/profile/playbook/source-policy versions; append-only `research_score_snapshots`; `LeadResearchService.persist_outcome(context: CampaignContext, organization: ResolvedIdentity, score: LeadScore, verdict: Verdict, facts: list[StoredFact]) -> PersistedOutcome`; one tenant lead per resolved organization; campaign-specific result rows for all outcomes.

- [ ] **Step 1: Write failing lead-materialization and immutable-result tests**

```python
@pytest.mark.parametrize("verdict_kind,reasons,lead_expected", [("strong_fit", [], True), ("review", ["missing_second_source"], True), ("reject", ["below_threshold"], False), ("reject", ["ineligible_target_presence"], False), ("reject", ["lifecycle_status_closed"], False)])
def test_only_actionable_verdicts_materialize_leads(service, verdict_kind, reasons, lead_expected):
    outcome = service.persist_outcome(context(), resolved_org(), score_fixture(), verdict_fixture(kind=verdict_kind, reasons=reasons), facts())
    assert (outcome.lead_id is not None) is lead_expected
    assert outcome.result_id is not None

def test_repeat_campaign_reuses_tenant_lead_but_keeps_distinct_result_snapshot(service):
    first = service.persist_outcome(context(campaign_id="rc_1"), resolved_org(), score_fixture(), verdict_fixture(kind="strong_fit"), facts())
    second = service.persist_outcome(context(campaign_id="rc_2"), resolved_org(), score_fixture(), verdict_fixture(kind="review"), facts())
    assert first.lead_id == second.lead_id
    assert first.result_id != second.result_id
    assert first.snapshot["campaign_id"] == "rc_1"
```

- [ ] **Step 2: Run snapshot/vertical tests and confirm current materialization behavior**

Run: `scripts/run_tests.sh tests/server/lead_research/test_result_snapshots.py tests/server/lead_research/test_vertical_slice.py -q`

Expected: FAIL because score/profile/playbook/source versions and all non-lead outcomes are not frozen in one immutable result contract.

- [ ] **Step 3: Persist complete result snapshots and enforce lead uniqueness**

```python
ACTIONABLE_VERDICTS = frozenset({"strong_fit", "review"})

def result_snapshot(context: CampaignContext, organization: ResolvedIdentity, score: LeadScore, fact_ids: list[str], verdict: Verdict) -> dict:
    verdict_data = {"kind": verdict.kind, "reasons": verdict.reasons, "missing_evidence": verdict.missing_evidence, "conflicting_claims": verdict.conflicting_claims}
    return {"campaign_id": context.campaign_id, "profile_version_id": context.profile_version.id, "scope": context.scope.model_dump(mode="json"), "playbook_versions": context.profile_version.profile.playbook_versions, "source_policy": context.config.enrichment.source_policy, "organization": organization.model_dump(mode="json"), "score": score.model_dump(mode="json"), "fact_ids": fact_ids, "verdict": verdict_data}
```

Always insert a campaign result and an append-only score snapshot containing campaign/result/profile version, weights, dimensions, unknown/not-applicable fields, fit, confidence, priority band, and fact/evidence IDs. Upsert a lead only for actionable verdicts using unique `(company_id, resolved_organization_id)` and link the result to it. Migration 017 adds the identity uniqueness/indexes, result snapshot fields, and `research_score_snapshots` with tenant RLS; it records itself and is required at startup.

- [ ] **Step 4: Run snapshot, band, vertical, and parity tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_result_snapshots.py tests/server/lead_research/test_band_outcomes.py tests/server/lead_research/test_vertical_slice.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit immutable outcomes**

```bash
git add server/lead_research/service.py server/lead_research/models.py server/routes/research_campaigns.py server/db.py server/postgres.py server/supabase/migrations/017_research_result_contract.sql server/supabase/verify.sql tests/server/lead_research/test_result_snapshots.py tests/server/lead_research/test_vertical_slice.py
git commit -m "feat(interfaze): snapshot lead research outcomes"
```

### Task 16: Add Hidden Label History and Correction Propagation

**Files:**
- Create: `server/lead_research/labels.py`
- Modify: `server/lead_research/facts.py`
- Modify: `server/lead_research/service.py`
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/routes/admin.py`
- Modify: `server/db.py`
- Create: `server/supabase/migrations/018_research_labels_corrections.sql`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Test: `tests/server/lead_research/test_labels_and_corrections.py`
- Modify: `tests/server/lead_research/test_band_outcomes.py`

**Interfaces:**
- Consumes: fact consumers from Task 7 and result snapshots from Task 15.
- Produces: `LabelRepository.assign(company_id: str, result_id: str, label_id: str, value: str, scope: str, source: Literal["system","admin","outcome_analysis"], actor_id: str, reason: str, profile_version_id: str) -> LabelAssignment`; `LabelRepository.history(company_id: str, result_id: str) -> list[LabelAssignment]`; `FactRepository.correct(fact_id: str, corrected_value_en: Any, actor_id: str, reason: str, apply: bool) -> CorrectionImpact`; admin endpoints for fact usage, correction preview/apply, label history, and conversion by band plus label.

- [ ] **Step 1: Write failing admin-only label and correction-impact tests**

```python
def test_customer_result_never_serializes_hidden_labels(client, user_headers, seeded_labeled_result):
    result = client.get(f"/research-campaigns/{seeded_labeled_result.campaign_id}/results", headers=user_headers).json()["items"][0]
    assert "hidden_labels" not in result
    assert "hidden_label_ids" not in json.dumps(result)

def test_correction_reports_and_recomputes_consumers(fact_repo, seeded_consumers):
    original_snapshot = seeded_consumers.db.one("SELECT snapshot_json FROM research_score_snapshots WHERE result_id='rr_1'")["snapshot_json"]
    impact = fact_repo.correct("sf_1", corrected_value_en="51-200", actor_id="adm_1", reason="registry correction", apply=False)
    assert set(impact.result_ids) == {"rr_1", "rr_2"}
    applied = fact_repo.correct("sf_1", corrected_value_en="51-200", actor_id="adm_1", reason="registry correction", apply=True)
    assert applied.recomputed_result_ids == ["rr_1", "rr_2"]
    assert seeded_consumers.db.one("SELECT snapshot_json FROM research_score_snapshots WHERE result_id='rr_1'")["snapshot_json"] == original_snapshot
```

- [ ] **Step 2: Run label/correction/outcome tests and confirm audit APIs are absent**

Run: `scripts/run_tests.sh tests/server/lead_research/test_labels_and_corrections.py tests/server/lead_research/test_band_outcomes.py -q`

Expected: FAIL because label assignment history, fact consumers, and correction recomputation are not exposed.

- [ ] **Step 3: Implement append-only label and correction audit flows**

```python
class LabelAssignment(ApiModel):
    id: str
    company_id: str
    result_id: str
    label_id: str
    value: str
    scope: str
    source: Literal["system", "admin", "outcome_analysis"]
    actor_id: str
    reason: str
    profile_version_id: str
    effective_from: float
    effective_until: float | None

class CorrectionImpact(ApiModel):
    fact_id: str
    result_ids: list[str]
    lead_ids: list[str]
    recomputed_result_ids: list[str] = Field(default_factory=list)

def correct(self, fact_id: str, corrected_value_en: Any, actor_id: str, reason: str, apply: bool) -> CorrectionImpact:
    impact = self.consumers(fact_id)
    if not apply:
        return impact
    corrected = self.supersede(fact_id, corrected_value_en, actor_id, reason)
    recomputed = self.recompute_consumers(corrected, impact.result_ids)
    return impact.model_copy(update={"recomputed_result_ids": recomputed})
```

Migration 018 creates append-only label assignments and correction records with tenant/service-role RLS, records itself, and is required at startup. Label assignments retain assigned/removed timestamps, provenance, and actor. Customer serializers remove label IDs and names. Admin analytics group conversion by priority band and label assignment active at the result timestamp, not by today's mutable label state.

- [ ] **Step 4: Run label, correction, outcome, serializer, and admin tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_labels_and_corrections.py tests/server/lead_research/test_band_outcomes.py tests/server/test_research_webui.py tests/server/test_api_mvp.py tests/server/test_postgres_parity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit labels and corrections**

```bash
git add server/lead_research/labels.py server/lead_research/facts.py server/lead_research/service.py server/routes/research_campaigns.py server/routes/admin.py server/db.py server/postgres.py server/supabase/migrations/018_research_labels_corrections.sql server/supabase/verify.sql tests/server/lead_research/test_labels_and_corrections.py tests/server/lead_research/test_band_outcomes.py
git commit -m "feat(interfaze): audit lead labels and fact corrections"
```

### Task 17: Localize Evidence-Rich Customer Results

**Files:**
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/pages/research-detail.js`
- Modify: `server/webui/js/pages/research-evidence.js`
- Modify: `server/webui/js/pages/research-results.js`
- Modify: `server/webui/css/app.css`
- Modify: `tests/server/test_research_webui.py`
- Modify: `tests/server/webui/test_research_results.mjs`
- Modify: `tests/server/webui/test_research_scoring.mjs`

**Interfaces:**
- Consumes: immutable result snapshots from Task 15, translations from Task 6, exact evidence spans from Task 8.
- Produces: paginated/streamed customer result payload with fit, confidence, band, known/unknown weight, reasons, evidence, canonical/original values, language, observation/retrieval dates, and no hidden labels.

- [ ] **Step 1: Write failing result-card, evidence-drawer, and locale tests**

```javascript
test('result card separates fit from confidence and explains unknown weight', () => {
  const html = renderResearchResult({ company_name: 'Acme', fit_score: 82, evidence_confidence: 0.61, priority_band: 'A', verdict: 'strong_fit', known_weight: 70, unknown_weight: 30, unknown_dimensions: { commercial_scale: 30 }, reasons: ['Strong product match'] });
  assert.match(html, /Fit 82/);
  assert.match(html, /Confidence 61/);
  assert.match(html, /Unknown 30%/);
  assert.match(html, /Recent growth/);
});

test('evidence drawer preserves source text beside canonical English', () => {
  const html = renderEvidence({ value_en: 'distributor', original_text: 'Distribütör', source_language: 'tr', observed_at: '2026-07-01', retrieved_at: '2026-08-24', url: 'https://example.test' }, 'en');
  assert.match(html, /distributor/);
  assert.match(html, /Distribütör/);
  assert.match(html, /Observed/);
});
```

- [ ] **Step 2: Run research UI tests and confirm missing uncertainty/evidence display**

Run: `node --test tests/server/webui/test_research_results.mjs tests/server/webui/test_research_scoring.mjs`

Run: `scripts/run_tests.sh tests/server/test_research_webui.py -q`

Expected: FAIL because current cards do not expose the full uncertainty and bilingual evidence contract.

- [ ] **Step 3: Extend serializers and render localized progressive results**

```javascript
export function resultViewModel(result, locale = 'en') {
  return Object.assign({}, result, {
    labels: {
      fit: locale === 'tr' ? 'Uyum' : 'Fit',
      confidence: locale === 'tr' ? 'Kanıt güveni' : 'Confidence',
      unknown: locale === 'tr' ? 'Bilinmeyen ağırlık' : 'Unknown weight',
    },
    evidence: (result.evidence || []).map(item => Object.assign({}, item, { display_value: locale === 'tr' ? item.display_tr : item.value_en })),
  });
}
```

Stream/poll completed result rows while the campaign runs, preserve existing pagination, display named exclusions, eligibility/rejection reasons, conflicted criteria, partial status, and a contact-discovery action for selected actionable leads. Provide an accessible evidence drawer with criterion/weight, source link, original span, canonical English, source language, observation/archive/retrieval dates, and validation state. Turkish/English fixed labels use dictionaries; no customer response includes hidden labels or cross-tenant reuse metadata.

- [ ] **Step 4: Run route and all research UI module tests**

Run: `scripts/run_tests.sh tests/server/test_research_webui.py tests/server/lead_research/test_result_snapshots.py -q`

Run: `node --test tests/server/webui/test_research_brief.mjs tests/server/webui/test_research_results.mjs tests/server/webui/test_research_scoring.mjs`

Expected: PASS.

- [ ] **Step 5: Commit customer lead display**

```bash
git add server/routes/research_campaigns.py server/webui/js/api.js server/webui/js/pages/research-detail.js server/webui/js/pages/research-evidence.js server/webui/js/pages/research-results.js server/webui/css/app.css tests/server/test_research_webui.py tests/server/webui/test_research_results.mjs tests/server/webui/test_research_scoring.mjs
git commit -m "feat(interfaze): explain lead fit and evidence"
```

### Task 18: Add Admin Research Quality and Cost Oversight

**Files:**
- Modify: `server/routes/admin.py`
- Modify: `server/routes/research_campaigns.py`
- Modify: `server/webui/js/pages/admin.js`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/css/app.css`
- Create: `tests/server/test_admin_research_quality.py`
- Create: `tests/server/webui/test_admin_research_quality.mjs`

**Interfaces:**
- Consumes: profile/label/fact histories, result snapshots, request meter, search cache, source metrics, and correction impact from earlier tasks.
- Produces: `GET /admin/research/quality`, `GET /admin/research/facts/{fact_id}/impact`, correction preview/apply, and admin UI quality warnings/metrics.

- [ ] **Step 1: Write failing quality-warning and admin-authorization tests**

```python
def test_quality_endpoint_is_admin_only_and_reports_actionable_warnings(client, user_headers, admin_headers, quality_seed):
    assert client.get("/admin/research/quality", headers=user_headers).status_code == 403
    payload = client.get("/admin/research/quality", headers=admin_headers).json()
    assert payload["warnings"] == [{"code": "thin_profile", "company_id": "cmp_a"}, {"code": "high_fact_reuse", "fact_id": "sf_1"}, {"code": "source_change", "source_id": "bright-data"}]
    assert payload["costs"]["requests"] >= payload["costs"]["cache_hits"]
```

```javascript
test('admin quality view shows exclusions, reuse, source change, and cost', () => {
  const html = renderResearchQuality(fixture);
  for (const label of ['Excluded candidates', 'Fact reuse', 'Source changes', 'Requests', 'Tokens', 'Cost']) assert.match(html, new RegExp(label, 'i'));
});
```

- [ ] **Step 2: Run admin API/UI tests and confirm oversight surface is incomplete**

Run: `scripts/run_tests.sh tests/server/test_admin_research_quality.py -q`

Run: `node --test tests/server/webui/test_admin_research_quality.mjs`

Expected: FAIL because combined profile/fact/source/cost quality reporting is absent.

- [ ] **Step 3: Implement admin-only oversight and correction controls**

```python
class FactCorrection(BaseModel):
    value_en: Any
    reason: str = Field(min_length=3, max_length=500)
    apply: bool = False

@router.get("/research/quality")
def research_quality(request: Request, principal: Principal = Depends(require_admin)):
    return ResearchQualityService(request.app.state.db).report()

@router.post("/research/facts/{fact_id}/corrections")
def correct_fact(fact_id: str, body: FactCorrection, request: Request, principal: Principal = Depends(require_admin)):
    return FactRepository(request.app.state.db).correct(fact_id, body.value_en, principal.id, body.reason, body.apply)
```

Report profile version/confirmation history, hidden-label history, candidates by source/visibility, collapsed-row counts, every named exclusion/rejection, shared-fact usage and correction impact, thin-evidence/high-reuse/sharp-score-change/source-change warnings, per-source requests/retries/fresh-cache hits/negative-cache hits/failures, agentic companies/pages/tokens/elapsed time/budget stops, contacts derived, cancellations, provider errors, and conversion by band plus label. Admin English is default and Turkish is selectable; review remains advisory and never gates campaign execution.

- [ ] **Step 4: Run admin API/UI, tenant-isolation, and outcomes tests**

Run: `scripts/run_tests.sh tests/server/test_admin_research_quality.py tests/server/lead_research/test_fact_isolation.py tests/server/lead_research/test_band_outcomes.py -q`

Run: `node --test tests/server/webui/test_admin_research_quality.mjs`

Expected: PASS.

- [ ] **Step 5: Commit admin oversight**

```bash
git add server/routes/admin.py server/routes/research_campaigns.py server/webui/js/pages/admin.js server/webui/js/api.js server/webui/css/app.css tests/server/test_admin_research_quality.py tests/server/webui/test_admin_research_quality.mjs
git commit -m "feat(interfaze): expose lead research quality controls"
```

### Task 19: Mechanically Tier and Rank Contacts

**Files:**
- Create: `server/lead_research/contacts.py`
- Modify: `server/routes/sales_intelligence.py`
- Modify: `server/agent_service.py`
- Modify: `server/quality.py`
- Modify: `server/db.py`
- Create: `server/supabase/migrations/019_contact_verification_tiers.sql`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Test: `tests/server/test_contact_verification_tiers.py`
- Modify: `tests/server/test_api_mvp.py`

**Interfaces:**
- Consumes: discovered contact records and immutable source evidence.
- Produces: `ContactVerification(tier, contact_kind, method, evidence_ids, checked_at)`; `verify_contact(contact: dict, evidence: list[dict]) -> ContactVerification`; `rank_contacts(contacts: list[dict]) -> list[dict]`.

- [ ] **Step 1: Write failing green/yellow/red/generic tests**

```python
@pytest.mark.parametrize("contact,evidence,tier,kind", [
    ({"email": "ayse@acme.test", "name": "Ayşe", "title": "Purchasing Manager"}, [official_staff_evidence()], "green", "person"),
    ({"email": "ayse@acme.test", "name": "Ayşe"}, [tenant_supplied_evidence()], "green", "person"),
    ({"email": "ayse@acme.test", "name": "Ayşe"}, [pattern_and_mail_domain_evidence()], "yellow", "person"),
    ({"email": "ayse@gmail.com", "name": "Ayşe"}, [], "red", "person"),
    ({"email": "info@acme.test"}, [official_contact_page_evidence()], "green", "generic"),
])
def test_contact_tiers_are_mechanical(contact, evidence, tier, kind):
    result = verify_contact(contact, evidence)
    assert (result.tier, result.contact_kind) == (tier, kind)

def test_generic_address_ranks_after_people_even_when_green():
    ranked = rank_contacts([contact(tier="green", kind="generic"), contact(tier="yellow", kind="person"), contact(tier="green", kind="person")])
    assert [(row["verification_tier"], row["contact_kind"]) for row in ranked] == [("green", "person"), ("yellow", "person"), ("green", "generic")]
```

- [ ] **Step 2: Run contact and sales-intelligence tests and confirm syntax-only verification**

Run: `scripts/run_tests.sh tests/server/test_contact_verification_tiers.py tests/server/test_api_mvp.py -q`

Expected: FAIL because `/contacts/{id}/verify` currently marks syntax-valid records verified without evidence tiers.

- [ ] **Step 3: Implement mechanical tiers, persistence, and rank order**

```python
class ContactVerification(ApiModel):
    tier: Literal["green", "yellow", "red"]
    contact_kind: Literal["person", "generic"]
    method: str
    evidence_ids: list[str]
    checked_at: float

def outreach_rank(contact: dict) -> tuple[int, int, int]:
    tier_rank = {"green": 0, "yellow": 1, "red": 2}[contact["verification_tier"]]
    generic_rank = 1 if contact["contact_kind"] == "generic" else 0
    role_rank = 0 if contact.get("buyer_role_match") else 1
    return generic_rank, tier_rank, role_rank
```

Green requires tenant-supplied data or published official/registry evidence binding address to person or official generic endpoint; yellow requires a corroborated person/role, an observed company-domain pattern, and a domain that accepts mail; red covers guessed, free-mail, catch-all, conflicting, generic-only unpublished, or uncorroborated data. Migration 019 stores tier, kind, method, evidence IDs, and checked time with tenant RLS, records itself, and is required at startup. Do not send verification emails. Generic contact success never counts as person-discovery success.

- [ ] **Step 4: Run contact, agent-output, quality, and Postgres parity tests**

Run: `scripts/run_tests.sh tests/server/test_contact_verification_tiers.py tests/server/test_api_mvp.py tests/server/test_run_harness.py tests/server/test_postgres_parity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit contact verification tiers**

```bash
git add server/lead_research/contacts.py server/routes/sales_intelligence.py server/agent_service.py server/quality.py server/db.py server/postgres.py server/supabase/migrations/019_contact_verification_tiers.sql server/supabase/verify.sql tests/server/test_contact_verification_tiers.py tests/server/test_api_mvp.py
git commit -m "feat(interfaze): tier contact verification evidence"
```

### Task 20: Enforce Outreach Language, Primary, and CC Safeguards

**Files:**
- Modify: `server/routes/outreach.py`
- Modify: `server/outreach_service.py`
- Modify: `server/quality.py`
- Modify: `server/routes/company.py`
- Modify: `server/webui/js/pages/setup.js`
- Test: `tests/server/test_outreach_contact_safety.py`
- Modify: `tests/server/test_api_mvp.py`

**Interfaces:**
- Consumes: contact tier/rank from Task 19 and company language-keyed templates.
- Produces: `eligible_primary_contacts`, `eligible_cc_contacts`, language-keyed `_template_for`, and Turkish-character QA before approval/send.

- [ ] **Step 1: Write failing CC, red-primary, and Turkish-template tests**

```python
def test_cc_contains_only_green_person_contacts(outreach_context):
    cc = _resolve_cc("cmp_a", outreach_context.lead, outreach_context.primary, outreach_context.request)
    assert cc == ["green.person@acme.test"]

def test_red_contact_is_never_auto_primary(db, client, user_headers, seeded_red_and_generic_contacts):
    response = client.post("/outreach/generate-for-lead", headers=user_headers, json={"lead_id": seeded_red_and_generic_contacts.lead_id, "language": "en"})
    assert response.status_code == 200
    run = response.json()
    assert run["contact_id"] == seeded_red_and_generic_contacts.green_generic_id

def test_turkish_character_guard_rejects_ascii_substitution():
    failures = validate_outreach_text("tr", "Sirketiniz icin cozum", "")
    assert "turkish_character_quality" in failures

def test_unsupported_language_is_reported_instead_of_falling_back_to_english(outreach_context):
    with pytest.raises(UnsupportedTemplateLanguage, match="no approved ar template"):
        _template_for("cmp_a", outreach_context.request, "ar", outreach_context.lead, outreach_context.primary)
```

- [ ] **Step 2: Run outreach safety tests and confirm permissive status filtering**

Run: `scripts/run_tests.sh tests/server/test_outreach_contact_safety.py tests/server/test_api_mvp.py -q`

Expected: FAIL because current CC admits any status except invalid and primary selection ignores the new tier/kind rules.

- [ ] **Step 3: Implement explicit outreach eligibility and language QA**

```python
def eligible_cc_contact(row) -> bool:
    return row["verification_tier"] == "green" and row["contact_kind"] == "person" and not row["do_not_contact"]

def eligible_primary_contact(row) -> bool:
    return row["verification_tier"] in {"green", "yellow"} and not row["do_not_contact"]

def validate_outreach_text(language: str, subject: str, body: str) -> list[str]:
    failures = preflight_message(subject, body)
    if language == "tr" and turkish_ascii_substitution_ratio(subject + " " + body) > 0.15:
        failures.append("turkish_character_quality")
    return failures
```

Templates are stored and resolved by language. Generation records selected language and template version; operator custom text must declare the same language. Unsupported template languages return a named validation error rather than English fallback. Yellow/red never enter CC; red never auto-primary; generic green may be primary only after all eligible people and remains visibly generic. Existing tenant-wide suppression and do-not-contact checks stay authoritative and tenant-scoped.

- [ ] **Step 4: Run outreach, compliance, quality, and company-template tests**

Run: `scripts/run_tests.sh tests/server/test_outreach_contact_safety.py tests/server/test_api_mvp.py tests/server/test_compliance.py tests/server/test_quality.py -q`

Expected: PASS.

- [ ] **Step 5: Commit outreach safeguards**

```bash
git add server/routes/outreach.py server/outreach_service.py server/quality.py server/routes/company.py server/webui/js/pages/setup.js tests/server/test_outreach_contact_safety.py tests/server/test_api_mvp.py
git commit -m "fix(interfaze): enforce outreach contact safeguards"
```

### Task 21: Schedule Freshness Refresh Without Starting Unbounded Work

**Files:**
- Modify: `server/scheduler.py`
- Modify: `server/app.py`
- Modify: `server/lead_research/facts.py`
- Modify: `server/lead_research/service.py`
- Modify: `server/config.py`
- Test: `tests/server/test_research_refresh_scheduler.py`
- Modify: `tests/server/test_daily_rhythm.py`

**Interfaces:**
- Consumes: per-field expiry from Task 9, durable campaign/research state from Task 13, existing scheduler thread.
- Produces: `ResearchRefreshService.enqueue_due(at: datetime, limit: int) -> int`; scheduler tick invokes a bounded refresh queue when enabled by existing YAML configuration.

- [ ] **Step 1: Write failing due-only, bounded, and idempotent refresh tests**

```python
def test_refresh_enqueues_only_due_consumed_facts_and_respects_limit(refresh, runs):
    seeded_fact(expires_at=PAST, consumer_count=2)
    seeded_fact(expires_at=FUTURE, consumer_count=4)
    assert refresh.enqueue_due(NOW_DT, limit=1) == 1
    assert len(runs.by_type("lead_research_refresh")) == 1

def test_same_due_fact_is_not_enqueued_twice(refresh):
    assert refresh.enqueue_due(NOW_DT, limit=10) == 1
    assert refresh.enqueue_due(NOW_DT, limit=10) == 0

def test_warm_refresh_does_not_rewrite_historical_campaign_score(refresh, seeded_score_snapshot):
    before = seeded_score_snapshot.snapshot_json
    refresh.enqueue_due(NOW_DT, limit=10)
    complete_refresh_runs()
    assert score_snapshot(seeded_score_snapshot.id).snapshot_json == before
```

- [ ] **Step 2: Run refresh/scheduler tests and confirm refresh queue is absent**

Run: `scripts/run_tests.sh tests/server/test_research_refresh_scheduler.py tests/server/test_daily_rhythm.py -q`

Expected: FAIL because the scheduler only writes daily digests.

- [ ] **Step 3: Add a bounded idempotent refresh phase to the existing scheduler**

```python
class ResearchRefreshService:
    def enqueue_due(self, at: datetime, limit: int) -> int:
        due = self.facts.due_for_refresh(at.timestamp(), limit)
        created = 0
        for fact in due:
            idempotency_key = f"lead-research-refresh:{fact.refresh_key}"
            existing = self.db.one("SELECT id FROM agent_runs WHERE company_id=? AND idempotency_key=?", (fact.company_id, idempotency_key))
            if existing:
                continue
            self.runs.create(fact.company_id, "lead_research_refresh", {"fact_id": fact.id, "field": fact.field, "dedupe_key": fact.refresh_key}, idempotency_key=idempotency_key)
            created += 1
        return created
```

Use `scheduler_enabled` and new nested YAML data under the existing server config structure (`research_refresh.enabled`, `research_refresh.hour`, `research_refresh.batch_limit`), not environment variables. The scheduler only enqueues bounded durable work; normal worker concurrency/cost/cancellation limits execute it. Warmed facts do not mutate campaign scores directly; the normal correction/recomputation path updates current results while historical snapshots stay append-only, and foreground correctness never depends on refresh completion.

- [ ] **Step 4: Run scheduler, refresh, config, and agent-run tests**

Run: `scripts/run_tests.sh tests/server/test_research_refresh_scheduler.py tests/server/test_daily_rhythm.py tests/server/test_config.py tests/server/test_run_harness.py -q`

Expected: PASS.

- [ ] **Step 5: Commit scheduled refresh**

```bash
git add server/scheduler.py server/app.py server/lead_research/facts.py server/lead_research/service.py server/config.py tests/server/test_research_refresh_scheduler.py tests/server/test_daily_rhythm.py
git commit -m "feat(interfaze): schedule bounded research refresh"
```

### Task 22: Backfill Compatibility Data and Prove Two-Tenant Migration Safety

**Files:**
- Create: `server/lead_research/backfill.py`
- Create: `server/supabase/migrations/020_lead_research_contract_backfill.sql`
- Modify: `server/postgres.py`
- Modify: `server/supabase/verify.sql`
- Modify: `server/README.md`
- Create: `tests/server/lead_research/test_contract_backfill.py`
- Modify: `tests/server/test_postgres_database.py`
- Modify: `tests/server/test_postgres_parity.py`

**Interfaces:**
- Consumes: legacy `company_sections`, candidate datasets, tenant organizations/evidence/claims, existing leads/results/contacts.
- Produces: idempotent `backfill_contract(db: Database) -> BackfillReport` and deploy-safe migration verification with no automatic cross-tenant promotion.

- [ ] **Step 1: Write failing idempotent/backward-compatible backfill tests**

```python
def test_backfill_is_idempotent_and_never_promotes_legacy_claims_without_validation(legacy_db):
    first = backfill_contract(legacy_db)
    second = backfill_contract(legacy_db)
    assert first.profile_versions_created == 2
    assert second.profile_versions_created == 0
    assert legacy_db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"] == 0
    tenant_count = legacy_db.one("SELECT COUNT(*) AS n FROM tenant_facts")["n"]
    legacy_count = legacy_db.one("SELECT COUNT(*) AS n FROM legacy_claims")["n"]
    assert tenant_count == legacy_count

def test_backfilled_campaign_and_contact_routes_remain_readable(client, legacy_seed, user_headers):
    assert client.get(f"/research-campaigns/{legacy_seed.campaign_id}", headers=user_headers).status_code == 200
    assert client.get(f"/contacts/{legacy_seed.contact_id}", headers=user_headers).status_code == 200
```

- [ ] **Step 2: Run backfill/Postgres tests and confirm compatibility gaps**

Run: `scripts/run_tests.sh tests/server/lead_research/test_contract_backfill.py tests/server/test_postgres_database.py tests/server/test_postgres_parity.py -q`

Expected: FAIL because legacy rows are not transformed into the new contracts.

- [ ] **Step 3: Implement conservative idempotent backfill and verification SQL**

```python
@dataclass(frozen=True)
class BackfillReport:
    profile_versions_created: int
    datasets_classified: int
    tenant_facts_created: int
    results_snapshotted: int
    contacts_classified: int

def backfill_contract(db: Database) -> BackfillReport:
    return BackfillReport(
        profile_versions_created=backfill_profiles(db),
        datasets_classified=backfill_candidate_visibility(db),
        tenant_facts_created=backfill_legacy_facts_as_tenant_private(db),
        results_snapshotted=backfill_result_snapshots(db),
        contacts_classified=backfill_contacts_as_unverified(db),
    )
```

Migration 020 performs only conservative SQL-safe classifications and records itself; the idempotent application backfill handles JSON transformations. Legacy company sections become version 1 profiles; old datasets become `service_public`; old tenant evidence/claims remain tenant-private until revalidated; old results retain original payload and gain a compatibility snapshot; old contacts become red/unverified unless existing published evidence supports a higher tier. Update `verify.sql` to assert all migrations, indexes, RLS, and absence of authenticated policies on shared tables. Document apply order, backfill command, rollback boundary, and post-deploy verification.

- [ ] **Step 4: Run migration, backfill, route compatibility, and parity tests**

Run: `scripts/run_tests.sh tests/server/lead_research/test_contract_backfill.py tests/server/test_postgres_database.py tests/server/test_postgres_parity.py tests/server/test_research_webui.py tests/server/test_api_mvp.py -q`

Expected: PASS.

- [ ] **Step 5: Commit compatibility migration**

```bash
git add server/lead_research/backfill.py server/postgres.py server/supabase/migrations/020_lead_research_contract_backfill.sql server/supabase/verify.sql server/README.md tests/server/lead_research/test_contract_backfill.py tests/server/test_postgres_database.py tests/server/test_postgres_parity.py
git commit -m "feat(interfaze): backfill lead research contracts"
```

### Task 23: Prove Clean-Database and Two-Tenant End-to-End Behavior

**Files:**
- Create: `tests/server/test_lead_research_contract_e2e.py`
- Modify: `tests/server/test_clean_demo_e2e.py`
- Modify: `tests/server/lead_research/fakes.py`
- Modify: `server/STATUS.md`

**Interfaces:**
- Consumes: the complete implementation from Tasks 1–22.
- Produces: deterministic E2E proof for onboarding, campaign creation, public/private candidate discovery, evidence reuse, agentic fallback, scoring, display serialization, contact safeguards, cancellation, correction, and tenant isolation; fake helpers `onboard_two_companies(app)`, `create_and_run_campaign(app, tenant, *, product_terms, countries=None, weights=None)`, `wait_for_results(app, tenant, campaign_id)`, `contract_scenario(name)`, and tenant-scoped assertion helpers used below.

- [ ] **Step 1: Write the failing full contract E2E test**

```python
def test_lead_research_contract_end_to_end(app_factory, fake_sources, fake_agent):
    app = app_factory(clean_db=True, sources=fake_sources, agent=fake_agent)
    a, b = onboard_two_companies(app)
    upload_private_candidates(app, a, ["A Valve GmbH"])
    upload_private_candidates(app, b, ["B Valve GmbH"])
    campaign = create_and_run_campaign(app, a, product_terms=["industrial valve"], countries=["DE"])
    results = wait_for_results(app, a, campaign.id)
    assert {row["company_name"] for row in results} == {"A Valve GmbH", "Public Valve GmbH"}
    assert all(row["profile_version_id"] == campaign.profile_version_id for row in results)
    assert all(row["known_weight"] + row["unknown_weight"] + sum(row["not_applicable_dimensions"].values()) == 100 for row in results)
    assert all("hidden_label_ids" not in row for row in results)
    assert every_evidence_span_matches_snapshot(app.state.db, a.company_id, campaign.id)
    assert campaign_results(app, b, campaign.id).status_code == 404
    assert eligible_cc(app.state.db, a.company_id) == ["green.person@public-valve.test"]

def test_two_tenants_reuse_public_fact_but_keep_decisions_and_compliance_private(app_factory, fake_sources, fake_agent):
    app = app_factory(clean_db=True, sources=fake_sources, agent=fake_agent)
    a, b = onboard_two_companies(app)
    first = create_and_run_campaign(app, a, product_terms=["industrial valve"], weights=weights(product_sector_fit=60, buyer_channel_fit=40))
    second = create_and_run_campaign(app, b, product_terms=["industrial valve"], weights=weights(product_sector_fit=40, buyer_channel_fit=60))
    assert shared_fact_count(app.state.db, "public-valve.test") == 1
    assert campaign_result(app, a, first.id).fit_score != campaign_result(app, b, second.id).fit_score
    assign_hidden_label(app, a, first.id, "high_export_readiness")
    suppress_address(app, a, "buyer@public-valve.test")
    assert hidden_labels(app, b, second.id) == []
    assert is_suppressed(app, b, "buyer@public-valve.test") is False

@pytest.mark.parametrize("scenario,explanation", [
    ("no_runnable_source", "no_candidate_source_runnable"),
    ("missing_market_mapping", "product_terms_missing_local_mapping"),
    ("no_named_candidate", "sources_named_no_candidate"),
    ("excluded_range", "candidates_excluded_by_range"),
    ("eligibility_veto", "candidates_failed_eligibility"),
    ("below_threshold", "researched_below_threshold"),
    ("source_failure", "sources_failed"),
    ("cancelled", "campaign_cancelled"),
])
def test_zero_lead_outcome_has_named_explanation(contract_scenario, scenario, explanation):
    result = contract_scenario(scenario)
    assert result.leads == []
    assert result.zero_result_explanation == explanation
```

- [ ] **Step 2: Run the E2E tests and confirm any remaining cross-slice contract failure**

Run: `scripts/run_tests.sh tests/server/test_lead_research_contract_e2e.py tests/server/test_clean_demo_e2e.py -q`

Expected: FAIL at the first integration boundary not yet wired exactly as the design requires.

- [ ] **Step 3: Wire only the integration gaps revealed by the E2E test**

```python
def assert_contract_result(result: dict) -> None:
    required = {"profile_version_id", "scope", "fit_score", "evidence_confidence", "known_weight", "unknown_weight", "unknown_dimensions", "not_applicable_dimensions", "priority_band", "evidence"}
    missing = required - result.keys()
    if missing:
        raise AssertionError(f"research result missing contract fields: {sorted(missing)}")
    total = result["known_weight"] + result["unknown_weight"] + sum(result["not_applicable_dimensions"].values())
    if total != 100:
        raise AssertionError("known, unknown, and not-applicable scoring weights must total 100")
```

Keep fixes inside the owning modules from the file map. Do not add an E2E-only branch or mock-only shortcut. Update `server/STATUS.md` with the new verified behavior, operational migration requirement, and any explicitly deferred external-source credentials.

- [ ] **Step 4: Run the E2E plus complete targeted lead-research suite**

Run: `scripts/run_tests.sh tests/server/test_lead_research_contract_e2e.py tests/server/test_clean_demo_e2e.py tests/server/lead_research tests/server/test_research_webui.py -q`

Run: `node --test tests/server/webui/test_research_brief.mjs tests/server/webui/test_research_results.mjs tests/server/webui/test_research_scoring.mjs tests/server/webui/test_research_onboarding.mjs tests/server/webui/test_admin_research_quality.mjs`

Expected: PASS.

- [ ] **Step 5: Commit E2E proof**

```bash
git add tests/server/test_lead_research_contract_e2e.py tests/server/test_clean_demo_e2e.py tests/server/lead_research/fakes.py server/STATUS.md
git commit -m "test(interfaze): prove lead research contract end to end"
```

### Task 24: Run Final Regression and Security Verification

**Files:**
- Modify only if verification exposes a real defect: the owning source/test file from Tasks 1–23.

**Interfaces:**
- Consumes: completed implementation and all tests.
- Produces: fresh verification evidence suitable for completion review.

- [ ] **Step 1: Run formatting/static checks available in the repository**

Run: `git diff --check`

Run: `python -m compileall -q server`

Expected: both exit 0.

- [ ] **Step 2: Run the complete Python lead-research and adjacent server suite**

Run: `scripts/run_tests.sh tests/server/lead_research tests/server/test_lead_research_contract_e2e.py tests/server/test_research_webui.py tests/server/test_clean_demo_e2e.py tests/server/test_company_research_profile.py tests/server/test_contact_verification_tiers.py tests/server/test_outreach_contact_safety.py tests/server/test_admin_research_quality.py tests/server/test_postgres_parity.py tests/server/test_postgres_backend.py tests/server/test_postgres_database.py -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Run all research-related Node tests**

Run: `node --test tests/server/webui/test_research_brief.mjs tests/server/webui/test_research_results.mjs tests/server/webui/test_research_scoring.mjs tests/server/webui/test_research_onboarding.mjs tests/server/webui/test_admin_research_quality.mjs`

Expected: PASS with zero failures.

- [ ] **Step 4: Run the full repository test command and inspect failures**

Run: `scripts/run_tests.sh`

Expected: PASS. If an environment-dependent test is skipped, record its exact skip reason; if a test fails, apply `superpowers:systematic-debugging`, reproduce the root cause, add or tighten its focused test, fix the owning module, and rerun Steps 1–4.

- [ ] **Step 5: Review the completed branch before integration**

Run: `git status --short`

Run: `git diff --stat origin/main..HEAD`

Run: `git log --oneline origin/main..HEAD`

Expected: only planned files are changed, every implementation slice has its focused commit, and the final test output is fresh. Apply `superpowers:requesting-code-review`, resolve evidence-backed findings, rerun affected tests, then apply `superpowers:finishing-a-development-branch` for the user's chosen integration path.
