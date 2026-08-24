# Lead Research Contract Completion Design

**Status:** Draft for written review; architecture approved in chat on 2026-08-24

**Date:** 2026-08-24

**Authority:** This design implements `lead-research-idea.md`. Where the older
`docs/superpowers/specs/2026-07-14-elite-lead-research-design.md` disagrees,
this document and `lead-research-idea.md` take precedence.

## 1. Outcome

Lead research must give a wholesale or export seller a ranked, explainable list
of businesses that plausibly buy what the seller offers, at the requested scale
and in the selected markets. The seller chooses the importance of the fixed
scoring criteria. The system chooses how to answer those criteria from sector
playbooks, configured structured sources, and bounded agentic research.

The completed workflow is:

1. Determine the authenticated user and tenant company.
2. Load an immutable version of that company's confirmed product profile.
3. Accept one or more target countries and at least one sector, HS code, tenant
   product, or plain product name.
4. Save campaign-specific weights totaling 100 and the exact profile version
   used by the campaign.
5. Select named-company candidates from tenant-private uploads, public
   authoritative sources, licensed sources, and the service corpus.
6. Apply the cheap selection gate before spending on deep research.
7. Resolve each company to one identity, apply eligibility and terminal vetoes,
   research every still-answerable weighted criterion, and cache the result.
8. Compute fit and evidence confidence separately from cited facts.
9. Materialize only in-scope qualified companies into the tenant's lead list,
   stream them into the results UI, and retain named reasons for every drop.
10. Run contact discovery only when the user requests it for a chosen company.

The engine remains sector-neutral. Sector knowledge lives in versioned
playbooks. Adding a sector changes data, not orchestration or scoring code.

## 2. Current-state findings

The current branch already provides useful foundations that must be retained:

- strict campaign, evidence, claim, score, and source models;
- campaign-specific weights totaling 100;
- a pre-indexed candidate corpus with whole-word matching;
- tenant-scoped campaigns, evidence, scores, leads, and suppressions;
- deterministic eligibility, hard-negative vetoes, and identity resolution;
- mechanically declared official and registry authority;
- monotonic claim combination with validated evidence as the anchor;
- separate fit and confidence values;
- source-specific concurrency limits, cancellation, request metering, fresh
  evidence reuse, live result polling, and score-band outcome reporting;
- on-demand contact-discovery and outreach run boundaries.

The following observed behavior contradicts the target contract and drives the
work in this design:

- `EnrichmentProfile.enabled`, its model profile, completeness target, page,
  time, and token budgets are collected but never executed by the campaign.
- `research_each_lead` only repeats the configured web verifier using different
  terms. It is not criterion-aware agentic research.
- Unsupported weighted dimensions are removed from the confidence denominator
  instead of being sent through an agentic path.
- Failed research is not cached as a result.
- Freshness is measured for scoring, but cache reuse expires by source rather
  than by fact.
- Evidence does not require an exact original-language source span, so the
  application cannot reject an invented quotation.
- Stored findings have no canonical-English/original-text contract or archive
  observation date.
- All research evidence and organizations are tenant-scoped, so validated
  public facts cannot compound across customers.
- Candidate corpora have no tenant owner, even though a customer-uploaded list
  must remain private.
- Campaigns do not identify the company-profile version they used.
- The simple campaign brief accepts only a sector; the advanced editor does not
  expose plain product names or a tenant-product picker.
- A campaign with no corpus match is refused before public or licensed candidate
  sources can discover companies.
- Unknown criteria and their share of the user's weight are not returned as a
  first-class score result.
- Contact verification checks syntax and format, not the specified source or
  observed-domain-pattern contract. Unverified contacts may currently enter CC.
- Labels have no complete hidden assignment history or conversion report.

## 3. Scope and boundaries

### In scope

