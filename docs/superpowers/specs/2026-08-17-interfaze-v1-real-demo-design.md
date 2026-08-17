# Interfaze v1 Real Demo Design

**Date:** 2026-08-17

**Status:** Approved design

**Scope:** Clean Silverline demo tenant, real evidence-backed lead research, and a review-focused WebUI

## Objective

Release an honest v1 demo in which Silverline begins with no leads or research history. The user imports Silverline's product catalog, selects target countries, and starts a real research run. Interfaze uses a private candidate corpus plus live web evidence to find, validate, score, and explain potential buyers. No production path may substitute mock or seeded operational data.

## Product boundary

The v1 workflow is:

1. Log in to a pre-provisioned Silverline workspace.
2. Review the completed company onboarding profile.
3. Import Silverline's product catalog through the WebUI.
4. Select target countries through the WebUI.
5. Configure evidence-based fit weights.
6. Run research.
7. Review Strong fit and Review results, their verdicts, and their sources.
8. Open rejected candidates in a separate Rejected tab when needed.
9. Export the selected result class.

Outreach is outside v1. The research workflow will not add email drafting, sequences, mailbox connection, approval, or sending controls.

## Data boundaries

### Silverline tenant data

The demo tenant contains only:

- the Silverline organization;
- verified Silverline company-onboarding facts;
- completed company onboarding state;
- one demo user with email `efe@anexa-arelvia.com`;
- Silverline products imported later through the WebUI; and
- research configuration created by the demo user.

The initial tenant contains no:

- leads or contacts;
- target-country selections;
- research campaigns, runs, snapshots, or verdicts;
- messages, drafts, conversations, sequences, or approvals;
- outreach or delivery history; or
- imported historical Silverline lead lists.

Silverline's current real-world leads are not integrated. The demo must behave as if Silverline has no leads before its first research run.

### Silverline product catalog

Silverline's product catalog is tenant-owned business data imported through the WebUI. It is not bundled seed data and is not part of initial account provisioning.

The import path must validate the supplied format, report row-level errors, avoid partial silent imports, and preserve enough source metadata to identify the imported file and import time. The catalog provides the product terms used to build research queries and calculate product-category fit.

### Shared candidate corpus

The kitchen-appliance database supplied by the user is a private backend candidate source. It may include countries and named companies, but those records are not Silverline leads and must not appear in Silverline's lead or result tables merely because the corpus was imported.

The candidate corpus must be stored outside tenant operational lead records and retain:

- source dataset identity and version;
- source record identifier;
- company name and available aliases;
- country and available market metadata;
- domain or other identity hints when supplied; and
- supplied category or relationship fields without treating them as verified facts.

Only a user-initiated research run may select corpus candidates. A selected candidate becomes a visible Silverline research result only after identity resolution, eligibility checks, and evidence-backed evaluation. Importing or updating the corpus creates zero Silverline leads.

## Account provisioning

An explicit, idempotent operational command provisions the Silverline organization, completed company onboarding, and the demo user. It is a provisioning operation, not a seed command.

The password is supplied at deployment time as a secret, hashed immediately, and never written to source control, example configuration, logs, command output, or this specification. The provisioning command must not accept a plaintext password in a form likely to remain in shell history or process listings.

Provisioning must:

- create missing identity and onboarding records;
- safely converge when the same clean account already exists;
- leave target countries empty;
- create no operational research or outreach data; and
- fail visibly if the target tenant already contains operational data instead of deleting it.

Verified public Silverline onboarding facts retain their source URLs and retrieval dates. Exact facts are gathered after live web credentials are available; no missing fact is invented.

## Research architecture

### Provider-neutral core

Interfaze owns the provider-neutral discovery, evidence, identity-resolution, scoring, and verdict contracts. Bright Data is an optional, service-gated provider at the edge, not a permanent requirement of the core agent tool schema.

Provider failures must not alter the research contract. Official registries, trade sources, manual imports, or future providers can implement the same interface.

### Bright Data adapter

Bright Data supplies broad live-web discovery and page retrieval. Its credentials are deployment secrets. A real credential and quota smoke test is required before claiming live-demo readiness.

For this workflow the adapter may:

