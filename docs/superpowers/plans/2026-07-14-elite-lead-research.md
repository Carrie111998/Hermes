# Elite Lead Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a tenant-scoped, modular lead-research vertical slice that configures campaigns in the web UI, ingests removable datasets, normalizes evidence, enriches and scores named companies, reports funnel metrics, and exports auditable results.

**Architecture:** Add a `server/lead_research` application package behind existing FastAPI and agent-run boundaries. Providers write immutable snapshots and canonical evidence; deterministic services resolve identity, qualify, score, and measure results; the existing sales skills orchestrate discovery and one-company enrichment. The web UI consumes server-owned catalogs and configuration rather than hard-coding provider behavior.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLite/Postgres-compatible SQL, gzip JSONL snapshots, PyYAML, pytest, vanilla ES modules, existing `server/webui` UI utilities.

## Global Constraints

- Add no new core Hermes model tool and do not change the model tool schema.
- Preserve the byte-stable system prompt and existing `lead_scan` / `lead_research` run-type split.
- Put behavioral settings in tenant application data and the web UI; use secret storage only for credentials.
- Resolve local storage through `get_hermes_home()` and isolate data by `company_id`.
- Never create a named lead from aggregate market data.
- Every feature claim includes provenance, period, method, confidence, and status.
- Keep `fit_score` and `evidence_confidence` separate in database, API, UI, and CSV.
- Use TDD, real imports, a temporary `HERMES_HOME`, and tenant-boundary E2E tests.
- Do not place vendor-specific third-party integrations in Hermes core; licensed adapters load through the provider boundary.
- Follow `docs/research-page-UI-guidelines.md` for all web behavior and copy.

---

## Delivery decomposition

This plan is split into reviewable streams whose outputs compose through stable interfaces:

1. Foundation: sectors, canonical evidence, local store, provider registry, and fixture provider.
2. Campaign runtime: lifecycle, acquisition, identity, eligibility, scoring, metrics, and removal semantics.
3. Public source packs: trade, registry/procurement, and exhibition/opportunity adapters.
4. Enrichment: company claims and bounded local-model web research.
5. Web product: research workspace, source administration, run detail, and lead evidence.
6. Qualification: E2E, CSV, security, accessibility, and operational documentation.

Each stream must pass its exit test before the next stream depends on it.

## File map

### New backend package

```text
server/lead_research/__init__.py              public service exports
server/lead_research/models.py                canonical Pydantic contracts
server/lead_research/paths.py                 profile-aware tenant paths
server/lead_research/schema.py                application SQL additions
server/lead_research/storage.py               snapshots and evidence repository
server/lead_research/sectors.py               taxonomy loading/generation
server/lead_research/registry.py              provider catalog/lifecycle
server/lead_research/acquisition.py           partition runner/checkpoints
server/lead_research/identity.py              reversible organization resolution
server/lead_research/qualification.py         eligibility gates
server/lead_research/scoring.py               fit/confidence calculation
server/lead_research/metrics.py               estimates and actual funnel metrics
server/lead_research/enrichment.py            feature completeness and research jobs
server/lead_research/providers/base.py         provider protocol
server/lead_research/providers/fixture.py      deterministic contract provider
server/lead_research/providers/trade.py        aggregate trade providers
server/lead_research/providers/registry.py     company registry providers
server/lead_research/providers/procurement.py  procurement providers
server/lead_research/providers/exhibitions.py  event/directory providers
server/lead_research/providers/matchmaking.py  permissioned buyer-intent imports
server/routes/research_campaigns.py            tenant API
```

### New skill data

```text
skills/sales/lead-research/references/sectors.yaml
skills/sales/lead-research/references/sectors.md
skills/sales/lead-research/references/sectors.csv
skills/sales/lead-research/references/provider-catalog.yaml
skills/sales/lead-research/references/feature-playbooks.yaml
```

### New web modules

```text
server/webui/js/pages/research.js
server/webui/js/pages/research-editor.js
server/webui/js/pages/research-detail.js
server/webui/js/pages/research-source-picker.js
server/webui/js/pages/research-scoring.js
server/webui/js/pages/research-enrichment.js
server/webui/js/pages/research-evidence.js
server/webui/js/research-state.js
```

### Tests

```text
tests/server/lead_research/test_sectors.py
tests/server/lead_research/test_models.py
tests/server/lead_research/test_storage.py
tests/server/lead_research/test_provider_contract.py
tests/server/lead_research/test_campaigns.py
tests/server/lead_research/test_identity.py
tests/server/lead_research/test_scoring.py
tests/server/lead_research/test_enrichment.py
tests/server/lead_research/test_public_providers.py
tests/server/lead_research/test_exhibition_providers.py
tests/server/lead_research/test_e2e.py
tests/server/test_research_webui.py
```