- Company and user tenancy as it affects research ownership and visibility.
- Company onboarding sufficient to produce a versioned research profile.
- Campaign scope, fixed scoring criteria, mutable weights, and cloning.
- Candidate ingestion, visibility, selection, and discovery.
- Public shared facts and tenant-private findings.
- Evidence capture, quote validation, translation, expiry, archive semantics,
  corrections, and negative-search caching.
- Eligibility, deterministic scoring, confidence, verdicts, and lead display.
- Hidden profile labels, admin audit, and outcomes by label and score band.
- On-demand contacts and the green/yellow/red address contract.
- Existing email-template language selection and the CC restriction required by
  contact reliability.
- SQLite and Postgres schema parity, FastAPI routes, and the current vanilla
  JavaScript WebUI.

### Out of scope

- Selecting or purchasing a licensed commercial-data provider. The provider
  interface and source economics report will support that later decision.
- Automatically tuning campaign weights from outcomes. Outcomes are reports.
- Contacting a prospect as part of research or contact verification.
- Logged-in or session-automation scraping of professional networks.
- Making every sector equally rich on day one. The engine and playbook schema
  are general; initial playbook coverage remains deliberately narrow.
- Adding a core Hermes model tool or changing the core tool schema.
- Outbound telemetry or third-party usage attribution.

## 4. Identity, tenancy, and ownership

### 4.1 User and company determination

The authenticated `Principal` remains the authority for user identity.
`company_scope()` remains the only route-level authority for tenant selection:

- a customer principal may access only `principal.company_id`;
- an administrator may select a company with the existing company header;
- background work receives an explicit `company_id` resolved before queueing;
- no model output, campaign payload, candidate row, or URL may choose a tenant.

Campaigns record `created_by` and `updated_by` user IDs for audit. Scores belong
to a company campaign, not to the individual user who clicked Run. Multiple
users in the same company see the same campaign and lead list, subject to their
existing route permissions.

### 4.2 Company research profile

The mutable `company_sections` rows remain the editing surface. Research never
reads those rows as an unversioned live configuration after a campaign starts.

A new immutable `company_profile_versions` record captures:

- legal and display identity, official website, and resolved official domain;
- seller countries;
- product range as tenant product IDs plus canonical English product names,
  HS codes, sectors, and customer emphasis;
- target-market preferences and research exclusions;
- internal labels, their assignment provenance, and the sector playbook version;
- source IDs and evidence IDs used to validate researched profile facts;
- the user confirmations that express private emphasis;
- creation time, creator, confirmation time, and superseding version.

Validated public facts may draft the profile. Credible independent press may
appear as a tenant-private suggestion, but it does not become a validated or
shared fact without official/registry corroboration. Private business emphasis
is confirmed by a user through small contextual onboarding prompts and is never
treated as a public fact.

A campaign stores `profile_version_id` and a compact immutable scope snapshot.
Re-running that campaign uses the same snapshot unless the user explicitly
creates a new campaign version against the latest profile. Historical scores
therefore remain explainable.

### 4.3 Ownership classes

Every research datum belongs to exactly one visibility class:

| Class | Examples | Owner and visibility |
|---|---|---|
| Public shared | Official website fact, registry filing, public tender | Global pool; visible through an in-scope tenant result |
| Tenant-private evidence | Unvalidated web finding, licensed result without redistribution rights | One company |
| Tenant-private candidate | CRM export, uploaded prospect CSV | One company; never enters another tenant's selection |
| Tenant-private decision | Weights, scores, verdicts, lead lists, labels inferred from private input | One company |
| Tenant-private compliance | Address opt-out, do-not-contact state | One company |

The fact that a tenant searched for or uploaded a company is never stored in a
global record. Public facts learned during that work may be promoted to the
shared pool only after mechanical validation.

## 5. Persistent model

The existing tables remain compatible while migrations add the following
boundaries.

### 5.1 Versioned company input

`company_profile_versions`

- `id`, `company_id`, `version`, `status`, `profile`, `source_ids`,
  `evidence_ids`, `playbook_versions`, `created_by`, `confirmed_by`,
  `created_at`, `confirmed_at`, `superseded_at`;
