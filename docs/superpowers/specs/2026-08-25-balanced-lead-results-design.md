# Balanced Lead Results and Corpus Evidence Design

**Status:** Draft for written review; architecture approved in chat on 2026-08-25

**Date:** 2026-08-25

**Authority:** This design refines the result-selection and evidence semantics in
`lead-research-idea.md` and
`docs/superpowers/specs/2026-08-24-lead-research-contract-completion-design.md`.
It supersedes those documents only where they treat every corpus row as a
selection-only hint or require an official/registry publisher plus a second
publisher before a lead can receive `strong_fit`.

## 1. Outcome

A normal lead-research campaign should present a short, useful list rather than
hundreds of weak candidates or an empty strong-fit section. The primary lead
list has these product guarantees:

- no more than 15 displayed strong-fit leads across the whole campaign;
- a target of at least 5 displayed strong-fit leads when at least 5 candidates
  clear the absolute quality floor;
- broad market representation, normally 1–3 leads from each represented target
  country before a fourth lead is taken from any country;
- identical scoring semantics for every configured data source;
- no promotion, padding, or score manipulation merely to reach five results;
- an evidence receipt and an explainable score for every displayed lead;
- a named shortfall reason when fewer than five candidates clear the floor.

The 5–15 contract is global, not per country. A campaign with 15 target
countries cannot display 2–3 leads from every country while remaining under the
15-lead cap. The ranker therefore maximizes country coverage first and adds
second and third leads in later rounds when capacity remains.

`review` candidates remain inspectable in a separate secondary view. They do
not count toward the 5–15 primary-list contract, do not increment
`qualified_leads`, and are not silently relabelled as strong fits.

## 2. Current behavior and root cause

The measured Demo Company run over the real corpus selected 396 candidates and
stored 127 results. Every result was `review`; none was `strong_fit`. Fit was
35–39 even though evidence confidence was 0.848–0.932. Every candidate missed
an official source and a second publisher.

That outcome came from three interacting policies:

1. the 5,470-row kitchen-appliance corpus is allowed to name candidates but,
   without a per-row external URL, its curated membership and sector category
   produce no evidence;
2. source authority discounts fit and an official/registry publisher plus a
   second publisher is a hard strong-fit gate;
3. the campaign evaluates a broad candidate set and materializes every review
   result instead of selecting a deliberately small final list.

The corpus itself contains useful market-specific buyer names. Its current
normalized rows retain company name, country, and the canonical
`household-appliances` category. The raw source also contains contact data, but
that personally identifying data was deliberately excluded from the shared
corpus and must remain excluded. The defect is therefore not candidate supply;
it is the system's refusal to treat an explicitly curated dataset as a factual
source and its lack of a final-list ranker.

## 3. Non-negotiable invariants

The following existing boundaries remain intact:

- eligibility runs before scoring; identity, target-country, exclusion,
  sanctions, seller-only, inactive-company, and hard-negative failures cannot
  be rescued by ranking;
- terminal facts such as closed, dissolved, or liquidated always reject;
- fit and evidence confidence remain separate numbers;
- unknown criteria remain unknown and their campaign-weight share is shown;
- a candidate's source cannot choose its tenant or bypass candidate visibility;
- shared corpus rows never retain personal names, email addresses, or phone
  numbers; contact discovery remains tenant-scoped and on demand;
- public-fact sharing still requires the existing official/registry validation
  policy; equal treatment for lead scoring does not make every fact globally
  shareable;
- result caps never alter stored evidence or historical score snapshots;
- no source ID, provider order, or commercial access tier appears in fit-point
  arithmetic.

## 4. One normalized evidence contract for every source

### 4.1 Data sources are equal; facts are not

Every source adapter maps its output into the same normalized facts before
eligibility or scoring. A product-category assertion has the same fit effect
whether it came from an imported list, a tender adapter, a licensed feed, an
official page, or agentic research. A provider receives no fit bonus or penalty
for being named TED, corpus, Bright Data, customer upload, public, or licensed.