---

### Task 1: Canonical sector taxonomy and generated Markdown/CSV

**Files:**
- Create: `server/lead_research/sectors.py`
- Create: `skills/sales/lead-research/references/sectors.yaml`
- Create: `skills/sales/lead-research/references/sectors.md`
- Create: `skills/sales/lead-research/references/sectors.csv`
- Test: `tests/server/lead_research/test_sectors.py`

**Interfaces:**
- Produces: `load_sectors(path: Path) -> tuple[Sector, ...]`
- Produces: `render_sector_markdown(sectors) -> str`
- Produces: `render_sector_csv(sectors) -> str`

- [ ] **Step 1: Write failing deterministic-generation tests**

```python
def test_generated_sector_artifacts_are_current():
    sectors = load_sectors(SECTOR_YAML)
    assert SECTOR_MD.read_text() == render_sector_markdown(sectors)
    assert SECTOR_CSV.read_text() == render_sector_csv(sectors)

def test_sector_codes_and_ids_are_unique():
    sectors = load_sectors(SECTOR_YAML)
    assert len({s.sector_id for s in sectors}) == len(sectors)
    assert all(s.hs_2022 or s.cpv_2008 or s.cpc for s in sectors)
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `pytest tests/server/lead_research/test_sectors.py -v`

Expected: FAIL importing `server.lead_research.sectors`.

- [ ] **Step 3: Implement strict sector loading and deterministic rendering**

```python
class Sector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sector_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    aliases: list[str] = Field(default_factory=list)
    hs_2022: list[str] = Field(default_factory=list)
    nace_rev2: list[str] = Field(default_factory=list)
    naics_2022: list[str] = Field(default_factory=list)
    cpv_2008: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)
    buyer_types: list[str]
    applicable_features: list[str]
    sourcing_terms: list[str] = Field(default_factory=list)
    default_source_categories: list[str]
```

Sort rows by `sector_id`, sort every code list, quote CSV using `csv.DictWriter`, and end both generated files with one newline.

- [ ] **Step 4: Generate artifacts and rerun tests**

Run: `python -m server.lead_research.sectors --check`

Expected: exit 0 and `sector taxonomy artifacts are current`.

Run: `pytest tests/server/lead_research/test_sectors.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the taxonomy unit**

```bash
git add server/lead_research/sectors.py skills/sales/lead-research/references tests/server/lead_research/test_sectors.py
git commit -m "feat(sales): add canonical lead research sectors"
```

---

### Task 2: Canonical evidence and claim models

**Files:**
- Create: `server/lead_research/__init__.py`
- Create: `server/lead_research/models.py`
- Test: `tests/server/lead_research/test_models.py`

**Interfaces:**
- Produces: `DiscoveryQuery`, `DiscoveryEstimate`, `DatasetDefinition`, `EvidenceEnvelope`, `Organization`, `MarketSignal`, `CompanySignal`, `Claim`, `CampaignConfig`, `ScoringProfile`, `EnrichmentProfile`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_market_signal_cannot_become_lead_candidate():
    signal = MarketSignal(metric="import_value", value=10, currency="USD", period="2025")
    with pytest.raises(ValidationError):
        LeadCandidate(organization_id=None, qualifying_evidence=[signal])

def test_claim_requires_period_for_time_varying_numeric_fields():
    with pytest.raises(ValidationError):
        Claim(field="store_count", value=84, status="observed", evidence_ids=["ev_1"])
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/server/lead_research/test_models.py -v`

Expected: FAIL importing model classes.

- [ ] **Step 3: Implement strict discriminated models**

```python
ClaimStatus = Literal["observed", "calculated", "estimated_range", "conflicted", "unknown", "not_applicable"]
RecordType = Literal["organization", "market_signal", "company_signal", "event", "opportunity", "lead_candidate"]

class Claim(ApiModel):
    field: str
    value: str | int | float | bool | list[str] | None = None
    low: float | None = None
    high: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    status: ClaimStatus
    confidence: float = Field(ge=0, le=1)
    method: Literal["observed", "calculated", "estimated_range"]
    evidence_ids: list[str]
    applicability: Literal["required", "useful", "not_applicable"]