- unique `(company_id, version)`;
- profile rows are append-only after confirmation.

`research_campaigns` gains `profile_version_id`, `created_by`, and `updated_by`.
The campaign JSON remains the full execution snapshot for backward compatibility.

### 5.2 Candidate visibility

`candidate_datasets` gains nullable `owner_company_id` and a constrained
`visibility` value:

- `service_public` for a service-owned or public corpus;
- `tenant_private` for a customer upload;
- `licensed_private` for tenant-licensed data that cannot be redistributed.

`candidate_records` inherit visibility from their dataset. Selection queries
receive `company_id` and may read only `service_public` rows plus rows owned by
that company. Existing rows migrate to `service_public`, because they were
loaded by the service-only CLI and were never tenant uploads.

Customer uploads use an authenticated API and always set `tenant_private`.
The service CLI keeps an explicit service-public import command and gains an
explicit tenant import option. Neither command infers ownership from filenames.

### 5.3 Resolved public identity and fact pool

`shared_organizations`

- one global resolved identity keyed first by registry identifier, then verified
  domain, with normalized name and country as a guarded fallback;
- preserves names, legal forms, addresses, and diacritics as published;
- stores no tenant or campaign relationship.

`shared_facts`

- `id`, `shared_organization_id`, canonical `field`, canonical-English value,
  original value where distinct, period/unit/currency, status, confidence,
  observed time, expiry time, validation basis, correction state, and timestamps;
- only mechanically validated official or catalog-declared registry facts;
- one fact may have several supporting evidence links.

`tenant_facts`

- the same fact shape plus `company_id` and optional campaign ID;
- holds unvalidated findings, license-restricted facts, private upload facts,
  and tenant translations or annotations;
- a later authoritative source promotes a public fact by creating or updating a
  shared fact while preserving the tenant fact's provenance history.

Existing tenant `organizations`, `evidence_records`, and `feature_claims` remain
the campaign materialization during migration. They gain references to the
shared identity/fact where applicable instead of being deleted in one release.

### 5.4 Evidence and search attempts

Evidence records add or carry in their payload:

- immutable fetched content or a content-addressed snapshot reference;
- provenance URL and retrieval method;
- publisher classification and declared authority;
- exact original-language source span;
- extracted canonical-English value;
- source language and optional cached display translations;
- `source_observed_at`, `retrieved_at`, and archive snapshot time;
- raw content hash and quote-validation status;
- field-level expiry derived from the fact type.

`research_search_attempts` records searches that returned no usable evidence:

- resolved organization, field, normalized query class, scope, source/provider,
  attempt time, expiry time, request count, and a named failure reason;
- a public generic field search may be reused globally;
- a query containing tenant product terms, private profile labels, or licensed
  data remains tenant-private;
- an expired failed attempt is researched again; a fresh one returns `unknown`
  without spending again.

### 5.5 Labels, score snapshots, and corrections

`research_label_assignments`

- label, value, scope, source (`system`, `admin`, `outcome_analysis`), setter,
  reason, effective interval, profile version, and timestamps;
- customer APIs never return these rows;
- admin APIs show every assignment and its history.

`research_score_snapshots`

- campaign, result, profile version, weights, dimension values, unknown fields,
  unknown weight, fit, confidence, priority band, evidence/fact IDs, and time;
- append-only for historical explanation even when the live result is refreshed.

`research_fact_consumers`

- maps a shared fact to current tenant results and leads using it;
- source withdrawal or correction enqueues recomputation for every consumer;
- a correction updates the live result but does not rewrite its score snapshots.

## 6. Source and provider contract

A customer-facing source catalog lists only sources whose adapter can execute
for that tenant at that moment. Disabled but runnable sources may be shown as
disabled. Credential-required sources with no credential and catalog-only
stubs are admin setup information, not customer campaign options.