Business meaning still matters. An exact named-product match is stronger than a
broad sector match; a confirmed distributor role is more specific than a
generic sector-buyer assertion; current multi-country operations show more
coverage than one historical address. These are fact-shape differences, not
provider preferences.

Publisher class is retained for:

- fact visibility and promotion into the shared public pool;
- the evidence receipt shown to users;
- correction and provenance workflows;
- a confidence explanation when the assertion itself is indirect or stale.

Publisher class is not a prerequisite for `strong_fit`, and an official source
plus a second publisher is no longer a hard verdict gate. Corroboration,
freshness, directness, completeness, and conflict status determine confidence
under provider-neutral rules.

### 4.2 Dataset assertion manifest

`candidate_datasets` gains an immutable `assertion_manifest` JSON object. It is
part of the dataset version's identity and contains:

- `purpose`: one of `directory`, `curated_buyers`, `curated_prospects`, or
  `discovered_candidates`;
- `asserted_fields`: normalized facts the dataset publisher says each row can
  establish, selected from `company_identity`, `target_presence`,
  `product_sector_relevance`, `buyer_membership`, and `contact_channel`;
- `sector_ids` and optional product terms applying to the dataset as a whole;
- `curated_at` or explicit `freshness_unknown`;
- `publisher_label`, visibility, and curation note;
- a content hash linking the manifest to the immutable imported snapshot.

The import interface requires the manifest for a dataset to be evidence-bearing.
A legacy dataset without a manifest remains candidate supply only. This avoids
turning arbitrary uploaded directories into proof merely because they contain a
category column.

The kitchen-appliance corpus receives a new immutable version whose manifest
declares `curated_buyers`, `target_presence`, `product_sector_relevance`, and
`buyer_membership` for `household-appliances`. This is an operator declaration
about the supplied list, not a hard-coded exception for a dataset ID. The
original versions remain unchanged for campaign reproducibility.

### 4.3 Corpus evidence receipt

An evidence-bearing corpus row creates an immutable dataset evidence record even
when it has no public URL. The receipt includes dataset ID and version, row ID,
publisher label, assertion manifest hash, source snapshot hash, import time,
curation date or unknown-freshness marker, and the exact non-personal row fields
used by the score. The application links to its authenticated evidence view;
it never invents an external URL.

For the kitchen-appliance corpus, a matching row may establish:

- company identity by name and target-country presence;
- product/sector relevance from the canonical category;
- a normalized `sector_buyer` role from curated-buyer membership.

`sector_buyer` satisfies the wholesale-buyer eligibility class without
pretending that the row proved a narrower role such as importer or distributor.
The UI says "curated sector buyer; exact channel not yet confirmed." A later
source may refine the role without lowering the score.

Contact fields from the raw source are not copied into shared evidence. An
existing company domain may establish contactability only when the imported
non-personal candidate row carries that domain explicitly.

## 5. Scoring and strong-fit verdict

### 5.1 Fit is business fit, not publisher prestige

The seven customer-weighted criteria remain unchanged:

1. product/sector fit;
2. buyer/channel fit;
3. buying intent;
4. market coverage;
5. commercial scale;
6. trade activity;
7. contactability.

Normalized facts earn deterministic criterion values based on specificity and
degree. The initial rules required by this change are:

| Fact | Criterion value |
|---|---:|
| exact named-product or HS-code match | 100 |
| exact canonical sector match | 90 |
| declared distributor/importer/retailer/wholesaler role matching the campaign | 90 |
| curated `sector_buyer` membership | 85 |
| confirmed presence in one requested market | 80 |
| additional confirmed requested markets | up to 100 |
| valid company domain or verified contact channel | 80–100 by channel state |

The values are fact rules shared by all adapters. They are not configured per
source. New facts use sector playbooks or shared rule tables, never provider-ID
conditionals.

For each criterion, the strongest non-conflicted claim is the anchor and
additional agreeing claims can add only bounded support. A source's authority
does not multiply or discount fit. This preserves the monotonic rule: adding
agreeing evidence cannot lower a criterion.