```

Add model validators for range ordering, currency/value compatibility, evidence requirements, target/seller country normalization, score weight sum, and monotonic bands.

- [ ] **Step 4: Run model tests**

Run: `pytest tests/server/lead_research/test_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit contracts**

```bash
git add server/lead_research tests/server/lead_research/test_models.py
git commit -m "feat(sales): define lead research evidence contracts"
```

---

### Task 3: Profile-aware tenant store and schema

**Files:**
- Create: `server/lead_research/paths.py`
- Create: `server/lead_research/schema.py`
- Create: `server/lead_research/storage.py`
- Modify: `server/db.py`
- Test: `tests/server/lead_research/test_storage.py`

**Interfaces:**
- Produces: `tenant_research_root(company_id: str) -> Path`
- Produces: `EvidenceRepository.save_snapshot`, `save_evidence`, `withdraw_source`, `impact`

- [ ] **Step 1: Write failing path, isolation, and idempotency tests**

```python
def test_snapshot_is_tenant_local_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    repo = EvidenceRepository(db, company_id="company_a")
    first = repo.save_snapshot(page)
    second = repo.save_snapshot(page)
    assert first.id == second.id
    assert str(first.path).startswith(str(tmp_path / "lead-research" / "company_a"))
    assert not repo.for_company("company_b").get_snapshot(first.id)
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/server/lead_research/test_storage.py -v`

Expected: FAIL because the repository is absent.

- [ ] **Step 3: Add normalized SQL tables and repository**

Add tables for `research_campaigns`, `dataset_definitions`, `dataset_snapshots`, `organizations`, `organization_links`, `evidence_records`, `feature_claims`, `campaign_partitions`, `campaign_metrics`, and `research_issues`. Every tenant-owned table includes `company_id` and an index beginning with it.

Store raw pages atomically as gzip JSONL using a temporary sibling and `Path.replace()`. Use `(company_id, source_id, source_record_id, snapshot_id, raw_hash)` as the evidence uniqueness key.

- [ ] **Step 4: Run storage and existing DB tests**

Run: `pytest tests/server/lead_research/test_storage.py tests/server/test_api_mvp.py -v`

Expected: PASS.

- [ ] **Step 5: Commit storage**

```bash
git add server/db.py server/lead_research/paths.py server/lead_research/schema.py server/lead_research/storage.py tests/server/lead_research/test_storage.py
git commit -m "feat(sales): add tenant lead evidence store"
```

---

### Task 4: Provider contract, catalog, and fixture provider

**Files:**
- Create: `server/lead_research/providers/__init__.py`
- Create: `server/lead_research/providers/base.py`
- Create: `server/lead_research/providers/fixture.py`
- Create: `server/lead_research/registry.py`
- Create: `skills/sales/lead-research/references/provider-catalog.yaml`
- Test: `tests/server/lead_research/test_provider_contract.py`

**Interfaces:**
- Produces: `Provider` protocol and `ProviderRegistry`.
- Consumes: canonical models and repository.

- [ ] **Step 1: Write the shared provider contract test**

```python
@pytest.mark.parametrize("provider_factory", PROVIDER_FACTORIES)
def test_provider_contract(provider_factory, discovery_query):
    provider = provider_factory()
    estimate = provider.discover(discovery_query)
    page = provider.fetch_page(discovery_query, cursor=None)
    records = [item for raw in page.records for item in provider.normalize(raw, page.snapshot)]
    assert estimate.kind in {"reported", "historical_range", "unavailable"}
    assert all(item.source_id == provider.definition.source_id for item in records)
    assert provider.checkpoint(page) == page.next_cursor
    assert provider.health().status in {"active", "degraded", "retired"}
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/server/lead_research/test_provider_contract.py -v`

Expected: FAIL importing the provider protocol.

- [ ] **Step 3: Implement registry and deterministic fixture provider**

```python
class Provider(Protocol):
    definition: DatasetDefinition
    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate: ...
    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage: ...
    def normalize(self, record: RawRecord, snapshot: SnapshotRef) -> list[EvidenceEnvelope]: ...
    def checkpoint(self, page: RawPage) -> str | None: ...
    def health(self) -> ProviderHealth: ...
```

Registry validation rejects duplicate IDs, undeclared capabilities, secret values in catalog YAML, and a retired source marked enabled.

- [ ] **Step 4: Run the contract suite**

Run: `pytest tests/server/lead_research/test_provider_contract.py -v`

Expected: PASS for the fixture provider.

- [ ] **Step 5: Commit provider foundation**

