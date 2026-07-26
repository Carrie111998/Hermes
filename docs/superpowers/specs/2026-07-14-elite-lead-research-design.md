# Elite Lead Research Design

**Status:** Approved design

**Date:** 2026-07-14

**Primary use case:** Find high-precision foreign importers, distributors, retailers, brands, wholesalers, and procurement organizations that are plausible buyers from factories in Türkiye or another configurable seller country.

## 1. Outcome

Build a modular lead-intelligence capability around the existing `lead-discovery`, `lead-research`, and `contact-discovery` skills. The capability must:

- accept a configurable seller country, target countries, sectors, products, buyer types, and datasets;
- combine public government data, registries, trade data, procurement data, exhibition directories, matchmaking platforms, customer uploads, and optional licensed databases;
- retain raw source snapshots locally and normalize every record into shared canonical entities;
- permit datasets to be installed, enabled, disabled, uninstalled, refreshed, or purged independently;
- distinguish market-level signals from named-company evidence;
- enrich named companies with market coverage, trade activity, company value, capacity, locations, store count, and buying intent;
- use a configurable local model to research missing applicable features company by company;
- attach provenance, period, units, method, freshness, and confidence to every claim;
- report defensible source volumes and conversion metrics at every funnel stage;
- generate `sectors.md` and `sectors.csv` from one canonical sector taxonomy;
- expose every behavioral setting in the tenant-scoped web UI.

The implementation adds no permanent model tool. It extends the product at the edge through skills, application services, provider adapters, and existing Hermes model/web capabilities. This preserves the narrow core and prompt-cache stability.

## 2. Scope boundaries

### In scope

- Company-first discovery and qualification.
- Country- and sector-neutral configuration with a Türkiye default.
- Public/open providers included by default.
- Licensed providers loaded only when the customer supplies valid access.
- Evidence-backed company enrichment.
- Local-first storage with tenant isolation.
- Web configuration, run monitoring, source health, results, and auditability.
- CSV export of leads, claims, source metrics, and sector taxonomy.

### Out of scope

- Contact discovery inside the research pipeline; the existing `contact-discovery` skill remains a downstream stage.
- Automated outreach or sending.
- Circumventing login walls, robots controls, rate limits, contracts, or event-platform permissions.
- Inferring named companies from country-level trade totals.
- Claiming precise private-company valuations or company export volumes without defensible evidence.
- Mirroring every global dataset eagerly. The catalog is global; acquisition is lazy or scheduled by configured country and sector.
- A new core Hermes tool or a mutable per-turn system prompt.

## 3. Architectural decision

Use an **evidence graph plus provider registry**.

```text
Campaign configuration
  seller country × target countries × sectors × buyer types × enabled datasets
                                  |
                                  v
Provider registry -> partitioned acquisition -> immutable raw snapshots
                                  |
                                  v
Canonical normalization -> identity resolution -> evidence graph
                                  |
                                  v
Eligibility gate -> structured enrichment -> missing-feature gate
                                              |
                                              v
                                    local-AI web research
                                              |
                                              v
Fit score + evidence confidence -> lead list -> contact discovery
```

The application data plane performs deterministic acquisition, normalization, identity resolution, scoring, storage, and metrics. The agent receives compact evidence bundles and bounded research instructions. It does not receive entire raw datasets in its prompt.

## 4. Product integration

The current split remains load-bearing:

- `skills/sales/lead-discovery/SKILL.md` orchestrates campaign discovery and emits named company candidates with evidence.
- `skills/sales/lead-research/SKILL.md` enriches one resolved company and returns canonical claims, fit, signals, and score inputs.
- `skills/sales/contact-discovery/SKILL.md` finds people only after a company qualifies.
- `server/run_types.py` continues to map `lead_scan` and `lead_research` to their respective skills.
- `server/agent_service.py` remains the run boundary and gains an executor abstraction only when the durable-queue phase requires it.