The provider boundary separates three jobs:

```python
class CandidateSource(Protocol):
    definition: DatasetDefinition
    def discover_candidates(self, query, cursor=None) -> CandidatePage: ...

class StructuredFactSource(Protocol):
    definition: DatasetDefinition
    def research_fields(self, company, fields, query) -> ResearchBundle: ...

class AgenticResearchSource(Protocol):
    definition: DatasetDefinition
    def research_gaps(self, request: GapResearchRequest) -> GapResearchResult: ...
```

A provider may implement one or more jobs. Metadata declares:

- named-company discovery capability;
- exact emitted fields;
- countries and sectors;
- public, customer-upload, licensed, or credentialed access;
- field/source freshness and concurrency limits;
- authority and redistribution constraints;
- whether its adapter is executable for the tenant.

Market aggregates and event-only directories never implement
`CandidateSource`. They may provide market context elsewhere, but cannot enter
the lead source list or qualify a named company.

## 7. Campaign creation and onboarding

### 7.1 Minimum onboarding state

Research becomes runnable when the company has:

- a company identity and official website or an explicit admin-confirmed
  identity exception;
- a confirmed profile version;
- at least one seller country;
- at least one product name, product record, sector, or HS code;
- at least one selected target market;
- at least one executable named-company source or a selectable candidate corpus;
- a valid scoring profile totaling 100.

Onboarding presents these as product questions, not engine settings. A user can
say “built-in ovens” without knowing an HS code. Sector and HS mappings are
suggestions backed by the playbook and remain confirmable.

When the user supplies an official website, onboarding queues a bounded profile
research run. It drafts public identity and product-range facts only from
quote-validated official or registry evidence, then asks the user to confirm
private emphasis. The confirmed output creates a profile version; raw model
output never becomes a profile directly.

### 7.2 Campaign input

`CampaignConfig` gains `product_terms: list[str]`. Scope validation accepts any
one of `sector_ids`, `hs_codes`, `product_ids`, or `product_terms`.

Tenant product IDs resolve to their names when the campaign is saved. Those
resolved names are included in the immutable config snapshot and provider
query; later product edits cannot silently alter a historical campaign.

The simple brief and advanced editor both support:

- campaign name and target markets;
- sectors, HS codes, tenant products, and plain product names;
- fixed scoring criteria with five-point weight transfers totaling 100;
- source selection from runnable sources;
- an estimate that distinguishes indexed candidates, discoverable candidates,
  unavailable sources, and terms with no local-language mapping;
- campaign cloning with independent weights and scope.

## 8. Selection and research pipeline

### 8.1 Query construction and language

Canonical storage is English. Discovery queries use the target market's
language.

Sector playbooks gain versioned `market_terms` keyed by ISO country or language.
Each entry maps a canonical English term to local equivalents. Campaign product
terms are retained verbatim and may receive admin-confirmed local equivalents.
The initial built-in-appliance playbook includes the active markets represented
in the repository fixtures; missing mappings are reported and fall back to the
canonical term without claiming full coverage.

Company names, brands, legal forms, and addresses are never translated.

The customer interface renders in the user's saved locale, with Turkish and
English dictionaries for the fixed research vocabulary. Admin defaults to
English and may select Turkish. Fixed criteria, statuses, roles, and categories
use shipped dictionaries. Free-text fact translations are generated once,
stored beside the fact and locale, and reused so the same evidence does not
change wording between visits. The canonical English value and original span
remain the audit source underneath every display translation.

### 8.2 Candidate supply

Candidates are unioned from:

1. tenant-private uploads and CRM imports;
2. service-public indexed corpora;
3. configured public authoritative `CandidateSource` adapters;
4. configured licensed `CandidateSource` adapters.

Candidates deduplicate on resolved identity, not spelling. Before identity is
resolved, verified registry/domain matches outrank guarded name-country matches.
The campaign records how many raw rows collapsed into each resolved company.