```bash
git add server/lead_research/providers server/lead_research/registry.py skills/sales/lead-research/references/provider-catalog.yaml tests/server/lead_research/test_provider_contract.py
git commit -m "feat(sales): add modular research provider registry"
```

---

### Task 5: Acquisition, identity, and source-removal behavior

**Files:**
- Create: `server/lead_research/acquisition.py`
- Create: `server/lead_research/identity.py`
- Create: `server/lead_research/qualification.py`
- Test: `tests/server/lead_research/test_identity.py`
- Test: `tests/server/lead_research/test_campaigns.py`

**Interfaces:**
- Produces: `CampaignRunner.run_partition(partition_id) -> PartitionResult`
- Produces: `IdentityResolver.resolve(evidence) -> ResolutionResult`
- Produces: `EligibilityService.evaluate(candidate, config) -> EligibilityResult`

- [ ] **Step 1: Write failing behavioral tests**

```python
def test_aggregate_trade_signal_never_creates_organization(runner):
    result = runner.ingest([market_signal_fixture()])
    assert result.market_signals == 1
    assert result.named_candidates == 0

def test_purge_withdraws_only_source_evidence(service):
    lead = service.seed_lead(sources=["registry", "event"])
    service.purge_source("event")
    assert service.get_lead(lead.id).qualified is True
    assert service.get_lead(lead.id).source_ids == ["registry"]
```

- [ ] **Step 2: Run and confirm failures**

Run: `pytest tests/server/lead_research/test_identity.py tests/server/lead_research/test_campaigns.py -v`

Expected: FAIL because runner/resolver services are absent.

- [ ] **Step 3: Implement bounded partitions and reversible identity links**

Partition on `(campaign_id, target_country, sector_id, source_id, cursor)`. Checkpoint after snapshot and normalized evidence commit. Identity resolution prioritizes registry ID, tax ID, LEI, and verified domain; ambiguous name/address matches create a `research_issue` instead of merging.

Eligibility returns explicit gate results:

```python
class EligibilityResult(ApiModel):
    eligible: bool
    gates: dict[str, Literal["pass", "fail", "unknown", "not_applicable"]]
    reasons: list[str]
```

- [ ] **Step 4: Run acquisition and identity tests**

Run: `pytest tests/server/lead_research/test_identity.py tests/server/lead_research/test_campaigns.py -v`

Expected: PASS, including resume and provider-isolation cases.

- [ ] **Step 5: Commit runtime core**

```bash
git add server/lead_research/acquisition.py server/lead_research/identity.py server/lead_research/qualification.py tests/server/lead_research/test_identity.py tests/server/lead_research/test_campaigns.py
git commit -m "feat(sales): add resumable lead research pipeline"
```

---

### Task 6: Fit scoring, evidence confidence, estimates, and metrics

**Files:**
- Create: `server/lead_research/scoring.py`
- Create: `server/lead_research/metrics.py`
- Test: `tests/server/lead_research/test_scoring.py`

**Interfaces:**
- Produces: `score_lead(candidate, claims, profile) -> LeadScore`
- Produces: `estimate_campaign(config, providers, history) -> CampaignEstimate`
- Produces: `CampaignMetricsRecorder`.

- [ ] **Step 1: Write failing invariant tests**

```python
def test_fit_and_confidence_are_separate():
    result = score_lead(high_fit_candidate(), weak_evidence(), DEFAULT_PROFILE)
    assert result.fit_score >= 75
    assert result.evidence_confidence < 0.5
    assert result.priority_band == "B"

def test_estimate_is_suppressed_without_basis():
    result = estimate_campaign(config, providers=[], history=[])
    assert result.status == "unavailable"
    assert result.qualified_range is None
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/server/lead_research/test_scoring.py -v`

Expected: FAIL importing scoring services.

- [ ] **Step 3: Implement configurable scoring and labeled estimate ranges**

Calculate fit only from applicable dimensions. Calculate confidence from authority, corroboration, freshness, conflict penalty, and estimated-value share. Store the seven dimension scores, confidence factors, eligibility result, and explanation inputs.

Pre-run estimates include `status`, `basis`, `confidence`, `named_candidate_range`, `eligible_range`, `qualified_range`, and `unavailable_source_ids`. Post-run metrics store actual funnel counts only.

- [ ] **Step 4: Run tests**

Run: `pytest tests/server/lead_research/test_scoring.py -v`

Expected: PASS.

- [ ] **Step 5: Commit scoring and metrics**

```bash
git add server/lead_research/scoring.py server/lead_research/metrics.py tests/server/lead_research/test_scoring.py
git commit -m "feat(sales): add evidence-aware lead scoring metrics"
```