The new application package is `server/lead_research/`. It is not a model tool and must not edit core Hermes tool schemas.

## 5. Configuration contract

Behavioral configuration is tenant-scoped application data, not a new `.env` contract. Secrets remain in the existing secret/integration storage.

### Campaign configuration

```json
{
  "name": "DACH appliance distributors",
  "seller_countries": ["TR"],
  "target_countries": ["DE", "AT", "CH"],
  "sector_ids": ["household-appliances"],
  "hs_codes": ["8418", "8516"],
  "product_ids": ["product_123"],
  "buyer_types": ["importer", "distributor", "retailer", "brand"],
  "enabled_source_ids": ["un-comtrade", "ted", "auma", "companies-house"],
  "precision_profile": "high_precision",
  "max_qualified_leads_per_country": 50,
  "freshness_days": 180,
  "exclusions": {
    "company_ids": [],
    "domains": [],
    "seller_only": true,
    "sanctioned_entities": true
  },
  "scoring_profile_id": "default-high-precision",
  "enrichment_profile_id": "local-balanced",
  "refresh_policy_id": "monthly-active"
}
```

`seller_countries` is plural because regional factories may be searched together, but the default is `["TR"]`. `target_countries` remains bounded per run; larger selections are split into child partitions.

### Configuration precedence

```text
system-safe defaults
    < tenant defaults
    < saved campaign template
    < current campaign overrides
```

The web UI shows the effective value and its origin. There are no hidden model-only thresholds.

## 6. Provider registry and dataset lifecycle

Every provider is registered through declarative metadata and a narrow interface.

```python
class Provider(Protocol):
    definition: DatasetDefinition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate: ...
    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage: ...
    def normalize(self, record: RawRecord, snapshot: SnapshotRef) -> list[EvidenceRecord]: ...
    def checkpoint(self, page: RawPage) -> str | None: ...
    def health(self) -> ProviderHealth: ...
```

`DatasetDefinition` contains:

- stable `source_id`, version, display name, publisher, jurisdiction, categories, and homepage;
- access tier: `public`, `credentialed_public`, `licensed`, `customer_upload`, or `retired`;
- entity capabilities: market signals, organizations, company trade, procurement, events, exhibitors, attendees, buying requests, or financials;
- geographic and sector coverage;
- rate-limit, pagination, retention, redistribution, and credential metadata;
- freshness expectations and health status;
- adapter package and schema version.

### Dataset operations

- **Install:** Register definition and adapter without automatically enabling it.
- **Enable:** Permit it in new campaigns.
- **Disable:** Stop new acquisition; keep existing snapshots and provenance.
- **Uninstall:** Remove adapter availability; retain metadata and historical evidence.
- **Purge:** Explicit destructive operation that removes raw and normalized evidence after an impact preview and confirmation.
- **Refresh:** Create a new immutable snapshot; never overwrite the prior snapshot.

Removing evidence triggers identity, eligibility, claim, and score recomputation. A lead supported by other evidence survives. A lead with no remaining qualifying evidence becomes `unqualified_after_source_removal`, not silently deleted.

## 7. Source catalog strategy

Source availability changes, so each entry has `active`, `degraded`, or `retired` health and a last-verification timestamp. The catalog is data, not hard-coded UI conditionals.

### Verified public and official foundations