### 8.3 Cheap gate

Every candidate is accounted for. Expensive research begins only when one of
these signals passes, in order:

1. the shared validated pool already proves in-scope product or buyer relevance;
2. an indexed corpus row matches the campaign's local-language product range on
   whole-word boundaries;
3. one bounded cheap verification finds identity and scope evidence.

A failed gate produces a named `excluded_by_range` or
`cheap_verification_no_scope_signal` result and increments admin-visible counts.
It does not produce a low-scoring lead.

The gate controls first contact only. Once public validated facts cover the
company, later tenants reuse them without repeating the gate fetch.

### 8.4 Identity and eligibility

Identity resolves before facts enter a reusable pool. Registry ID is strongest,
then verified official domain, then an evidence-backed normalized name-country
match that refuses cross-country or conflicting-domain merges.

Eligibility runs before scoring and returns every gate state. Required failures
produce a rejected research result with named reasons and no lead. Terminal
negatives—closed, dissolved, liquidated, sanctioned when a screening source
actually confirms it, or a seller/manufacturer-only business where the campaign
requires a buyer—veto the assessment regardless of score configuration.

An unchecked compliance condition remains `unknown`; it never becomes a pass.

### 8.5 Gap planning

The gap planner works once per resolved company against the union of:

- shared fresh facts;
- tenant fresh facts;
- structured source capabilities;
- all campaign scoring dimensions with nonzero weight;
- required and useful fields in the applicable sector playbook.

It batches fields by likely page/source so an about page can answer several
gaps in one fetch. It never plans per criterion in isolation.

Structured sources run first. Every remaining nonzero-weight dimension and
required playbook field goes to the agentic source unless a fresh failed-search
record already answers that attempt. No accepted weight is silently removed
because a structured adapter cannot emit it.

### 8.6 Agentic research

The existing `lead-research` skill and durable agent-run service execute bounded
gap research. Campaign mode supplies:

- resolved company identity and official domain;
- compact current facts and their evidence;
- missing fields and sector playbook definitions;
- local-language query terms;
- allowed source policy;
- page, request, wall-time, and token budgets;
- cancellation state.

Campaign mode returns schema-valid claims plus evidence candidates. The
application, not the model, decides validation, quote acceptance, identity,
expiry, score, and sharing.

The run stops on terminal veto, cancellation, complete required coverage,
source exhaustion, or configured budget. It may also stop when the lead is
already in the top priority band and no required evidence is missing. It may
not stop merely because the current score looks low.

Planning and conflict resolution use the campaign's configured decision model.
Literal extraction from an already fetched page uses an optional cheaper
extractor model profile and escalates to the decision model only when extraction
is ambiguous or sources disagree. Both paths return the same evidence contract
and pass the same application quote validator. These profiles are behavioral
tenant configuration, never new environment variables.

The requested gaps control what the agent seeks, not what the store is allowed
to learn. Any incidental schema-known public fact found on an accepted page is
stored with its own provenance and expiry. Campaign projection later selects
only facts relevant to that campaign's range and criteria.

### 8.7 Quote and translation validation

Every observed agentic fact must include an exact span from stored fetched
content. Acceptance requires:

1. the evidence URL and content hash resolve;
2. the original span is a literal substring of the stored original document;
3. the claimed value is represented by that span or explicitly marked as a
   calculated/translated derivation;
4. names and identifiers are unchanged;
5. archive-derived facts carry the archive snapshot date as their observation
   date and are not presented as current.

English extraction occurs in the same agent pass. The original value and span
remain attached. Display translations are cached separately and never affect
validation status.

Rejected evidence produces a named issue and cannot create a fact or score.

### 8.8 Caching and freshness

Each fact field has its own shelf life. Reuse is evaluated per fact, not per
source bundle. A provider fetch may be skipped for fresh fields while being run
for expired fields from the same page.

Failed searches have field-specific expiry. A current failure returns an
explicit unknown claim without spending. Expired failures are attempted again.