---

### Task 7: Campaign API and run integration

**Files:**
- Create: `server/routes/research_campaigns.py`
- Modify: `server/app.py`
- Modify: `server/run_types.py`
- Modify: `server/agent_service.py`
- Test: `tests/server/lead_research/test_campaigns.py`

**Interfaces:**
- Produces the API resources listed in `docs/research-page-UI-guidelines.md` section 10.
- Consumes campaign runner, metrics, registry, and existing `AgentRunService`.

- [ ] **Step 1: Write failing API round-trip tests**

```python
def test_campaign_draft_round_trips_all_configuration(client, tenant_headers, campaign_body):
    created = client.post("/api/v1/research-campaigns", headers=tenant_headers, json=campaign_body)
    assert created.status_code == 201
    loaded = client.get(f"/api/v1/research-campaigns/{created.json()['id']}", headers=tenant_headers)
    assert loaded.json()["config"] == campaign_body

def test_cross_tenant_campaign_access_is_forbidden(client, tenant_a, tenant_b):
    campaign = create_campaign(client, tenant_a)
    assert client.get(f"/api/v1/research-campaigns/{campaign['id']}", headers=tenant_b).status_code == 404
```

- [ ] **Step 2: Run and confirm 404 failures**

Run: `pytest tests/server/lead_research/test_campaigns.py -v`

Expected: FAIL because routes are absent.

- [ ] **Step 3: Implement tenant-scoped lifecycle and optimistic versioning**

Statuses are `draft`, `estimating`, `queued`, `running`, `partial`, `completed`, `failed`, `cancelled`, and `archived`. PATCH requires the current integer `version`; mismatches return HTTP 409 with the current resource. Start validates all effective configuration and creates one parent agent run plus partition records.

- [ ] **Step 4: Run API and existing run harness tests**

Run: `pytest tests/server/lead_research/test_campaigns.py tests/server/test_run_harness.py tests/server/test_api_mvp.py -v`

Expected: PASS.

- [ ] **Step 5: Commit campaign API**

```bash
git add server/routes/research_campaigns.py server/app.py server/run_types.py server/agent_service.py tests/server/lead_research/test_campaigns.py
git commit -m "feat(sales): expose lead research campaigns"
```

---

### Task 8: Public trade, registry, and procurement adapter packs

**Files:**
- Create: `server/lead_research/providers/http.py`
- Create: `server/lead_research/providers/trade.py`
- Create: `server/lead_research/providers/registry.py`
- Create: `server/lead_research/providers/procurement.py`
- Modify: `skills/sales/lead-research/references/provider-catalog.yaml`
- Test: `tests/server/lead_research/test_public_providers.py`
- Create: `tests/server/lead_research/fixtures/providers/`

**Interfaces:**
- Produces providers for UN Comtrade, Eurostat Comext, WITS, TÜİK metadata/import, US Census trade, Companies House, SEC EDGAR, TED, Contracts Finder, SAM.gov, USAspending, Etimad, and UAE public procurement/statistics where machine access is available.

- [ ] **Step 1: Add recorded-response contract fixtures and failing normalization tests**

```python
@pytest.mark.parametrize("source_id,fixture,expected_type", [
    ("un-comtrade", "un_comtrade_page.json", "market_signal"),
    ("companies-house", "companies_house_page.json", "organization"),
    ("ted", "ted_page.json", "opportunity"),
])
def test_official_provider_normalization(source_id, fixture, expected_type, registry):
    provider = registry.get(source_id)
    records = normalize_fixture(provider, fixture)
    assert records and {r.record_type for r in records} == {expected_type}
```

- [ ] **Step 2: Run and confirm unregistered-provider failures**

Run: `pytest tests/server/lead_research/test_public_providers.py -v`

Expected: FAIL because providers are not registered.

- [ ] **Step 3: Implement injected HTTP transport and adapters**

`HttpTransport` accepts base URL, safe headers, timeout, and limiter; tests inject fixture responses. Aggregate trade adapters emit only `MarketSignal`. Registry adapters emit legal `Organization` evidence. Procurement adapters emit `Opportunity` plus named buyer organizations when the source supplies them.

Catalog every source even when its adapter mode is `manual_import` or `credential_required`. Never convert a web-only catalog entry into an unsupported scraper.

- [ ] **Step 4: Run adapter and shared contract tests**

Run: `pytest tests/server/lead_research/test_public_providers.py tests/server/lead_research/test_provider_contract.py -v`