- discover public pages relevant to selected countries and Silverline products;
- retrieve candidate company pages, directories, registries, and other allowed public sources;
- handle JavaScript-heavy or bot-protected pages; and
- return content with stable provenance for the evidence pipeline.

The adapter does not decide whether a company is a lead. It only discovers or retrieves source material.

### Research data flow

For each research run:

1. Build a bounded brief from Silverline's imported products, selected countries, buyer types, exclusions, and scoring profile.
2. Select matching records from the backend candidate corpus without creating tenant leads.
3. Use enabled live and authoritative providers to discover and validate candidate companies.
4. Capture immutable evidence snapshots with source URL, source identifier, retrieval time, and content hash.
5. Resolve legal names, domains, aliases, and duplicate candidates into organization identities.
6. Apply non-overridable eligibility gates.
7. Extract evidence-backed claims and record unknown, stale, or contradictory claims explicitly.
8. Calculate fit score and evidence confidence separately.
9. Produce a deterministic verdict with human-readable reasons.
10. Persist the visible result only after the candidate has passed the required pipeline stages.

Aggregate market data may support market context but may not qualify a named company by itself.

## Scoring

### Fit and confidence remain separate

Fit measures business relevance according to user-controlled dimension weights. Evidence confidence measures the authority, corroboration, freshness, completeness, and conflicts in the supporting material. A high fit score cannot compensate for weak evidence.

Every dimension score must be derived from evidence claims or remain unknown. Production scoring must remove hard-coded fallback dimension values.

### Fixed-budget weight controls

The scoring-weight budget is always exactly 100 points.

- Each dimension is between 0 and 100.
- Every weight is a multiple of 5.
- The UI uses `-5` and `+5` buttons; it does not expose one-point adjustment.
- Pressing `+5` deducts 5 from the highest-weighted eligible other dimension.
- Pressing `-5` credits 5 to the lowest-weighted eligible other dimension.
- Ties use the stable scoring-dimension order.
- A transfer is blocked only when no other dimension can legally absorb it.
- Both changed controls are visually highlighted and announced accessibly.
- The client and server validate the same invariants.

The deterministic transfer rule prevents invalid intermediate totals and makes every change explainable.

## Verdicts

The verdict is deterministic policy applied to resolved identities, eligibility gates, fit, confidence, and evidence claims. A language model may extract claims or generate the concise explanation, but it cannot independently invent or override the verdict.

### Strong fit

Requires:

- resolved company identity;
- presence in a selected target country;
- a supported buyer, importer, distributor, retailer, buying-group, procurement, or applicable project-supply role;
- relevant product-category evidence;
- at least two independent supporting sources; and
- at least one official or first-party source.

### Review

Used for a promising candidate whose evidence is incomplete, stale, low-authority, or contradictory. The UI must state which evidence is missing or conflicting.

### Reject

Used for a wrong category or geography, manufacturer or competitor rather than buyer, inactive business, duplicate entity, failed eligibility gate, or an entity whose qualifying claims cannot be verified.

The verdict and fit band may be stored separately so policy changes do not erase the underlying score and evidence.

## Research-results WebUI

The approved information hierarchy is:

1. active research brief;
2. compact run and coverage summary;
3. filterable company result list; and
4. selected-company evidence panel.

The default results view shows Strong fit and Review companies only. Rejected candidates do not appear in the initial count, default list, or standard export. A separate Rejected tab is loaded when selected and shows the rejection reason and evidence. If rejected export is supported, it is an explicit export from that tab.

Each visible result shows:

- verdict and concise reason;
- fit score and the evidence-derived dimension breakdown;
- evidence confidence and freshness;
- buyer role and selected-country match;
- supporting and conflicting claims;
- missing evidence;
- source links and snapshot metadata; and
- research timestamp.

The results view is a research-review workspace, not a CRM. It contains no outreach controls.

Empty states must distinguish among no research run, no qualified results, incomplete source coverage, and a failed run.

## Failure and rerun behavior

A research run ends as `succeeded`, `partial`, or `failed`.