Fit continues to normalize over the weight of answered applicable criteria.
The result still reports `known_weight`, `unknown_weight`, and every unknown
dimension. Missing dimensions therefore reduce confidence and remain visible,
without being falsely scored as zero.

### 5.2 Provider-neutral confidence

Evidence confidence is calculated from:

- weighted criterion coverage;
- identity certainty;
- assertion directness;
- freshness;
- corroboration from distinct evidence records;
- conflict and estimate penalties.

No factor checks a provider ID or access tier. Official/registry state is still
shown in the explanation and controls public-fact reuse, but it is not an
automatic zero for strong-fit eligibility. Unknown freshness is lower than a
current dated assertion but is not treated as stale or false.

### 5.3 Absolute quality floor

A candidate enters the strong-fit pool only when all of these are true:

- eligibility passed and no terminal veto exists;
- fit meets the campaign A-band fit threshold, default 80;
- evidence confidence is at least 0.60;
- at least 50 points of the customer's criterion weight are answered;
- product/sector fit and buyer/channel fit are both answered;
- no unresolved conflicting claim affects product, buyer role, identity, or
  target-country presence.

The 0.60 confidence and 50-point known-weight values are engine quality floors,
not a quota adjustment. A user may make a campaign stricter through its scoring
profile, but cannot make it weaker than the engine floor.

Candidates meeting a B or C band remain `review`. Candidates below C remain
rejected. The ranker never converts a review candidate to strong merely because
fewer than five strong candidates exist.

## 6. Candidate acquisition, deduplication, and cost control

### 6.1 Equal opportunity across sources

Each runnable source receives the same per-country acquisition ceiling. The
service unions all supplied identities, then resolves and deduplicates them
before final scoring. Reordering `enabled_source_ids` must not change which
leads are selected or their scores.

When several sources identify the same company, their facts accumulate on one
resolved organization. The company is not given multiple candidate slots and
is not charged as multiple leads.

### 6.2 Local pre-score before expensive research

Evidence-bearing structured rows are normalized and locally pre-scored before
network or agentic enrichment. Per country, the service retains a bounded
research shortlist large enough to fill the final list after expected drops.
The initial shortlist ceiling is the greater of 15 or three times the remaining
global result capacity, bounded by the campaign's existing safety limit.

Pre-score ordering uses, in order:

1. eligibility facts already known;
2. product/sector specificity;
3. buyer-role specificity;
4. answered campaign-weight share;
5. contactability already known;
6. freshness and corroboration;
7. stable normalized company identity.

Source ID and source order are absent. Stable identity is only a deterministic
tie-breaker; it adds no score.

Deep research targets unknown, high-weight criteria for shortlisted companies.
It does not re-fetch facts the row already established. A corpus-only campaign
can complete without a network credential when its rows establish enough facts
to clear the quality floor.

## 7. Final ranking and country balance

After scoring, all strong-fit candidates are sorted within each country by:

1. fit descending;
2. evidence confidence descending;
3. known campaign-weight descending;
4. evidence freshness descending;
5. normalized company name and resolved organization ID ascending.

The global selector then performs country rounds in the campaign's requested
country order:

1. take the best remaining candidate from each country;
2. take the second from each country;
3. take the third from each country;
4. if fewer than 15 have been selected, continue round-robin with the best
   remaining candidates until the global cap is reached.

The selector stops at 15. A country with no qualifying candidate is skipped
without consuming a slot. A candidate belongs to one resolved country and can
appear only once.

This makes the usual outcomes predictable:

- 15 qualifying countries: one lead per country;
- 5 qualifying countries: up to three per country;
- 2 qualifying countries: balanced rounds first, then the best remaining leads;
- fewer than 5 qualifying companies: show all of them and report a shortfall.

The displayed rank, country round, and tie-break values are saved with the
result snapshot so a later rerun remains explainable.

## 8. Persistence, metrics, API, and display