Expected: PASS without live network access.

- [ ] **Step 5: Commit official source packs**

```bash
git add server/lead_research/providers skills/sales/lead-research/references/provider-catalog.yaml tests/server/lead_research/test_public_providers.py tests/server/lead_research/fixtures/providers
git commit -m "feat(sales): add official lead research source packs"
```

---

### Task 9: Exhibition, matchmaking, and customer-upload adapters

**Files:**
- Create: `server/lead_research/providers/exhibitions.py`
- Create: `server/lead_research/providers/matchmaking.py`
- Modify: `skills/sales/lead-research/references/provider-catalog.yaml`
- Test: `tests/server/lead_research/test_exhibition_providers.py`

**Interfaces:**
- Produces event discovery imports for AUMA, TOBB, EEN, and US ITA.
- Produces official exhibitor directory imports when permitted.
- Produces credentialed/customer-export adapters for b2match, Swapcard, and Grip.

- [ ] **Step 1: Write failing intent-classification tests**

```python
def test_exhibitor_status_alone_is_not_buying_intent(provider):
    evidence = provider.normalize(exhibitor_fixture(), snapshot())
    assert not any(item.payload.get("intent") == "buying" for item in evidence)

def test_looking_for_field_is_explicit_intent(matchmaking_provider):
    evidence = matchmaking_provider.normalize(looking_for_fixture(), snapshot())
    assert any(item.record_type == "opportunity" and item.payload["intent"] == "sourcing" for item in evidence)
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/server/lead_research/test_exhibition_providers.py -v`

Expected: FAIL because event adapters are absent.

- [ ] **Step 3: Implement layered event normalization**

Use stable organizer/event/company external IDs. Event discovery emits `Event`; directory membership emits `Organization` plus attendance relation; explicit sourcing text, meeting goals, hosted-buyer role, or buyer-mission brief emits `Opportunity`. Store access and redistribution policy with every snapshot.

- [ ] **Step 4: Run tests**

Run: `pytest tests/server/lead_research/test_exhibition_providers.py tests/server/lead_research/test_provider_contract.py -v`

Expected: PASS.

- [ ] **Step 5: Commit exhibition intelligence**

```bash
git add server/lead_research/providers/exhibitions.py server/lead_research/providers/matchmaking.py skills/sales/lead-research/references/provider-catalog.yaml tests/server/lead_research/test_exhibition_providers.py
git commit -m "feat(sales): add exhibition buyer intent providers"
```

---

### Task 10: Feature playbooks and bounded local-model enrichment

**Files:**
- Create: `server/lead_research/enrichment.py`
- Create: `skills/sales/lead-research/references/feature-playbooks.yaml`
- Modify: `skills/sales/lead-research/SKILL.md`
- Test: `tests/server/lead_research/test_enrichment.py`

**Interfaces:**
- Produces: `FeaturePlanner.missing_claims(organization, sector_ids) -> list[FeatureRequest]`
- Produces: `EnrichmentService.research_company(job) -> EnrichmentResult`
- Consumes: existing Hermes model profile and web retrieval capabilities through an injected executor.

- [ ] **Step 1: Write failing applicability and evidence tests**

```python
def test_store_count_does_not_apply_to_pure_manufacturer(planner):
    requests = planner.missing_claims(manufacturer(), ["industrial-machinery"])
    assert "store_count" not in {request.field for request in requests}

def test_unsupported_numeric_ai_claim_is_rejected(service):
    result = service.validate_claim(ai_claim(value=84, evidence_ids=[]))
    assert result.accepted is False
    assert result.reason == "numeric_claim_requires_evidence"
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/server/lead_research/test_enrichment.py -v`

Expected: FAIL because enrichment services are absent.

- [ ] **Step 3: Implement priority retrieval, budgets, and claim validation**

One job contains `organization_id`, `missing_fields`, `sector_ids`, `model_profile`, `max_pages`, `max_seconds`, `max_tokens`, `allowed_source_classes`, and `completeness_target`. The executor returns fetched-document hashes and candidate claims. Reject claims whose evidence IDs cannot be resolved in the tenant repository.

Update `SKILL.md` to require structured JSON matching the `Claim` schema, to state that aggregate evidence cannot become company metrics, and to stop at the configured budget.

- [ ] **Step 4: Run enrichment and skill validation tests**

Run: `pytest tests/server/lead_research/test_enrichment.py tests/skills -q`

Expected: PASS.

- [ ] **Step 5: Commit enrichment**