An idempotent scheduled refresh job warms facts approaching expiry using the
existing digest/cron infrastructure. It obeys the same source concurrency and
cost ceilings, records its requests separately, and never changes a campaign
score without normal correction/recomputation. Foreground correctness never
depends on the warmup job completing.

## 9. Scoring contract

The criteria remain fixed across sectors:

- product and sector fit;
- buyer and channel fit;
- buying intent;
- market coverage;
- commercial scale;
- relevant trade activity;
- contactability.

Sectors change which facts can answer a criterion, not the criteria themselves.

### 9.1 Evidence-to-dimension combination

- Candidate hints never score.
- Unknown and not-applicable claims never become zero.
- A direct dimension claim is bounded to 0–100.
- Evidence claims earn degree from authority, corroboration, and breadth.
- The strongest validated claim anchors the dimension when one exists.
- Without validated evidence, the strongest unvalidated claim is discounted and
  cannot reach 100.
- Additional independent claims add a bounded share of remaining headroom.
- Adding evidence cannot lower the dimension.
- Conflicts resolve to the validated source; on equal authority the newer source
  wins; only equal-authority, equal-recency disagreement remains conflicted.

### 9.2 Fit and unknown weight

Fit remains the weighted mean over known dimensions:

```text
fit = sum(dimension_score × configured_weight) / sum(known configured weights)
```

Every score also returns:

- `known_weight`;
- `unknown_weight`;
- `unknown_dimensions` with each configured weight;
- `not_applicable_dimensions` where the playbook explicitly says the criterion
  has no meaning for this sector;
- the evidence/fact IDs used for each dimension.

An unknown weighted criterion stays visible even after every research path has
been exhausted. A fit of 92 with 45% unknown weight is valid but cannot be
mistaken for a well-supported 92.

### 9.3 Evidence confidence

Confidence remains a separate 0–1 value based on:

- learned share of applicable configured weight;
- publisher authority;
- independent corroboration;
- per-fact freshness;
- unresolved contradictions;
- estimated rather than observed evidence.

It is never averaged into fit. Priority bands continue to require independent
fit and confidence thresholds saved in the campaign scoring profile.

### 9.4 Verdict and lead materialization

- Ineligible or terminal-negative companies are rejected with named reasons and
  are not materialized as leads.
- A score below all priority bands is rejected as below threshold.
- A top-band lead with authoritative corroborated evidence and no unresolved
  conflict is `strong_fit`.
- Other qualifying bands are `review` and show their missing evidence.
- Only `strong_fit` and `review` results enter the tenant lead list.
- A lead is unique per tenant and resolved company; campaigns create distinct
  result/score snapshots without duplicating the lead.

## 10. Customer and admin surfaces

### 10.1 Customer research brief

The default brief asks only product questions:

- what are you selling—sector, HS code, saved product, or plain product name;
- where do you want to sell;
- what matters most among the fixed criteria;
- which currently runnable sources should this campaign use.

The user never sees internal labels. The UI may ask a contextual emphasis
question when the confirmed profile contains several product families.

### 10.2 Results and evidence

Results stream as each company qualifies. Every row shows fit, confidence,
priority band, unknown weight, and the primary reasons. The detail view shows:

- each criterion and configured weight;
- supporting facts and source links;
- original quoted span and an English rendering when different;
- retrieval/observation dates and archived-state labels;
- unknown and conflicted criteria;
- eligibility and rejection reasons;
- contact-discovery action for a selected lead.

Customers see evidence, never internal profile labels or cross-tenant usage.

### 10.3 Admin oversight

Admin research views add:

- profile versions and every hidden label assignment, setter, reason, and time;
- excluded-by-range and cheap-gate counts;
- shared-fact usage count and correction impact;
- thin-evidence, high-reuse, and sharp-change warnings;
- source requests, cache hits, negative-cache hits, agent tokens, companies
  researched, contacts derived, cancellation, and provider errors;