### 8.1 Persistence

Research keeps every evaluated result needed for audit, but only final-selected
strong fits receive `displayed=true`, `display_rank`, and a primary lead-list
materialization. Strong candidates outside the cap retain `strong_fit` with
`displayed=false` and reason `outside_result_limit`. Review candidates are
stored without primary lead materialization and remain available in the review
view.

Historical snapshots include:

- normalized criterion claims and evidence IDs;
- known and unknown weight;
- fit, confidence, and their factors;
- verdict before final selection;
- display decision, rank, country round, and shortfall state;
- source IDs for provenance only, never as score inputs.

### 8.2 Truthful metrics

Campaign metrics distinguish:

- `strong_fit_pool`: every evaluated candidate clearing the absolute floor;
- `qualified_leads`: displayed strong-fit leads only;
- `review_candidates`: stored review results;
- `outside_result_limit`: strong fits not displayed because the list is full;
- `countries_represented` and `leads_by_country`;
- `result_target_min=5` and `result_limit=15`;
- `result_shortfall` and structured `shortfall_reasons`;
- source supply, requests, failures, and candidate-stage counts already tracked.

The final campaign response and UI must agree with persisted rows. In
particular, `qualified_leads` equals the number of `displayed=true AND
verdict='strong_fit'` results and the number of primary lead-list entries for
that campaign.

### 8.3 Customer UI

The results page leads with the 5–15 primary list. Each row shows company,
country, fit, confidence, known/unknown weight, buyer-role wording, top reasons,
and the evidence receipt. A small country-distribution summary makes balancing
visible.

Below the primary list, the UI shows counts for review, rejected, and
strong-but-outside-limit candidates. Review candidates require an explicit
secondary-view action and are not visually presented as qualified leads.

When fewer than five results qualify, the empty capacity is explained with
counts such as no eligible candidate, product mismatch, buyer role unknown,
insufficient answered weight, conflict, or source failure. The UI never says a
campaign succeeded with five leads when it did not.

## 9. Migration and compatibility

- Add the dataset assertion manifest as a nullable legacy-safe column. Existing
  rows remain selection-only until an explicit new dataset version supplies a
  manifest.
- Build a new kitchen-appliance corpus version from the same sanitized 5,470
  company rows and an immutable curated-buyer manifest. Do not mutate versions
  1 or 2.
- Keep the raw contact-list file outside shared runtime storage and git. The
  existing build script's PII self-check remains mandatory.
- Add display-decision fields in a backward-compatible result payload or
  explicit columns with SQLite/Postgres migration parity.
- Preserve old campaign snapshots and verdicts. New semantics apply on a new
  run and are identified by a scoring-policy version.
- Continue accepting `max_qualified_leads_per_country` for old campaign JSON;
  it remains a research safety ceiling. It no longer controls the primary
  display count, which is globally capped at 15.
- Do not hard-code Demo Company, Silverline, kitchen-appliance dataset IDs, or
  country lists in engine code. Demo setup belongs in test/setup tooling.

## 10. Observability

Every campaign emits structured lifecycle logs keyed by `company_id`,
`campaign_id`, and `run_id`, without candidate names or contact data:

- campaign start and frozen profile/scoring-policy versions;
- per-country and per-source supply counts;
- pre-score and shortlist counts;
- eligibility, strong-pool, review, and rejection counts;
- final rank/balance summary and shortfall reasons;
- provider request counts, unavailable sources, and processing errors;
- terminal status and elapsed time.

The same summary is written to `run_events` (or the repository's equivalent
durable event path) and the normal application log. A real run must therefore
be traceable by run ID after its worker has completed.

## 11. Test strategy

Implementation follows test-driven development. Tests assert behavioral
relationships rather than frozen corpus counts or provider enumerations.

### 11.1 Unit tests

- an evidence-bearing curated row emits identity, target presence,
  product/sector relevance, and generic sector-buyer claims;
- a row without an assertion manifest remains a hint and earns no score;
- the same normalized fact set has the same fit and confidence regardless of
  source ID, source order, access tier, or adapter;