```bash
git add server/lead_research/enrichment.py skills/sales/lead-research/SKILL.md skills/sales/lead-research/references/feature-playbooks.yaml tests/server/lead_research/test_enrichment.py
git commit -m "feat(sales): add evidence-bound lead enrichment"
```

---

### Task 11: Research workspace routes, state, and campaign editor

**Files:**
- Create: `server/webui/js/pages/research.js`
- Create: `server/webui/js/pages/research-editor.js`
- Create: `server/webui/js/pages/research-source-picker.js`
- Create: `server/webui/js/pages/research-scoring.js`
- Create: `server/webui/js/pages/research-enrichment.js`
- Create: `server/webui/js/research-state.js`
- Modify: `server/webui/js/main.js`
- Modify: `server/webui/js/shell.js`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/adapters.js`
- Modify: `server/webui/css/app.css`
- Test: `tests/server/test_research_webui.py`

**Interfaces:**
- Produces `/app/research`, `/app/research/new`, and edit routes.
- Consumes server configuration/catalog APIs.

- [ ] **Step 1: Write failing static and API-surface tests**

```python
def test_research_modules_and_routes_are_served(client):
    assert client.get("/js/pages/research.js").status_code == 200
    main = client.get("/js/main.js").text
    assert "/app/research" in main

def test_research_page_has_no_hard_coded_provider_ids(client):
    source = client.get("/js/pages/research-source-picker.js").text
    assert "un-comtrade" not in source
    assert "companies-house" not in source
```

- [ ] **Step 2: Run and confirm missing asset failures**

Run: `pytest tests/server/test_research_webui.py -v`

Expected: FAIL because modules/routes are absent.

- [ ] **Step 3: Implement server-backed editor state and five steps**

```javascript
export function createResearchState({ call, campaign = null }) {
  let current = structuredClone(campaign);
  return {
    get: () => structuredClone(current),
    updateConfig: patch => { current = { ...current, config: deepMerge(current.config, patch) }; },
    save: () => call(current.id ? 'researchCampaigns.patch' : 'researchCampaigns.create', {
      params: current.id ? { campaignId: current.id } : {},
      body: current.id ? { version: current.version, config: current.config } : current.config,
    }),
  };
}
```

Build controls from catalog/config responses. Implement exact weight-total, model availability, source availability, and required-scope validation from the UI guide. Debounce estimates and ignore stale responses with an incrementing request ID.

- [ ] **Step 4: Run web contract tests**

Run: `pytest tests/server/test_research_webui.py tests/server/test_webui.py -v`

Expected: PASS.

- [ ] **Step 5: Commit editor**

```bash
git add server/webui/js/pages/research*.js server/webui/js/research-state.js server/webui/js/main.js server/webui/js/shell.js server/webui/js/api.js server/webui/js/adapters.js server/webui/css/app.css tests/server/test_research_webui.py
git commit -m "feat(web): add lead research campaign editor"
```

---

### Task 12: Campaign detail, lead evidence, and source administration

**Files:**
- Create: `server/webui/js/pages/research-detail.js`
- Create: `server/webui/js/pages/research-evidence.js`
- Modify: `server/webui/js/pages/leads.js`
- Modify: `server/webui/js/pages/admin.js`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/adapters.js`
- Modify: `server/webui/js/mocks/seed.js`
- Modify: `server/webui/js/mocks/handlers.js`
- Modify: `server/webui/css/app.css`
- Test: `tests/server/test_research_webui.py`

**Interfaces:**
- Produces run funnel, source progress, issue review, claim evidence drawer, and source impact/purge UI.

- [ ] **Step 1: Add failing copy and mock-contract tests**

```python
def test_lead_ui_separates_fit_and_evidence_confidence(client):
    source = client.get("/js/pages/leads.js").text
    assert "Evidence confidence" in source
    assert "Fit score" in source

def test_source_admin_uses_distinct_lifecycle_copy(client):
    source = client.get("/js/pages/admin.js").text
    assert "Stops future collection" in source
    assert "Historical evidence" in source
    assert "recalculates affected leads" in source
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/server/test_research_webui.py -v`

Expected: FAIL on missing copy and modules.

- [ ] **Step 3: Implement detail views and destructive impact confirmation**

Render actual ordered funnel counts; source rows preserve independent states. The lead claim drawer shows field, value/range, unit/currency, period, status, confidence, method, evidence links, and retrieval date. Purge first fetches impact, displays counts/storage, then requires the source display name typed exactly.

Mock handlers must return the same resource shapes as real APIs for success, partial, stale, conflict, and purge-impact states.