- conversion by priority band and by internal label.

Admin review never gates the pipeline. One correction action withdraws or
replaces a fact, records the correction, and recomputes every affected current
result and lead.

## 11. Contact discovery and outreach safeguards

Contact discovery remains downstream and on demand.

Stored contacts receive a mechanical `verification_tier`:

- `green`: the exact address was published by a source or supplied by the
  tenant's own records;
- `yellow`: the person's identity and buyer role are sourced, the address was
  derived from an observed company-domain pattern, and the domain accepts mail;
- `red`: no observed pattern, unverifiable person/role, invalid or catch-all
  domain behavior, generic-only result, or any weaker condition.

Generic addresses such as `info@` are retained as fallback channels and marked
red unless a source published the address. A published generic address has a
green *address* tier but a separate `contact_kind=generic` marker, ranks after
named decision-makers, and is never presented as a successful person search.

Discovery never sends verification email. A syntax check may reject an address
but may not promote it to green.

Outreach generation and CC resolution enforce:

- yellow is displayed as bounce risk;
- yellow and red contacts never enter CC;
- red contacts are not selected automatically as the primary recipient;
- tenant suppressions and do-not-contact flags always win;
- one tenant's suppression never changes another tenant's contact record.

Campaign outreach chooses one of the tenant's language-keyed templates. English
facts are composed into the selected recipient language at send-draft time, and
operator-added custom text must declare the same language. Deterministic QA
retains the existing Turkish-character guard and adds language-specific checks
only for languages whose templates are shipped; an unsupported language is
reported rather than silently treated as English.

## 12. Failure, cost, cancellation, and concurrency

Every campaign reports:

- candidates by source and visibility class;
- excluded-by-range and every eligibility rejection reason;
- source requests and retries;
- fresh shared/tenant cache hits and negative-cache hits;
- agentic companies, pages, requests, tokens, elapsed time, and budget stops;
- contact derivations when contact discovery is requested separately;
- source failures and partial partitions.

Cancellation is checked before dispatch, between candidate batches, between
providers, and by the agent executor. No new request starts after cancellation
is observed. In-flight requests may finish and are still metered.

Per-source concurrency limits remain catalog-owned and are applied to discovery,
structured research, and agentic fetches. Database writes stay deterministic and
tenant-scoped even when network work runs concurrently.

A campaign returning no leads must distinguish at least:

- no candidate source was runnable;
- product terms lacked a local-language mapping;
- sources named no candidate;
- candidates were excluded by range;
- candidates failed eligibility or terminal vetoes;
- candidates were researched but fell below thresholds;
- sources failed or the campaign was cancelled.

## 13. API compatibility and migration strategy

Existing `/api/v1/research-campaigns` and lead/contact endpoints remain the
public route family. Responses gain fields; existing fields keep their meaning.

Migration order:

1. add profile-version, candidate-ownership, shared-fact, search-attempt, label,
   score-snapshot, and consumer tables/columns in SQLite and Postgres;
2. mark existing candidate datasets `service_public`;
3. create confirmed profile versions from each tenant's latest approved company
   brain plus current profile/products, recording the migration source;
4. leave existing evidence tenant-private until a new run mechanically validates
   and promotes it—migration must not guess authority;
5. dual-write current tenant campaign materialization and the new fact stores;
6. switch selection, gap planning, and scoring reads to the new boundaries;
7. retain compatibility reads for historical campaigns and exports.

Every Postgres migration is added to `REQUIRED_MIGRATIONS`, `verify.sql`, and
the schema-parity tests in the same change.

## 14. Verification strategy

Implementation follows red-green-refactor. Each behavior receives a failing
test before production code.

Required verification layers:

### Model and scoring tests

- direct product terms satisfy campaign scope and reach provider queries;
- weights total 100 and unknown weight is exact;
- more evidence never lowers a dimension or fit;
- validated claims anchor, unvalidated-only claims remain capped;
- hard negatives veto before scoring;
- fit and confidence remain separate;
- fact-level freshness and conflict precedence are deterministic.