- Partial runs retain valid findings and identify uncovered countries, unavailable sources, and failed partitions.
- Provider authentication, quota, timeout, blocked-source, and parsing failures remain visible.
- A failed provider never creates a placeholder company or fabricated claim.
- Unknown claims remain unknown and reduce completeness or confidence as defined by policy.
- Re-runs reuse eligible snapshots according to retention and freshness rules.
- Identity resolution prevents duplicate visible results across corpus and live-discovery paths.
- Failed or interrupted runs remain inspectable and retryable by bounded partition.
- No runtime failure may activate fixtures, mock stores, or synthetic results.

## Removal of mock and seed behavior

The shipped production runtime must not expose or import operational demo data.

Implementation must remove or isolate from production packaging:

- the demo seed command and Silverline demo-result payloads;
- bundled company packs that create Silverline operational data;
- production WebUI imports of the mock database or mock handlers;
- dormant mock adapters in production entry points;
- automatic business-record creation during application startup; and
- the fixture research provider from the production provider registry.

Tests may retain purpose-built fixtures under explicit test configuration. Production configuration must have no switch that silently selects those fixtures.

## Testing strategy

### Provisioning and isolation

- Provision a clean database and assert completed company onboarding, one intended demo user, empty target countries, and zero operational records.
- Run provisioning twice against a clean account and assert safe convergence.
- Assert provisioning refuses to wipe an account containing operational data.
- Verify the secret value is absent from tracked files and logs.
- Import the shared candidate corpus and assert that Silverline still has zero leads and research results.

### Catalog and candidate inputs

- Verify WebUI product imports are tenant-scoped, validated, and auditable.
- Verify malformed product or candidate files return actionable row-level errors.
- Verify candidate-corpus versioning and deduplication.
- Verify corpus imports cannot write directly to tenant lead tables.

### Scoring and verdict policy

- Use behavior tests for all weight-transfer invariants, including ties and 0/100 boundaries.
- Assert every successful adjustment changes exactly two dimensions by 5 and preserves a total of 100.
- Assert missing dimension evidence remains unknown rather than receiving a default score.
- Cover Strong fit, Review, and Reject with authoritative, stale, conflicting, insufficient, and duplicate evidence.
- Assert aggregate market signals cannot qualify a named lead.

### Provider and evidence pipeline

- Exercise provider contracts, normalization, snapshot hashing, identity resolution, and partition retries with isolated deterministic fixtures.
- Run at least one credential-gated real Bright Data smoke test outside the default unit suite.
- Verify partial coverage and provider errors are surfaced without synthetic fallback.
- Verify every supported claim references stored evidence.

### WebUI and end-to-end

- Verify the production bundle has no mock-store or mock-handler import path.
- Verify product import, country selection, 5-point weight controls, run states, result list, evidence panel, and exports.
- Verify default results and exports exclude rejected candidates.
- Verify the Rejected tab loads rejected records and reasons only when selected.
- Run a clean-database E2E from login through first real research result.

## Release acceptance

Interfaze v1 is ready for the live demo only when all of the following are true:

1. A clean deployment boots without creating sample business data.
2. The Silverline demo account can be provisioned and authenticated.
3. Silverline onboarding is complete while products, target countries, and operational records remain empty until user action.
4. Silverline's product catalog can be imported through the WebUI.
5. The backend candidate corpus can be imported without populating Silverline.
6. Target countries can be selected through the WebUI.
7. Scoring weights rebalance in 5-point transfers and remain valid.
8. A configured real provider can complete a research run or report partial coverage honestly.
9. Visible companies have evidence-backed scores, verdicts, and inspectable sources.
10. Rejected candidates are excluded from initial results and available in their separate tab.
11. Relevant backend, WebUI, scoring, provider, and clean-database E2E checks pass.
12. No production mock, fixture fallback, seeded Silverline lead data, or plaintext demo credential is present.

## External inputs

The genuine live demo requires the user to provide:

- Silverline's product catalog for WebUI import;
- the kitchen-appliance candidate corpus with country and company records; and
- Bright Data credentials through the deployment secret mechanism.

The implementation must define and validate the accepted import schemas before loading either dataset. Until the inputs arrive, tests use isolated fixtures but the live account remains empty.

## Out of scope

- outreach drafting, approval, sending, or mailbox integration;
- importing Silverline's existing leads;
- preselecting target countries;
- treating supplied candidate records as verified facts;
- automatic destructive demo-account resets;
- a new permanent core agent tool for Bright Data; and
- fabricated fallback results when live providers are unavailable.