| Category | Source | Main contribution | Entity level | Access |
|---|---|---|---|---|
| Global trade | [UN Comtrade API](https://uncomtrade.org/docs/un-comtrade-api/) | HS trade value/quantity by reporter, partner, period | Market signal | Public/credentialed |
| Global trade | [World Bank WITS API](https://wits.worldbank.org/witsapiintro.aspx?lang=en) | Trade and tariff context | Market signal | Public/credentialed |
| European trade | [Eurostat Comext API](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-getting-started/comext-database) | EU detailed trade flows | Market signal | Public |
| Türkiye trade | [TÜİK Data Portal](https://veriportali.tuik.gov.tr/en/press/53911) | Official Turkish trade statistics | Market signal | Public |
| Türkiye capacity | [TOBB Industry Database](https://sanayi.tobb.org.tr/kapasite_sss.php) | Industry/capacity context where published | Market or organization signal | Public site |
| Türkiye fairs | [TOBB Fair Calendar](https://fuarlar.tobb.org.tr/FuarTakvimi) | Event discovery | Event | Public site |
| Türkiye buyer missions | [Ministry of Trade buyer missions](https://ticaret.gov.tr/haberler/ticaret-bakanligindan-ihracata-yonelik-onemli-hamle-ozel-nitelikli-alim-heyetleri-ile-kuresel-alicilar-turk-ihracatcilariyla-bulusuyor) | Hosted buyers and sourcing events when published | Event/opportunity | Public site |
| EU procurement | [TED Search API](https://docs.ted.europa.eu/api/latest/search.html) | Tenders and awarded buyers | Opportunity/organization | Public API |
| EU partnering | [Enterprise Europe Network opportunities](https://een.ec.europa.eu/partnering-opportunities) | Sourcing and partnership requests | Opportunity/organization | Public site |
| UK registry | [Companies House API](https://developer.company-information.service.gov.uk/) | Legal identity, status, officers, filings | Organization | Credentialed public API |
| UK trade | [DBT API](https://data.api.trade.gov.uk/) | UK trade context | Market signal | Public API |
| UK procurement | [Contracts Finder](https://www.data.gov.uk/collections/government/contracts-finder) | Contract opportunities and awards | Opportunity/organization | Public |
| US trade | [US Census International Trade API](https://api.census.gov/data/timeseries/intltrade/exports/hs.html) | HS trade flows | Market signal | Public API |
| US filings | [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | Public-company filings and financials | Organization/claim | Public API |
| US procurement | [SAM.gov](https://sam.gov/contracting) | Contract opportunities and entities | Opportunity/organization | Public/credentialed |
| US spending | [USAspending API](https://api.usaspending.gov/docs/endpoints) | Awards and buyer/supplier history | Opportunity/organization | Public API |
| Canada trade | [Canadian International Merchandise Trade](https://www150.statcan.gc.ca/n1/en/catalogue/71-607-X2021004) | Canadian merchandise trade | Market signal | Public |
| Australia statistics | [ABS Data API](https://www.abs.gov.au/statistics/application-programming-interfaces-apis/data-api-user-guide) | Official market and trade context | Market signal | Public API |
| Saudi procurement | [Etimad API Portal](https://apiportal.etimad.sa/en/docs/introduction) | Procurement opportunities and awards | Opportunity/organization | Credentialed public API |
| UAE procurement | [UAE Digital Procurement Platform](https://mof.gov.ae/en/public-finance/government-procurement/digital-procurement-platform/) | Federal procurement | Opportunity/organization | Public/credentialed |
| UAE statistics | [Federal Competitiveness and Statistics Centre](https://fcsc.gov.ae/) | Official market context | Market signal | Public |

### Exhibition and buyer-intent layers

| Layer | Sources | Qualification rule |
|---|---|---|
| Event discovery | [AUMA fair finder](https://www.auma.de/en/find-your-fair/), TOBB, EEN events, [US ITA events](https://www.trade.gov/attend-event) | Event existence is not a lead signal by itself. |
| Official exhibitor/product directories | Organizer-owned directories such as [Messe Düsseldorf/interpack](https://interpack.messe-dus.co.jp/visitors/database) | Exhibitor status identifies a company and product context, not buyer intent. |
| Matchmaking | [b2match data exports](https://support.b2match.com/exporting-event-data), [Swapcard APIs](https://swapcard.dev/), [Grip data warehouse](https://support.grip.events/data-warehouse) | “Looking for,” meeting goals, hosted-buyer, or sourcing-request fields are explicit intent when access permits. |
| Buyer missions | Government and chamber buyer delegations | Named attendee plus sourcing brief is strong evidence. |
| Customer-owned event data | Customer exports from organizers, CRM, badge scans, or meeting systems | Use only within tenant and contractual retention boundaries. |

[JETRO J-messe](https://www.jetro.go.jp/en/database/j-messe/user/login.html) is cataloged as retired because its service notice states discontinuation on 2026-03-31. Retired sources remain visible so campaigns and historical provenance stay understandable.

### Optional licensed adapter classes

Company, contact, shipment, financial, and event-directory vendors are installable adapters. They are never assumed available and never bundled with scraped credentials. Before activation, the catalog must record the customer's license, allowed fields, retention, redistribution, and API terms. A licensed source may improve named-company coverage but cannot bypass canonical evidence or confidence rules.

## 8. Canonical sector taxonomy

The source of truth is:

```text
skills/sales/lead-research/references/sectors.yaml
```

Generated artifacts are:

```text
skills/sales/lead-research/references/sectors.md
skills/sales/lead-research/references/sectors.csv
```

`sectors.yaml` contains, per sector:

```yaml
- sector_id: household-appliances
  name: Household appliances
  aliases: [home appliances, white goods]
  hs_2022: [8418, 8422.11, 8450, 8516]
  nace_rev2: [C27.5]
  naics_2022: [335220, 423620]
  cpv_2008: [39700000]
  cpc: []
  buyer_types: [importer, distributor, retailer, brand, wholesaler]
  applicable_features: [store_count, countries_served, relevant_import_value, brands_carried]
  sourcing_terms: [OEM, private label, distributor wanted]
  default_source_categories: [trade, registry, procurement, exhibition, web]
```

The CSV flattens lists with semicolon separators and includes a taxonomy version. Generated files must be deterministic and must fail CI when stale.

## 9. Canonical evidence model

Every normalized record uses a common envelope:

```python
class EvidenceEnvelope(BaseModel):
    evidence_id: str
    source_id: str
    source_record_id: str
    snapshot_id: str
    record_type: Literal[
        "organization", "market_signal", "company_signal",
        "event", "opportunity", "lead_candidate"
    ]
    observed_at: datetime | None
    retrieved_at: datetime
    jurisdiction: str | None
    sector_ids: list[str]
    provenance_url: str | None
    raw_hash: str
    method: Literal["observed", "calculated", "estimated_range"]
    confidence: float
    payload: dict
```

Typed entities are:

- `Organization`: legal/display names, registry IDs, domains, addresses, roles, status.
- `MarketSignal`: aggregate trade, market size, tariffs, country-sector trend; never a lead.
- `CompanySignal`: company-specific trade, financial, capacity, location, product, or intent evidence.
- `Event`: organizer, dates, location, sectors, event identifiers.
- `Opportunity`: procurement notice, buying request, partnership request, meeting intent.
- `LeadCandidate`: resolved organization plus eligibility evidence and campaign relevance.

### Claim contract

Every lead feature is a claim with:

```text
field, value or range, unit, currency, period, status, confidence,
method, source evidence IDs, verified_at, applicability
```

Allowed statuses are `observed`, `calculated`, `estimated_range`, `conflicted`, `unknown`, and `not_applicable`.

Financial concepts remain separate:

- `market_cap` for a publicly traded company on an observation date;
- `reported_company_valuation` when a transaction or filing supports it;
- `estimated_company_value_range` only with a documented method;
- `addressable_market_value` for a country-sector market signal.

The UI and exports must not label these all as “market value.”

## 10. Local storage and tenant isolation

Resolve the root through `get_hermes_home()` and place tenant data under:

```text
<HERMES_HOME>/lead-research/<company_id>/
  catalog.sqlite
  raw/<source_id>/<snapshot_id>/*.jsonl.gz
  exports/
  cache/web/<content_hash>.html.gz
```

SQLite stores provider definitions, campaigns, snapshots, canonical organizations, identity links, evidence, claims, metrics, and job checkpoints. Raw pages and web documents remain immutable compressed files referenced by hashes. Optional Parquet materialization is a later analytical adapter, not a foundation dependency.

Public source downloads may use a shared content-addressed cache only when license terms permit. Licensed and customer-uploaded datasets are always tenant-isolated. Secrets are never written into snapshots, logs, claim provenance, or exports.

## 11. Acquisition and normalization pipeline

Each campaign expands into bounded work partitions:

```text
target country × sector × provider × cursor/window
```

The initial executor is a bounded local worker pool. Each provider has independent concurrency, timeout, retry, and rate-limit settings. The executor contract permits a later durable queue without changing provider or evidence contracts.

Pipeline stages are deterministic:

1. Validate campaign and effective source configuration.
2. Ask providers for a discovery estimate.
3. Acquire pages and save immutable snapshots.
4. Validate raw record shape and quarantine drifted pages.
5. Normalize to canonical evidence.
6. Resolve organization identity.
7. Deduplicate evidence and organizations.
8. Apply eligibility gates.
9. Enrich structured features.
10. Queue local-AI research for missing priority features.
11. Calculate fit and confidence.
12. Persist funnel metrics and emit qualified leads.

Ingestion is idempotent on `(source_id, source_record_id, snapshot_id, raw_hash)`. A resumed job continues from provider checkpoints.

## 12. Identity resolution

Identity resolution uses evidence, not fuzzy name matching alone.

Strong identifiers:

- official registry identifier plus jurisdiction;
- verified domain;
- VAT/tax identifier;
- LEI or market identifier;
- organizer/platform company identifier within a source.

Supporting identifiers:

- normalized name and address;
- telephone domain/country;
- official social profile;
- parent/subsidiary relationship.

Ambiguous matches remain separate and enter an identity-review queue. Merges are reversible and preserve source records. Dataset removal can therefore detach evidence without corrupting the organization.

## 13. Lead-feature enrichment

Applicable feature families:

- identity and scale;
- market coverage;
- import/export activity;
- commercial and physical capacity;
- public financial value or documented private value range;
- store, facility, warehouse, and office counts;
- procurement and buying intent;
- product, brand, certification, OEM, and private-label fit.

Structured sources run first. A completeness gate then compares present claims with the sector/company-type feature playbook. Missing non-applicable features do not count against completeness.

### Local-AI research worker

The worker receives one resolved company, its evidence bundle, missing-field brief, sector playbook, and budget. Retrieval prioritizes:

1. official company website and store locator;
2. regulators, registries, and stock-exchange filings;
3. annual reports and investor documents;
4. official procurement and event pages;
5. credible news and independent directories.

The local model extracts only schema-valid claims with evidence references. Unsupported numeric claims are rejected. Pages are cached by content hash and timestamp. The worker stops on the completeness target, page/time/token budget, or source exhaustion.

The model profile is selected through existing Hermes configuration and the web UI. Ollama or another supported local provider can be used without changing skill or provider contracts.

## 14. Eligibility and scoring

Before scoring, a company must pass:

- resolved named identity;
- target geography;
- product/sector relevance;
- plausible buyer role;
- campaign exclusion and compliance rules.

Default fit weights:

| Dimension | Weight |
|---|---:|
| Product and sector fit | 25 |
| Buyer and channel fit | 20 |
| Buying intent | 15 |
| Market coverage | 15 |
| Commercial scale and capacity | 10 |
| Relevant trade activity | 10 |
| Contactability | 5 |

Weights are campaign-configurable and must total 100. Applicability is sector-aware. `fit_score` (0–100) and `evidence_confidence` (0–1) stay separate in storage, UI, API, and exports.

Priority bands:

- **A:** strong fit and strong evidence;
- **B:** promising fit with enrichment or review required;
- **C:** weak fit or material uncertainty;
- **Rejected:** failed eligibility gate.

An estimated range cannot contribute more than its dimension cap multiplied by its confidence. Market-level signals can affect market attractiveness but cannot establish company buying intent.

## 15. Metrics and volume estimates

Persist this funnel per campaign, country, sector, provider, and snapshot:

```text
raw records
-> named candidates
-> resolved organizations
-> unique eligible companies
-> qualified leads
-> contactable leads
```

### Acquisition metrics

- records/pages acquired;
- bytes downloaded;
- source-reported total where available;
- duplicates and unchanged records;
- elapsed time, errors, retries, and cache hit rate;
- freshness and snapshot age.

### Quality metrics

- identity resolution rate;
- precision@K against reviewed samples;
- user acceptance/rejection rate;
- corroboration and conflict rate;
- applicable-field completeness;
- numeric-evidence rate;
- false-positive rate and rejection reasons.

### Source economics

- named candidate yield;
- qualified and accepted lead yield;
- time and model usage per qualified lead;
- licensed cost per qualified and accepted lead;
- survival rate after refresh or source removal.

### Volume reporting

Before a run, show a range derived from source-reported counts, catalog coverage, freshness, and historical conversion rates. Label it `estimate`, include the method, and suppress it when history is insufficient. After a run, show actual values at every funnel stage. Never present an aggregate trade row count as expected named leads.

## 16. Failure handling

- Provider failure is isolated to its partition.
- Retries use bounded exponential backoff and honor server guidance.
- Repeated failures open a circuit breaker and mark the provider `degraded`.
- Checkpoints permit safe resume.
- Schema drift quarantines the affected snapshot and keeps the last valid snapshot as `stale`.
- Partial campaigns complete with an explicit source-coverage report.
- Identity conflicts and claim conflicts enter review queues.
- Local-model unavailability skips AI enrichment but preserves structured results.
- Dataset purge requires an impact preview and typed confirmation.
- Retired providers cannot be selected for new campaigns but remain visible in historical runs.

## 17. Web product contract

The primary route is `/app/research`. It replaces the current scan-only modal with a persistent research workspace while keeping the map as an optional target-market selector.

All configurable items in this design must be editable or inspectable in the web UI according to [research-page-UI-guidelines.md](../../research-page-UI-guidelines.md).

Admin source management at `/admin/data-sources` owns installation, credentials, health, licensing, and purge. Tenant campaign users choose among enabled sources but cannot alter platform credentials or destructive retention settings unless authorized.

## 18. Security, compliance, and licensing

- Tenant scope is enforced at every API and storage boundary.
- Provider credentials use secret storage and are returned to the browser only as masked status.
- Retrieval honors access controls, rate limits, terms, robots policy, and customer licenses.
- Logs contain provider IDs and safe error categories, not credentials or restricted record bodies.
- Customer-owned event data is never pooled across tenants.
- Every exported claim includes source attribution and retrieval date subject to redistribution rights.
- Sanctions and do-not-contact policies remain separate gates; research does not authorize outreach.

## 19. Test strategy

### Contract tests

Every provider adapter runs the same fixture suite for pagination, checkpointing, health, normalization, idempotency, and drift quarantine.

### Behavioral tests

- Aggregate signals cannot create a lead.
- Disabling a source blocks new acquisition but retains evidence.
- Purging a source withdraws only its evidence and recomputes dependent results.
- Identity merges are reversible.
- All score weights total 100 and non-applicable fields do not penalize a company.
- A numeric AI claim without evidence is rejected.
- Pre-run estimates are labeled and post-run metrics are actual.
- Tenant A cannot read tenant B datasets, snapshots, claims, or web cache.

### End-to-end tests

Using a temporary `HERMES_HOME`, execute:

```text
create campaign -> acquire fixture provider -> normalize -> resolve -> qualify
-> enrich -> score -> export -> disable/purge provider -> recompute
```

The web test exercises configuration, validation, save/resume, run monitoring, partial failure, results filtering, evidence inspection, and CSV export.

## 20. Rollout

### Phase 1: Foundation

Canonical schemas, sector generator, provider registry, tenant-local storage, snapshots, identity resolution, metrics, and research-page configuration shell.

**Exit:** fixture provider completes the full pipeline idempotently; sector Markdown and CSV are deterministic; all configuration round-trips through the UI.

### Phase 2: Public-source pack

Priority aggregate trade, registry, procurement, exhibition, and opportunity adapters for Türkiye, the EU, UK, US, and selected surrounding/target markets.

**Exit:** at least one market, named-company, opportunity, and exhibition source run through the shared contract; provider failure produces a valid partial campaign.

### Phase 3: Enrichment and scoring

Company claims, feature playbooks, local-AI worker, evidence inspection, scoring profiles, refresh scheduler, and reviewed calibration set.

**Exit:** unsupported numeric claims fail validation; score/confidence separation is visible end to end; precision@K is measurable.

### Phase 4: Scale and licensed sources

Durable queue executor, additional country packs, licensed adapters, optional analytical materialization, and source-cost analytics.

**Exit:** restart-safe distributed execution without provider-contract changes; licensed evidence remains tenant-isolated and removable.

## 21. Critical review

### Why this plan is good

- **Evidence before narrative:** Every result can be traced to source records, snapshots, methods, periods, and confidence.
- **No category error:** Aggregate trade data informs markets but cannot masquerade as named companies or company export volume.
- **Modular and removable:** Providers share one contract; enabling, disabling, uninstalling, and purging have defined semantics.
- **Country and sector neutral:** Seller country, targets, sector codes, buyer types, and source sets are configuration rather than code branches.
- **Local and reusable:** Raw evidence, normalized claims, and web snapshots are cached locally and reused across campaigns when licensing permits.
- **Precision is measurable:** Funnel metrics, precision@K, acceptance, conflict, completeness, and cost make source quality visible.
- **AI is bounded:** Deterministic datasets run first; the local model researches only missing applicable fields and cannot silently invent numeric facts.
- **Hermes-compatible:** The design extends skills and application services without growing the permanent core tool schema or destabilizing prompt caching.
- **UI-complete:** Behavioral configuration is visible in the web product rather than buried in files or model prompts.

### Why it is not perfect

- **Official data is uneven:** Many government trade datasets are aggregate, delayed, differently classified, or unavailable at company level.
- **Company metrics may remain unknown:** Private valuation, purchasing capacity, export value, and precise store counts often lack defensible public evidence.
- **Exhibition intent is access-sensitive:** Participant and matchmaking data may require organizer permission, customer exports, or paid platform access.
- **Identity resolution is probabilistic:** Corporate groups, reused names, translated names, franchise networks, and domain changes create unavoidable review cases.
- **Local models vary:** Extraction quality, language coverage, and citation discipline depend on the chosen model and hardware.
- **Web sources drift:** Site structure, APIs, licenses, robots policies, and service availability require ongoing catalog health and adapter maintenance.
- **Global breadth is incremental:** A global catalog is realistic; equal depth across every country and sector on day one is not.
- **Storage and refresh have costs:** Snapshots, licensed data, repeated web retrieval, and large campaigns require retention and budget controls.
- **Scores encode business judgment:** Default weights are defensible starting points, not universal truth. They require campaign-specific calibration and user feedback.
- **Human review remains valuable:** High-value leads, conflicts, ambiguous identities, and estimates benefit from evidence inspection before outreach.

These imperfections are made explicit rather than hidden. The architecture records unknowns, conflicts, access limits, and source health so the system can improve without breaking its evidence contract.