### Storage and isolation tests

- tenant uploads cannot be selected by another tenant;
- service-public candidates are available to all tenants;
- only validated facts enter the shared pool;
- shared facts reveal no originating tenant or campaign;
- unvalidated and licensed-private facts remain tenant-scoped;
- suppressions remain tenant-scoped;
- source correction recomputes every current consumer and preserves snapshots;
- SQLite and Postgres schemas and migrations remain equivalent.

### Evidence and agentic tests

- a source span absent from stored content is rejected;
- original-language text survives English extraction;
- archived facts use snapshot time and are not current facts;
- fresh facts skip research field by field;
- failed searches are cached and expire;
- every weighted unsupported structured dimension reaches the agentic planner;
- page/time/token/request limits and cancellation stop further spending;
- a terminal negative prevents the agentic pass;
- a low current score alone never prunes research.

### Pipeline and E2E tests

- a clean tenant can onboard, confirm a profile, create a campaign using only a
  plain product name, receive a streamed cited lead, and request contacts;
- two tenants reuse one validated public fact but retain independent campaigns,
  weights, scores, leads, uploads, labels, and opt-outs;
- a profile update does not change an old campaign's explanation;
- public candidate discovery can produce a lead when the local corpus has no
  match;
- every zero-result class returns the correct named explanation;
- cancellation and provider failure leave truthful metrics and partial results.

### WebUI tests

- onboarding confirmation and profile-version display;
- sector/HS/saved-product/plain-name scope paths;
- source catalog shows only runnable customer options;
- weights and campaign cloning;
- live results, unknown weight, cited evidence, original/English text, dates;
- labels absent from customer responses and present in admin history;
- green/yellow/red contacts and CC restrictions.

### Final commands

The implementation plan will list focused commands per task. Completion also
requires fresh successful runs of:

- all `tests/server/lead_research` tests;
- research, company, onboarding, contact, outreach, compliance, migration, and
  Postgres-parity server tests;
- research and affected Buyers/Setup WebUI Node tests;
- the clean-tenant lead-research end-to-end test;
- the repository's required full test command if the focused integration set is
  clean.

## 15. Delivery sequence

The implementation plan will divide work into independently testable slices:

1. immutable company profile versions and onboarding readiness;
2. campaign product terms, profile snapshots, and WebUI scope controls;
3. candidate ownership and tenant-private customer uploads;
4. shared identity/fact/evidence schema and mechanical promotion;
5. local-language query mappings and multi-source candidate supply;
6. field-level gap planner, quote validator, negative cache, and durable agentic
   execution, including decision/extractor model routing and scheduled refresh;
7. scoring unknown-weight contract and score snapshots;
8. corrections, hidden label history, and outcome reports;
9. localized lead result/evidence UI and streaming integration;
10. contact reliability tiers and outreach CC safeguards;
11. migration/backfill verification, clean-tenant E2E, and full regression.

Each slice preserves a runnable system. Compatibility bridges may exist during
the sequence, but completion is judged against the final contracts above, not
against the old behavior.

## 16. Resolved and deferred product decisions

Resolved here:

- preserve the current engine and APIs while replacing incorrect boundaries;
- use global public validated facts plus tenant-private overlays;
- use the existing durable agent-run service and `lead-research` skill for gap
  research rather than adding a core tool;
- preserve fixed criteria and campaign-specific weights;
- make direct product names first-class;
- keep contact discovery downstream and on demand;
- make admin review advisory and correction global for shared facts.

Deferred without blocking implementation:

- which licensed trade/company provider to purchase;
- whether outcome reports should eventually recommend weight changes;
- the per-provider commercial price at which a subscription replaces web
  research.

The campaign's existing page, request, time, token, and company limits provide
an explicit deep-pass ceiling until real cost data supports different defaults.