- official/registry classification controls sharing but is not a strong-fit
  prerequisite;
- exact product facts outrank sector-only facts and exact buyer roles outrank
  generic sector-buyer membership;
- unknown dimensions remain unknown and contribute to unknown weight;
- fewer than 50 known weight points cannot be strong fit;
- conflicts and terminal negatives still block strong fit;
- adding agreeing evidence never lowers fit or confidence.

### 11.2 Ranker tests

- no campaign displays more than 15 strong fits;
- the ranker never pads with review candidates;
- 15 qualifying countries produce one result per country before seconds;
- five countries with enough supply produce up to three each;
- source-order permutations produce the same selected identities and ranks;
- duplicate identities from several sources appear once with combined evidence;
- fewer than five qualified candidates returns the honest result and shortfall;
- persisted displayed rows, lead rows, API metrics, and UI counts agree.

### 11.3 Integration and UI tests

- a sanitized representative slice derived from the real corpus, containing
  several countries and both plausible and ineligible rows, replaces the
  two-row success smoke as the lead-result acceptance fixture;
- a corpus-only campaign can produce 5–15 strong fits without web credentials;
- mixed corpus and tender sources use the same fact rules and are invariant to
  enabled-source order;
- review candidates are separated from the primary lead list;
- result rows render evidence receipts, unknown weight, country balance, and
  shortfall explanations;
- SQLite and Postgres migration/contract tests cover the new fields.

### 11.4 Required Demo Company run

After focused tests and the complete lead-research test suite pass:

1. copy the live Interfaze database to a temporary path and run migrations;
2. install the new curated manifest corpus version and verify its record count,
   visibility, absence of PII, and country distribution;
3. run Demo Company against its real frozen company profile, products, target
   countries, weights, and currently available sources;
4. observe structured logs and durable run events by run ID until terminal;
5. verify 5–15 displayed strong fits when the corpus supplies at least five
   floor-clearing candidates, no more than three from one country while another
   represented country still has an unselected candidate, and exact metric/row
   agreement;
6. inspect the evidence and score breakdown for every displayed lead and
   manually sample rejected/review rows for false promotions;
7. only after the isolated run passes, execute one authorized Demo Company run
   on the live application database and report the exact run ID, status,
   funnel, strong-fit pool, displayed lead count, country distribution, review
   count, request count, errors, and log/event evidence.

The live acceptance run must use the real 5,470-row current corpus lineage. A
synthetic two-row fixture is not evidence that the Demo Company workflow works.

## 12. Acceptance criteria

The change is complete only when all of the following are true:

- the current curated corpus is usable as evidence without importing personal
  contact data or fabricating public URLs;
- provider identity and ordering cannot change a normalized fact's score;
- a strong fit no longer requires an official/registry source plus a second
  publisher;
- eligibility, conflict, hard-negative, unknown-weight, and tenant boundaries
  remain enforced;
- the primary list contains at most 15 displayed strong fits and aims for at
  least 5 without padding;
- country balancing follows deterministic rounds and is snapshot-auditable;
- review candidates no longer inflate qualified-lead metrics or the primary
  lead list;
- application metrics, API output, UI counts, persisted results, and leads all
  agree;
- lifecycle logs and durable events make the Demo run observable by run ID;
- the full required test set passes;
- a real Demo Company campaign is run after implementation and its exact results
  are reported to the user.

## 13. Explicit non-goals

- Guaranteeing five leads for a market/product combination that has fewer than
  five eligible, evidence-backed companies.
- Copying personal contacts from the raw shared list into candidate evidence.
- Giving every target country 2–3 leads when that would violate the global cap.
- Removing confidence, evidence receipts, unknown criteria, or rejection
  reasons to make the list look fuller.
- Adding provider-specific score boosts, quotas, or hard-coded exceptions.
- Making unvalidated customer/list facts globally reusable as validated public
  facts.
- Replacing on-demand contact research with old contact-list data.