- [ ] **Step 4: Run web tests**

Run: `pytest tests/server/test_research_webui.py tests/server/test_webui.py -v`

Expected: PASS.

- [ ] **Step 5: Commit detail and admin UI**

```bash
git add server/webui/js/pages/research-detail.js server/webui/js/pages/research-evidence.js server/webui/js/pages/leads.js server/webui/js/pages/admin.js server/webui/js/api.js server/webui/js/adapters.js server/webui/js/mocks server/webui/css/app.css tests/server/test_research_webui.py
git commit -m "feat(web): expose research evidence and source lifecycle"
```

---

### Task 13: CSV exports and end-to-end qualification

**Files:**
- Modify: `server/routes/operations.py`
- Create: `tests/server/lead_research/test_e2e.py`
- Modify: `tests/server/test_api_mvp.py`
- Modify: `server/STATUS.md`
- Modify: `skills/sales/README.md`

**Interfaces:**
- Produces tenant CSVs for campaigns, source metrics, leads, claims, and sector taxonomy.
- Verifies the complete vertical slice.

- [ ] **Step 1: Write failing full-path E2E test**

```python
def test_research_vertical_slice(app, client, tenant_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = install_fixture_provider(client, tenant_headers)
    campaign = create_fixture_campaign(client, tenant_headers, source["id"])
    run = start_and_wait(client, tenant_headers, campaign["id"])
    assert run["status"] == "completed"
    metrics = get_metrics(client, tenant_headers, campaign["id"])
    assert metrics["raw_records"] >= metrics["named_candidates"] >= metrics["qualified_leads"]
    exported = export_research(client, tenant_headers, campaign["id"])
    assert {"fit_score", "evidence_confidence", "source_ids"} <= csv_headers(exported)
    impact = source_impact(client, tenant_headers, source["id"])
    purge_source(client, tenant_headers, source["id"], source["name"])
    assert_recomputed_after_purge(client, tenant_headers, impact)
```

- [ ] **Step 2: Run and confirm failure at export/recompute**

Run: `pytest tests/server/lead_research/test_e2e.py -v`

Expected: FAIL until export and final lifecycle wiring are present.

- [ ] **Step 3: Implement stable CSV schemas and status documentation**

CSV exports begin with stable identity/campaign columns, then fit/confidence, feature claims, and permitted source attribution. Unknown values are empty plus a `<field>_status` column; ranges use `<field>_low` and `<field>_high`; currency and period are separate columns. UTF-8 BOM behavior remains compatible with the existing exporter.

Update product status documentation with source packs, local storage, operational limits, and web routes.

- [ ] **Step 4: Run focused and full server verification**

Run: `pytest tests/server/lead_research tests/server/test_research_webui.py tests/server/test_api_mvp.py tests/server/test_webui.py -v`

Expected: PASS.

Run: `scripts/run_tests.sh tests/server`

Expected: exit 0.

- [ ] **Step 5: Commit the qualified vertical slice**

```bash
git add server/routes/operations.py tests/server/lead_research/test_e2e.py tests/server/test_api_mvp.py server/STATUS.md skills/sales/README.md
git commit -m "test(sales): qualify elite lead research workflow"
```

---

## Final verification gate

- [ ] Run `python -m server.lead_research.sectors --check`; expect exit 0.
- [ ] Run `pytest tests/server/lead_research tests/server/test_research_webui.py -v`; expect all pass.
- [ ] Run `scripts/run_tests.sh tests/server`; expect exit 0.
- [ ] Start the product with a temporary `HERMES_HOME`, create one fixture campaign through the web UI, and verify draft persistence, estimate labeling, run progress, partial-provider state, evidence drawer, CSV export, and source-impact preview.
- [ ] Review browser layout at 320 px, 768 px, and desktop width; verify keyboard-only editor completion and first-error focus.
- [ ] Confirm `git diff --check` produces no output.
- [ ] Confirm `git status --short` contains only intended implementation files.

## Follow-on plan boundaries

After the foundation passes, add country/source packs as independent plans grouped by stable public contracts rather than individual one-off modules:

- Türkiye and neighboring-country official sources;
- EU/UK trade, registry, procurement, and exhibition sources;
- US/Canada official sources;
- Gulf procurement, registry, and event sources;
- licensed company/shipment/financial adapters;
- durable distributed executor and optional analytical materialization.

Each source-pack plan must reuse the shared provider contract, include recorded fixtures, state access/licensing limits, and add no provider-specific branch to the research page.
