# Demo readiness and enrichment plan

State as of 2026-08-20. Two independent tracks: getting a demo run to work on
the deployed service, and making reruns stop paying to re-research companies
they already settled. The demo does not depend on the enrichment work.

## Deployment

| | |
|---|---|
| App | `agent-rota` (Fly, region `fra`, one machine, one volume) |
| Host | https://agent-rota.fly.dev |
| Release running | v11, 2026-08-20 09:15 UTC — 12 minutes after `2ec40dcad6`, so incremental selection ships in it. Only `139f23cee0` (tests) is unreleased. Image contents not grep-verified. |
| Database | Supabase Postgres, migrations 001–009 all applied |
| Model | `minimax/MiniMax-M3`, `MINIMAX_API_KEY` set |
| Verifier | Bright Data Web Unlocker, zone `cli_unlocker`, `active` server-side |
| CLI | `flyctl` now lives at `~/.fly/bin/flyctl`; the Homebrew copy is gone |

`fly deploy` needs `--remote-only` — there is no local Docker on the dev
machine and no builder app, so a plain `fly deploy` dies before creating a
release.

## Demo readiness

### Done

- Supabase project resumed; all nine migrations applied. 006 and 007 had been
  skipped while 008 and 009 were in, so the boot guard was refusing to serve.
- `provision-demo` works on Postgres. It was broken until `2815a3f869`: the
  transaction proxy returned asyncpg's status string, so any `SELECT` inside
  `db.transaction()` raised `AttributeError: 'str' object has no attribute
  'fetchone'`. SQLite tests passed throughout.
- Demo tenant provisioned — `demo@tugrap.dev`, one company, onboarding
  complete, zero operational rows. Password is in the password manager only.
- Bright Data fully gated on: `BRIGHTDATA_API_KEY` secret set,
  `brightdata_enabled: true` in `/data/config.yaml`, verifier reports
  `BrightDataVerifier | active`. The token and zone were confirmed against
  `api.brightdata.com/request` directly, including Google SERP domains.
- Candidate corpus imported: **5,470 companies across 150 markets** (AE 579,
  SA 541, IQ 274, IN 207, LY 194).
- Local SQLite brought to parity: same corpus imported, demo tenant
  `demo@tugrap.dev` provisioned (`cmp_3ad7118113ed4cf99ad4`), catalog seeded.
  The `candidate_*` tables were missing from the local database because it
  predated that schema; they are created on boot by `Database.initialize`.
- All three catalog sources enabled on the local demo tenant. TED and the
  corpus report `active` and `available`; Bright Data reports `active` as soon
  as `BRIGHTDATA_API_KEY` is in the serving shell's environment, and
  `credential_required` without it. The key is deliberately not on local disk.
- A real campaign ran end to end on that tenant: 173 qualified leads from the
  TED-derived list with per-lead research on. Nothing here is fixture data.
- Fly is on v12 and carries all of it. The Data Sources page showed nine
  entries until the pruning seed ran — it is lazy, so the first catalog view
  after deploy is what clears the stale tenant rows. All three sources now read
  active and available there, Bright Data included.

### Remaining — two steps, both in the WebUI

1. **Enable the data source — as the admin, not as the demo user.** Provider
   catalog sources are admin-managed by design: `_source_enabled`
   (`server/routes/operations.py:534`) rejects a non-admin principal for any
   `source_id` in the registry, and the only page that renders the control is
   `/admin/data-sources`. `demo@tugrap.dev` is `role='customer'`
   (`server/provisioning.py:77`), so it cannot do this and has no UI for it.

   Sign in as `INTERFAZE_BOOTSTRAP_ADMIN_EMAIL` → **Customers** → the demo
   company → **Open workspace**. That button is the only thing that writes
   `session.company`, and `config.beforeRequest` (`server/webui/js/main.js:62`)
   only sends `X-Company-ID` when it is set — without it every admin call 400s
   on `company_id is required for admin requests`. Then **Data sources**:
   `dataset_definitions` seeds lazily on first view (`service.catalog` →
   `ensure_catalog`), and Bright Data arrives `installed=1, enabled=0`
   (`default_enabled: false` in the provider catalog), so it is one **Enable**
   click. Health must read `active`.
2. **Leave `product_ids` empty and pick the sector.** This entry previously said
   to import a product. That is backwards, and it would have produced an empty
   demo. See "Lead search flow" below. The customer brief page already does the
   right thing; the sector id to use is `household-appliances`.

Step 1 is no longer the only path to a working demo: TED needs no credential
and is enabled the same way, so a run can produce leads before the Bright Data
key is in place. It just cannot produce a `strong_fit` without one.

Step 3 used to read "include `public procurement supplier` in `buyer_types`".
That was a workaround for the vocabulary trap, and it was not something the
customer brief page could do — it derives buyer roles from the sector. The
bridge is in `qualification.py` now, so no hand-set buyer type is needed.

### Validated 2026-08-20, on local SQLite

The customer path — the exact body `research-brief.js` submits — was run
end to end over HTTP against the demo tenant, on a tenant already holding 732
claims from an earlier campaign. Sector `household-appliances`, market RO,
sources `customer-list-corpus` + `ted`, deep research on, limit 3:

| | |
|---|---|
| `POST /research-campaigns` | 201 |
| `POST .../start` | 202, `succeeded` |
| Funnel | 9 raw → 9 named → 5 resolved → 5 eligible → 5 qualified |
| Verdicts | 5 × `review`, fit 100 |
| Issues | 4 — the corpus rows that abstained, correctly named |
| `enriched_companies` | 0, correct: enrichment re-queries `web_evidence` sources only, and Bright Data is `credential_required` locally |

Four bugs stood between that config and a lead; all four are described in their
own sections below. Before them the same request returned zero at three
different stages.

## Lead search flow

What actually happens when a campaign runs, traced through
`LeadResearchService.run` (`server/lead_research/service.py:535`). Two supplies
meet here and they are on different axes, which is the thing that is easy to
get wrong: **the corpus supplies names, the data sources supply evidence.**

```
candidate_records                    dataset_definitions
(shared, no company_id)              (per tenant, seeded from provider-catalog.yaml)
        |                                     |
        |  select(country, terms, -settled)   |  enabled AND available
        v                                     v
    candidates  ------------ verify() ------------>  VerificationBundle
                                                            |
                            evidence_records <--------------+
                                     |
                     IdentityResolver -> organizations, organization_links
                                     |
                              feature_claims  (country, buyer_role, lifecycle_status)
                                     |
                     evaluate_verdict + score_lead
                                     |
                        research_results -> leads
```

### 1. Candidate selection

`CandidateRepository.select` (`server/lead_research/candidates.py:260`) filters
the corpus by:

- **country** — one campaign country at a time, ISO alpha-2, indexed.
- **product terms** — `sector_ids + hs_codes + names of the selected products`.
- **dataset version** — only the newest version of each dataset. A corpus is
  immutable, so a correction ships as a new version beside the old one, and
  without this both were selected: every company in a corrected corpus was
  verified twice at full request cost, with the superseded row's stale facts
  competing against the fix.
- **closed identities** — an organization whose latest `lifecycle_status` claim
  says closed. Applied before `limit`, so a rerun still gets a full batch.

Rows are ordered by `source_record_id`, not by `dataset_id`. Ordering by
dataset let the alphabetically-first corpus consume the whole limit, so a
tenant holding both a plain contact list and a procurement-derived corpus never
reached the second one — and on the demo that meant a limit of 9 drew 9
uncitable rows and verified none of them.

Term matching is `all(term in searchable for term in terms)`: **AND, substring,
against the candidate name plus its aliases and categories.** Two consequences
that decide whether a demo produces anything at all:

- Selecting **two or more products guarantees zero candidates.** No company
  name contains two different product names.
- Selecting **one product almost certainly returns zero too.** The term is the
  product's `name` column, e.g. `built-in oven series (svn-b)`, and that is not
  a substring of `al ahmadi trading co.`.

What does work is a term that matches the category stamped on every corpus row
by `build_corpus.py`: `kitchen-appliances`, or any substring of it. Matching is
case-sensitive after `normalize_name`, which only lowercases — `Kitchen
Appliances` matches nothing, `kitchen-appliances` matches all 5,470.

So: put the term in `sector_ids`, leave `product_ids` empty. Products are still
worth importing for outreach copy; they are just not a discovery filter.

### 2. Verification

For each candidate, every enabled **and available** source gets
`provider.verify(query, candidate)`. A candidate that produces no evidence is
recorded as `candidate_processing_failed` and dropped.

**An empty bundle is an abstention, not a verification.** A provider with
nothing to say returns a bundle carrying no sources — `CorpusProvider` does
exactly this for a row with no citation, which is most rows in a contact-list
import. Counting it as a bundle carried the candidate to the identity stage to
die there on `verification returned no evidence-backed identity`: an
internal-sounding error in place of the true one, plus a `verified` count for a
candidate nothing had verified. Abstentions are now dropped before that, and the
issue names the sources that abstained.

Availability is stricter than enabled. `service.catalog` marks a source
unavailable when it is retired, unhealthy, credential-gated, or —
the case that catches people — **when the provider has no `verify` method at
all**. `verify` is not part of the `Provider` protocol; `CatalogProvider` does
not implement it.

All three surviving catalog entries implement it, but they do not cover the same
ground. TED verifies EU public-contract winners; the corpus speaks only for rows
carrying a `provenance_url`; Bright Data is the only one that can verify an
arbitrary company in an arbitrary market. So on the corpus — Gulf and Asian
traders with no citations — Bright Data is still the real gate, and without its
credential those candidates abstain their way to zero.

### 3. Evidence to verdict

Bundles become immutable `evidence_records`. `IdentityResolver` maps each to an
`organizations` row via `organization_links`, so the same company found twice
stays one identity. Facts become `feature_claims` carrying value, status,
confidence, evidence ids and `verified_at`. `evaluate_verdict` and `score_lead`
then decide fit and confidence separately, and a qualifying result upserts a
`leads` row.

Claims are also what makes the next run cheap: they are what
`_settled_identities` reads back.

### Cost

`MAX_SEARCH_PAGES = 3` Web Unlocker fetches per candidate. The full corpus is
roughly 16,500 requests. `select()` takes a country filter and a limit — use
both. Thirty candidates in one market is about 90 requests.

### Housekeeping

`rm /data/secure/demo-password` once the password is in the manager. The volume
is snapshotted.

## Data sources

The catalog carries three sources and every one of them can verify a candidate.
It used to carry nine, and eight of those rendered as permanently unavailable —
which reads as a broken product, not as the "not integrated yet" it meant.

| Source | Access | Verifies | State |
|---|---|---|---|
| customer-list-corpus | imported file | rows carrying a `provenance_url` | active |
| ted | free API, no key | EU public-contract winners | active |
| brightdata-web | key in `BRIGHTDATA_API_KEY` | any candidate, any market | active with the key |

Removed 2026-08-20: `un-comtrade` and `eurostat-comext` publish market
aggregates and cannot name a company at any level of adapter effort; `auma`
lists fairs rather than exhibitors; `companies-house` needs a key and a GB-only
adapter nobody has written; `b2match-export` waits on a customer export;
`jetro-jmesse` shut down 2026-03-31. No history was lost — `evidence_records`,
`organization_links` and `campaign_partitions` all carry `source_id` as free
text with no foreign key.

Two mechanics worth knowing, both of which used to bite:

- A tenant row caches the catalog definition. `ensure_tenant` refreshes it on
  every seed now, so a change like TED going from `manual_import` to `live`
  actually reaches tenants that were already seeded — and so does a retirement.
- `catalog()` resolves every tenant row through the registry, so a row for a
  removed source raised `KeyError` and took the whole Data Sources page down.
  Seeding prunes those rows.

Enable/disable is per tenant and always was: `dataset_definitions` is keyed
`(company_id, source_id)` and `_source_enabled` scopes every write by company.
`test_ted.py` now pins it so it stays that way.

### What the corpus source will and will not say

A corpus is candidate supply. If an imported row could vouch for itself, every
name anyone ever uploaded would qualify and the verdict system would be
decoration. So `CorpusProvider` speaks only for rows carrying a
`provenance_url` — the record the row was taken from — and abstains on the
rest. A citation pointing at the candidate's own domain is classified
`official`, so it cannot satisfy an eligibility rule that asks for an
independent source.

The kitchen-appliance corpus has no provenance and therefore produces no
evidence on its own; it still needs TED or Bright Data behind it.

## The TED lead list

Scanning the customer list against TED verified 6 of 244 EU rows — 2.5%, and
every one a real match (Coolblue, Dedeman, Leroy Merlin, Ekupi, Gemma B&D,
Menigo Foodservice). The list is private Gulf and Asian traders; TED covers EU
public procurement. Low overlap is the honest answer, not a bug.

Turning the query around is what works. `build_ted_leadlist.py` pulls award
notices for ten kitchen and catering CPV codes and keeps the winners: **201
companies across 20 markets, 40 with a website, each row carrying the notice
it came from.** Every row is verifiable by construction, because TED is where
it came from.

A campaign over it on the demo tenant returns **91 qualified leads** — 396
candidates, 98 resolved identities, 91 eligible. RO 26, CZ 20, ES 19, HU 8,
BE 8. Names like PRO HORECA, IN-GASTRO and GOZ GASTRO are catering-equipment
suppliers, which is the buyer profile.

All 91 land on `review` rather than `strong_fit`, and that is correct: TED is
one independent source, and `evaluate_verdict` wants an official source and a
second one before it calls anything strong. Bright Data is what supplies the
second.

### Two traps this run walked into — both now fixed

- **Buyer-type vocabulary.** The first run qualified nothing: every candidate
  was rejected on `buyer_role` because the campaign asked for
  `distributor/importer/retailer` while the rows carry
  `public procurement supplier`, and `EligibilityService` intersects the two
  sets. `ROLE_EQUIVALENTS` in `qualification.py` is that bridge now: winning a
  public supply contract proves a company resells these goods, so the term
  answers a request for any reselling role and deliberately not for `brand` or
  `manufacturer`, which claim production it does not evidence. The gate also
  reads observed `buyer_role` claims and not only the corpus row's guess — a
  contact list states no role at all, so gating on the row alone rejected every
  company in it.
- **Rejected companies counted as settled.** A rejected candidate still gets
  `country` and `buyer_role` claims, so `_settled_identities` skipped it on the
  next run. That skip is gone (see plan items 1–2), so a config fix no longer
  needs the tenant's claims cleared first.

### A defect worth remembering

The first version of the lead list paired each winner with a website by array
position. TED returns those arrays at equal length but in different order — in
notice `255023-2024`, `Conti Grup` is winner index 2 while `contigrup.ro` is
site index 1 — so companies were handed each other's domains. `TDH MOB DESIGN`
went out with `climatherm.ro`.

Websites are now matched by name and dropped when nothing matches, which is why
only 40 of 201 rows carry one. That corpus version was withdrawn rather than
edited; corpora are immutable, so the fix shipped as version 2.

## Per-lead research and the customer brief

Two things the campaign was missing, both now wired.

### Research each lead

`run()` never mentioned enrichment. `FeaturePlanner` and `EnrichmentService`
were importable and referenced by exactly one unit test, and the
`EnrichmentProfile` the editor collects was stored in the config and read by
nobody. A source match was the end of the line.

Qualified candidates now get a second pass aimed at what the first one did not
establish, searching the sector's own vocabulary — "white goods", "private
label", "distributor wanted" — instead of repeating the campaign's terms
against the same pages.

Three things that decide whether it does anything:

- **The vocabularies had never met.** Playbooks ask for `product_fit`;
  verifiers emit `product_term`. `PLAYBOOK_SATISFIED_BY` is that bridge and is
  written out explicitly, because a guessed mapping marks a gap filled without
  filling it.
- **`research_each_lead` is not `enabled`.** They are different mechanisms.
  `enabled` is the local-model fallback and still requires a `model_profile`;
  this pass costs requests, not tokens, and needs no model. Overloading one
  flag would have made the model contract a lie.
- **Only `web_evidence` sources are re-queried.** TED retrieves by winner name
  and country, so asking it again returns the same notices and spends a request
  to learn nothing. Bright Data searches the terms and reaches different pages.
  The capability is already declared in the catalog, so this is a lookup rather
  than a hardcoded source id.

The corpora carry `household-appliances`, the canonical sector id from
`sectors.py`. `kitchen-appliances` matched no playbook at all, so every gap
looked closed and enrichment could never have run whatever else was fixed.
That change alone took the same campaign from 91 qualified leads to 173,
because the sector's buyer roles finally lined up with the eligibility gate.

**`build_corpus.py` emitting the canonical id is not the same as the imported
data carrying it.** Only `ted-appliances` v3 was re-imported at the time. The
5,470-row `kitchen-appliances` v1 rows kept the old category, so the sector id
the brief page offers matched 201 EU rows and **none** of the corpus — a search
of AE or SA returned zero. Fixed by re-importing the same file as
`kitchen-appliances` v2, which reproduces byte-for-byte (5,470 rows, AE 579,
SA 541), and by the version filter in `select` that stops v1 and v2 both being
selected. Anywhere the corpus is loaded from scratch, import it as v2 or later.

### The customer brief

Campaign configuration was admin-only by **routing**, not by permission. The
API scopes by company and never checks `is_admin`; the WebUI simply redirected
every `/app/research/*` path back to the results list. The person who has to
act on the leads could not choose markets or touch the weights.

`/app/research/new` is a customer page now. It asks for the three things that
are theirs — markets, sector, and what a good lead weighs — and nothing else.
Source access stays admin-owned, so it never shows a provider picker: it runs
whatever the tenant already has, names them, and refuses to start when that
list is empty rather than running a search that cannot find anything.

Buyer roles come from the sector rather than a free-text field, which is the
direct fix for the trap below.


## Corpus provenance

Built from `customer list - KitchenAppliancesCustomerData.csv` (5,642 rows) by
`build_corpus.py`. That file is now gitignored; it was neither ignored nor
tracked, one `git add -A` from being published.

Dropped, from 5,642 to 5,470: 142 duplicate company+country pairs (the importer
raises rather than deduping), 13 blank company names, 3 regions with no alpha-2
code (`Caribbean`, `Middle Asia`, `West Indies`), 14 Kosovo rows (`XK` is
user-assigned, not ISO 3166-1, so `ISO_ALPHA_2` rejects it — adding it is a
one-line change if Kosovo matters).

**All personal columns were stripped deliberately.** Unknown CSV columns are
stored into `candidate_records.data`, and that table has no `company_id` and is
shared across tenants. Importing the file as-is would have put ~5,600 people's
names, titles, emails and phone numbers into non-tenant-scoped storage.

Those contacts are still valuable and still recoverable — see plan item 4.

`build_corpus.py` is now tracked at the repo root; the earlier copy had been
lost. It reproduces the deployed corpus exactly — same 5,470 rows, same
per-country counts (AE 579, SA 541, IQ 274, IN 207, LY 194) — so local and Fly
are running identical candidate supply. It emits `source_record_id`,
`company_name`, `country` and a `kitchen-appliances` category, and nothing else;
the PII columns are dropped at parse time rather than filtered later.

Country names are mapped by a hand-written table in that script rather than a
country-data package, because the raw file uses misspellings no lookup resolves
(`Malasia`, `Krgyzistan`, `Venezuella`, `Afganistan`, `Djibuti`, `Ethiopa`). An
unrecognised name is a hard failure, not a silent drop.

## Enrichment plan

The corpus being unvalidated is by design, not a defect: `candidate_records` is
immutable (re-import raises `CandidateImportConflict`) and shared with RLS
deny-all. Whether a company is worth contacting is a per-tenant judgement, so
validation state lives in `organizations` and `feature_claims`.

Much of what this plan needs already existed: `feature_claims` carries
field/value/status/confidence/evidence_ids/verified_at per organization with a
nullable `campaign_id`; `organization_links` is the cross-run identity backbone;
`_fact_matches` already emits `country` and `buyer_role` facts, so country
validation and buyer-type tagging were already claims.

### 1–2. Closed state — DONE. Incremental selection — WITHDRAWN

`2ec40dcad6` added both. Closure stayed; skipping merely-validated identities
was removed, because it never worked and it broke every campaign after the
first.

Selection now skips only identities whose latest `lifecycle_status` claim says
closed.

- Closure is read latest-first, so a later `operating` claim reopens a wrongly
  retired company. Closure is not a one-way door.
- Closure is read tenant-wide, across campaigns: a dissolved company is
  dissolved for every campaign, and production writes claims with a campaign
  id.
- The verifier emits the claim from 17 narrow multi-word phrases, gated on an
  identity match. Bare "closed" is ordinary prose ("closed on Sundays", "closed
  a funding round") and a false positive removes a live company from every
  future run.
- `excluded_closed` sits outside `FUNNEL_KEYS` — that funnel is monotonic and
  this describes work never started.
- No migration. A new column would have duplicated `feature_claims` and lost the
  evidence binding.

**Why the validated-skip was withdrawn.** A run rebuilds its own results from
scratch — `_run_campaign` deletes this campaign's results and claims before
selecting — so any identity it skips is simply absent from its output. Two
consequences, both measured on the demo tenant:

- For the case it was written for, a **rerun**, the skip set was always empty:
  the campaign's own claims are deleted before `_settled_identities` reads them.
  It never made a single rerun cheaper.
- For a **new campaign**, claims written by an earlier campaign are never
  cleared, so it skipped all 173 settled identities and reported
  `raw_records: 0` and zero leads. That is what a demo hit.

Every test covered `_settled_identities` directly with claims written at
`campaign_id = None`, a shape the production writer never produces, which is
how it survived. Per-candidate cost is bounded by `select(limit=…)` regardless —
the 16,500-request figure assumed an unbounded full-corpus pass that `select`
does not perform. Making skipping sound would mean carrying a skipped
identity's prior result and claims through the rebuild and counting them at
every funnel stage they previously passed; worth doing if request cost bites,
but it is a redesign of the rebuild model, not a filter.

### 3. Buyer-type vocabulary — DONE for the terms in play

`ROLE_EQUIVALENTS` and `satisfies_buyer_role` in `qualification.py`. Sector
roles across `sectors.yaml` are a closed set of seven — brand, distributor,
importer, manufacturer, procurement_organization, retailer, wholesaler — and the
only foreign vocabulary any verifier emits is TED's
`public procurement supplier`. Bright Data matches the campaign's own terms, so
it never mismatches; a corpus emits whatever its file said.

So the map is one entry, written out per term rather than inferred, plus role
spelling normalised (`procurement_organization` and "Procurement Organization"
are the same role). Add an entry when a verifier starts emitting a new term.

Still open: OEM, HORECA supplier, contractor and e-commerce appear in the plan
but in no sector and no verifier, so there is nothing yet to bridge them to.
Country is validated but is not a tag.

Blocked on a decision: `buyer_role` facts only match terms passed in through
`query.buyer_types`, so the taxonomy has to be defined before tagging does
anything useful across industries.

### 4. Contact import — NOT STARTED, highest value per unit of work

~5,600 real contacts with titles from the original customer list into
tenant-scoped `contacts`, marked `verified` with the customer list as
provenance. Means discovery fills gaps instead of starting cold.

Blocked on a decision: contacts attach to `organizations`, which today are only
created during research. Either create organizations from the corpus up front,
or hold the contacts until research creates them.

### 5. Discovery gap-fill — NOT STARTED

Published sources plus pattern-guessed, where a pattern is confirmed by at least
one known address and stored as `pattern-guessed`. Decided: published +
pattern-guessed, no licensed enrichment vendor for now.
`skills/sales/contact-discovery/SKILL.md` already specifies this pipeline
including verification status, and outreach already refuses to CC a guess.

## Three contract fixes, 2026-08-21

A review of the lead-research module found the plumbing sound and three
contracts broken: a route that lied about being asynchronous, a policy the UI
collected and the engine ignored, and an identity resolver that silently
duplicated every company it could not link. All three are fixed.

### A1. `/start` no longer runs the campaign in the request

`POST /research-campaigns/{id}/start` declared `202` and then ran the entire
campaign inline — hundreds of blocking Web Unlocker fetches at a 45s timeout
each. Any proxy or worker timeout killed it mid-run and left the campaign
`running` for good. `/cancel` wrote a status nothing read, so a cancelled run
kept spending until the corpus ran out.

`LeadResearchService.start()` now queues onto a bounded pool and returns
`{"status": "queued"}`; `run()` is unchanged and is still the synchronous engine
the CLI and tests drive. The shape is copied from `DocumentProcessingService`
deliberately — same pool, same `wait_until_settled`, same `shutdown()` wired
into the app lifespan — rather than introducing a second background mechanism.

- **The status move to `queued` is the race guard.** A run deletes and rebuilds
  its own results, so two concurrent starts would interleave deletes with
  inserts over the same rows. The compare-and-swap makes the loser a 409.
- **Cancellation is read once per candidate**, before verification: one indexed
  SELECT against three HTTP fetches is free, and the alternative is a cancel
  that does nothing until the whole batch is paid for. A cancelled run
  terminalizes as `cancelled`, not `partial` — reporting a source failure would
  send someone hunting a provider that was working fine.
- **A campaign cancelled between queueing and pickup never starts.** Claiming
  `running` first would have lost the cancellation.
- Callers updated: the smoke script now polls `GET /research-campaigns/{id}`
  and is the reference for how a client waits; both WebUI toasts said the
  search had *finished*, which sent people to an empty list that read as a
  failed search.

Still open: the results page tells you research is running but does not refresh
itself. A live progress view is a product decision, not part of this fix.

### A2. The eligibility policy is the policy that runs

`EligibilityService` hardcoded five gates and never read `config.eligibility`.
The campaign editor has rendered switches for `require_official_domain`,
`require_target_presence`, `require_buyer_role`, `exclude_inactive` and
`minimum_independent_sources` the whole time, and flipping any of them changed
nothing. `config.exclusions` was equally dead.

- Every switch now changes the outcome, and `minimum_independent_sources` got
  the number input it never had — it is enforced, so a hidden value would be a
  policy the tenant cannot see or loosen. Zero switches the gate off.
- **A gate switched off reports `not_required`, not nothing.** The stored gate
  map is the record of why a company qualified; a gate that vanishes is
  indistinguishable from one that passed, which makes an old verdict
  unreadable after a policy change.
- Source coverage moved ahead of the gate, because `require_official_domain`
  and `minimum_independent_sources` ask about it and a gate cannot read a value
  produced two stages later.
- `exclusions.domains` and `exclusions.company_ids` are applied, domains
  compared on the normalized host.
- **Compliance now reports `unknown` instead of `pass`.** No sanctions
  screening source is connected and nothing ever set `candidate["sanctioned"]`,
  so a pass claimed a check nobody ran. It does not block. The editor copy
  claimed sanctions gates existed; that copy is corrected.

**This makes the default policy stricter than what shipped**, because the
default asks for one independent source and that is now enforced. TED and the
corpus both classify a non-self citation `independent`, so the 173-lead run
would be unaffected; a corpus row citing only its own domain is `official`-only
and would now be rejected. Set the minimum to 0 to get the old behaviour.

### A3. An identity no longer duplicates when nothing links it

`IdentityResolver` matched on `registry_id` or `domain` only. Nothing emits a
registry id, and a domain arrives only from an official-classified page — TED
carries one for 40 of 201 rows and classifies every source `independent`. So
every company whose evidence named it but did not link it got a **new
organization on every run**: duplicate orgs, duplicate leads (leads are keyed
by organization), broken result-id preservation on refresh, and the tenant's own
prior claims hidden from the run that needed them.

A verified name plus a market now matches, after every identifier tier:

- **The name must be verified**, never the corpus hint — this is evidence
  matching evidence, which is what makes it a match rather than a suggestion.
  The market may come from the candidate hint, since verifiers often name a
  company without stating its country while the corpus row always carries a
  validated ISO code. The hint is never stored as a fact.
- **Two different verified domains under one name are two companies.** Merging
  would credit one with the other's evidence, which is worse than a duplicate.
- Countries must agree when both are known. While the stored market is still
  blank the name is the only signal there is — and that duplicate happens on
  every run, whereas the wrong merge it risks needs the same name in two
  markets with the country still unrecorded. Once a market is on record the
  strict rule applies again.
- Name matching bootstraps toward strong links: the run that first learns a
  domain writes the link, and every later run matches on that instead.

Verified by reverting the fix — six tests fail without it. 436 server tests and
the four WebUI suites pass with it.

### B1. Fit separates leads instead of confirming they exist

`_claim_score` returned `100.0` for any truthy claim value, and `score_lead`
divides by the weight of the dimensions a lead actually has — so a single
product term found in a single search snippet scored the same as a company
corroborated across four sources, and the weighted average of a few 100s is
100. That is exactly what both real runs showed: 91 leads and then 173 leads,
**every one of them fit 100 and `review`**. Ranking is the product, and the
list had nothing to rank by.

A claim whose field *is* a dimension is still a provider stating that
dimension's score, and is respected as before. Everything else — a matched
product term, an observed buyer role — is evidence *of* a dimension and is now
scored by degree: authority (the claim's own confidence, which is where
official-vs-independent already lives) times a strength built from
corroboration and breadth.

Anchors, and the reasoning for each:

| Evidence | Score |
|---|---|
| one value, one source | ~50 — a mention with nothing corroborating it |
| one value, two sources | ~74 — corroborated but narrow |
| two values, two sources | ~82 |
| three or more, two sources | ~90 — the strongest this evidence model states |

- **Corroboration saturates at two sources on purpose.** "An official source and
  an independent one agreeing" is already the standard `evaluate_verdict` uses
  for `strong_fit`; paying for a fourth would let a company with many weak
  mentions outrank one with an official page and a registry agreeing. Above
  that line breadth does the separating.
- **Coverage is not double-counted.** Fit still divides by the weight of the
  known dimensions only, because missing dimensions are already priced into
  `evidence_confidence` through `completeness`. Fit is the quality of what is
  known; confidence is how much is known and how well.
- Measured spread on real evidence shapes: thin (one independent snippet) 47 /
  band C, ordinary (official + independent, one term) 72 / band B, strong
  (three sources, three terms, two roles) 81 / band A.

**Consequences to expect.** The shared test fixture states one term and one
role across two sources, so it now lands on `review` rather than `strong_fit` —
which is correct, and a dedicated end-to-end test carries the `strong_fit`
canary instead. On the 173-lead TED run, leads will spread across C and B
rather than tying at 100; band A needs several corroborated terms, which TED
alone does not supply. That is the precision-first stance the product already
claims, made real.

**`/leads` was not sorted by fit at all.** It ordered by `leads.created_at
DESC`, so the customer's list arrived in the corpus's arbitrary insertion order
while the brief page promised it was "ranked by your weights". It now orders by
fit, then evidence confidence, with `created_at` only breaking ties so the
order is stable across reruns. `/results` was already ordered correctly.

Not done here, deliberately: magnitude is still not read for numeric evidence
fields (`store_count`, `revenue`). No verifier emits one, so a scale curve
would be tuned against nothing — there is a `ponytail:` marker at the spot.

Verified by reverting: seven scoring tests and the ordering test fail without
it. 456 server tests and six WebUI suites pass with it.

### B2. A verdict is no longer capped by corpus metadata

**The first diagnosis of this was wrong and is worth recording.** It read: TED
classifies every source `independent` and never emits `official`, so accept two
independent domains as sufficient for `strong_fit`. That would have lowered the
bar for the wrong reason. TED alone, and a corpus whose rows cite the TED notice
they came from, are **one publisher domain between them** — `ted.europa.eu` — so
`second_source` is legitimately missing and `review` is the right answer. One
publisher is one source.

The actual defect is narrower and worse. `official` means "a page on the
company's own domain", and that classification is only ever produced by
fetching a domain the candidate row *already carried*: `BrightDataVerifier`
skips the official fetch entirely without one, and `_is_official(url, None)` is
`False` by construction. Only 40 of the 201 TED-derived rows carry a website. So
for the other 161, `strong_fit` was unreachable **at any evidence level** — the
verdict was capped by corpus metadata rather than by evidence.

`SourceCoverage` now carries a third, orthogonal set. `official` and
`independent` answer *who published it*; `registry` answers *what standing the
publisher has*. A TED award notice is both independent and authoritative, and
the two sets overlap on purpose.

- **Standing is declared per source in the provider catalog**, not inferred from
  a page or a URL. "The EU's Publications Office is authoritative" is a
  provenance judgement about a publisher, and guessing it would be the same
  upgrade-a-hint-into-evidence move this module refuses everywhere else. TED
  carries `authoritative_registry`; a test pins that nothing else does, because
  a web verifier reports whatever page it reached and reaching a page is not
  standing.
- **What blocks a strong verdict is now separate from what is merely absent.**
  One authoritative publisher plus a second publisher agreeing. `missing_evidence`
  is still reported in full — so a `strong_fit` backed by a registry notice says
  plainly that the company's own page was never read, instead of blanking its
  gaps the way the old code did.
- The bar did not move: a registry notice alone is still `review` (one domain),
  two directory listings with no authority are still `review`, a lower band is
  never upgraded, and conflicts still block.

Verified by reverting: four tests fail without it. 469 server tests and six
WebUI suites pass with it.

### B3. Completeness is measured against what the sources can supply

Found while testing B2: fixing the verdict left the same company blocked one
step later. `evidence_confidence` weights `completeness` at .2 and completeness
divided by all seven scoring dimensions — but four of them (`buying_intent`,
`market_coverage`, `commercial_scale`, `trade_activity`) have **no field that
any shipped verifier emits**. No company could exceed 3/7, a company with no
website could not exceed 2/7, and every lead's confidence was understated by the
same fixed amount. Because the understatement was uniform it never changed the
*ordering* — it just sat every lead lower against band thresholds that had
plainly been calibrated for a metric able to approach 1.

A source now declares the claim fields it can produce, and completeness is
measured against the dimensions those fields can reach.

- **Declared per source, not inferred.** Capabilities are too coarse to answer
  this: `candidate_verification` says a source verifies companies, not that it
  can ever speak to their store count. `DatasetDefinition.emits` carries the
  fields, populated for all three sources from what their verifiers actually
  emit, and the in-code fallback definitions are kept in step with the catalog.
- **Availability, not just enablement.** A credential-gated source emits
  nothing, so counting its fields would understate completeness exactly as the
  fixed denominator did.
- **An undeclared fact can only help.** The denominator is the declared set
  unioned with what was actually found, so a source emitting something it never
  declared adds to both sides instead of being dropped from the numerator —
  otherwise a lead would score worse for carrying extra evidence.
- **An empty declaration falls back to all seven.** Undeclared means nobody
  said, not nothing is reachable; treating it as the denominator would score a
  lead with one claim as fully complete.
- A gap the sources *could* have filled still costs. A company with no website
  is missing `contactability`, and every source declares a domain, so that is a
  real gap at 2/3 rather than an excused one.

Measured effect, with TED and Bright Data enabled — fit is untouched, and the
tiers still separate C / B / A:

| Lead | Before | After |
|---|---|---|
| no website, notice + two pages | conf .713, band B | conf .789, band A |
| website, notice + two pages | conf .766, band A | conf .880, band A |
| one thin snippet | conf .677, band C | conf .753, band C |

`attainable_dimensions` is also recorded in the run metrics, so a campaign says
which dimensions its sources could speak to at all instead of leaving the
question invisible.

Verified by reverting: three tests fail without it. 477 server tests and six
WebUI suites pass with it.

### C2. A rerun stops paying twice for evidence it already holds

Verifying one candidate is three Web Unlocker fetches, and a rerun re-fetched
every page it had already bought. `refresh.reuse_public_cache` has been in the
config and read by nobody since it was added.

Evidence is immutable and content-addressed, so the stored rows rebuild the
bundle the provider would have returned — same provenance, same hashes, same
facts, therefore the same claims and the same verdict. Reuse changes the cost,
not the answer, and there is a test comparing full result rows across a reused
rerun to say so.

- **Read once per run**, not once per candidate: a single query where the naive
  shape would be a query per candidate per source.
- **Each source's own `freshness_days` sets its window.** TED's 7 days and a
  customer corpus's 365 are different claims about how fast the underlying
  record changes, and that field already says so. It had no reader before this.
- **A reused bundle does not restamp `retrieved_at`.** `save_evidence` skips a
  row it already has, so the window expires on the age of the evidence rather
  than the age of the last run that looked at it. Cached evidence that
  refreshed its own timestamp would never be re-fetched again.
- Withdrawn evidence is never reused, so purging a source still costs its
  evidence, and the read is tenant-scoped.

**The trap this would otherwise have set, which is the real content of this
change.** Evidence facts are not a property of the page. A web verifier emits
`product_term` and `buyer_role` by matching *the campaign's own terms* against
what it fetched, and after B1 fit is scored on how many matched. Caching on
(source, candidate) alone would mean an edited campaign silently reused evidence
extracted under its old terms — and editing a campaign then rerunning it is
precisely how this system gets tuned, as the four bugs above this section
record. So a bundle carries a fingerprint of the question it answered, and reuse
requires that fingerprint to match. Term *order* does not affect it, target
countries are excluded deliberately (extraction keys off the candidate's own
country, fixed by the immutable corpus row), and anything that changes
extraction — sector ids, HS codes, buyer types — does.

Evidence written before this change carries no fingerprint and is never reused,
so the cache starts cold rather than starting wrong.

Known limits, both deliberate:

- **Abstentions are not cached.** A source that fetched pages and found nothing
  stored no evidence, so it is asked again next run. Caching a negative would
  mean storing evidence of absence, which is a different contract than
  `evidence_records` currently has.
- **A provider swapped behind an unchanged campaign is invisible** while its
  evidence is fresh, which is correct — but it means a test that changes a
  verifier's behaviour has to turn the cache off, and one does.

Verified by reverting: three tests fail with reuse disabled, and the
fingerprint guard has its own test that fails when the check is removed. 492
server tests and six WebUI suites pass.

### C1. A run says what it spent

Nothing counted an outbound request. `agent_runs.cost` was inserted as 0 and
never updated, while this document reasons about 90 requests versus 16,500 and
the entire cost model of the system is Web Unlocker fetches. A run could not
answer the one question an operator has about it.

**The provider reports its own spend**, on the bundle, because only the provider
knows: one `verify` is zero fetches for a local corpus, one search for TED, two
when TED backs off a 429, and up to four for Bright Data (an official page plus
three searches). Reported per call rather than counted on the instance —
providers are shared singletons and campaigns run concurrently, so an instance
counter would attribute one tenant's spend to another.

- **A bundle rebuilt from stored evidence reports 0**, which is exactly true, so
  the saving from C2 shows up as a number instead of a claim. There is a test
  that a reused rerun reports zero spend.
- **An abstention still reports what it spent.** The pages were fetched whether
  or not anything in them was usable; counting only successful bundles would
  make the runs that found nothing look like the cheapest ones.
- **The enrichment pass is counted too**, including bundles whose sources were
  all already cited — those pages were still fetched.
- Reported per run (`provider_requests`) and per partition, so spend can be
  attributed to the source that incurred it.

**`agent_runs.cost` is deliberately left alone.** That column is model spend, and
`/admin/analytics/costs` documents itself as such. Requests and tokens are
different units and a single number summing both would mean nothing, so the
endpoint gained a separate `provider_requests` field with its own
`provider_requests_metered: true`, beside the existing `metering_enabled: false`
that still honestly reports token spend as unmeasured. The rollup sums in Python
rather than with `json_extract`, matching two existing comments in this codebase
about that function being spelled differently on Postgres.

A run's total is a **floor**: a `verify` that raises after spending never
returns its bundle, so those requests are not counted. Failures are recorded per
partition, so an unusually cheap run with errors is legible as one.

Two UI fixes came with it. The campaign page now has a "What this run cost" card
beside the funnel — requests are not a funnel stage, that list is monotonic and
its bars are scaled against `raw_records`. And the Source progress table was
reading `records`, `normalized` and `eligible`, **keys nothing has ever
written**, so every cell in it rendered as a dash; it now reads the keys the run
actually stores, including the new per-partition reuse and request counts.

Verified by reverting: three tests fail without it. 504 server tests and six
WebUI suites pass.

### C3. Candidates are verified concurrently; everything else stays serial

Three fetches per candidate at a 45-second timeout, one candidate at a time,
made a 150-candidate market roughly 450 sequential round trips. That was the
dominant wall-clock cost in the system.

**Concurrency is confined to the network.** `_collect_bundles` is the only thing
that runs on a worker thread, and it deliberately touches nothing shared: it
returns pure data, and the caller applies every partition counter and every
write on the campaign's own thread. So all database work stays single-threaded,
and identity resolution — which reads before it writes, so two candidates
resolving to one company must arrive one after the other — is untouched.

Candidates are fetched in batches of `verify_workers` (default 4) and then
consumed **in candidate order**. Order is restored deliberately: a run rebuilds
its results from scratch, so their creation order decides which row identities a
refresh preserves. `verify_workers=1` restores strictly serial behaviour, and a
test pins that a concurrent run and a serial one reach identical results.

**A per-source cap, because concurrency would otherwise have broken TED.** TED
answers 429 readily enough that its adapter already carries a backoff; four
workers hitting it at once converts a working free source into a failing one. So
`max_concurrency` is declared per source in the provider catalog — a property of
the upstream, not of our appetite — and enforced by a semaphore shared across
campaigns, since the limit protects the upstream rather than any one run. TED
declares 1; a web unlocker is sold for concurrent use and declares 4. A test
proves a capped source is never entered twice at once *and* that it does not
serialise the uncapped source behind it.

**Bright Data now retries once**, on the statuses that are about the moment
rather than the page — 429 and the upstream's own 5xx, matching what the TED
adapter already did. A single 429 used to lose the whole candidate, and a
candidate is the unit a campaign reports on, so one transient refusal read as
"no source could vouch for this company". A 404 is not retried: that is about
the page. `_fetch_markdown` returns its attempt count rather than assuming one,
because every attempt is billable and metering that guessed would understate
spend exactly when a run was struggling.

**The trade this buys, stated plainly: a cancel is now bounded by one batch
rather than one candidate.** Work already dispatched finishes — a fetch in
flight cannot be unsent — so up to `verify_workers` candidates continue after a
cancel instead of one. That is at most a batch's worth of requests against a
market's worth, and the test says so in its name.

Also fixed: several tests asserted an ordered list of the candidates a provider
had been asked about. Order is no longer a contract, so those now compare
sorted — a latent flake removed rather than discovered later.

Verified by reverting: forcing verification serial deadlocks the two barrier
tests, which is the point of using barriers rather than timing observations.
515 server tests pass, four consecutive full runs with no flake, plus six WebUI
suites.

### C4. Work that grew with the tenant instead of with the batch

Three lookups, each measured rather than assumed.

**The lead table was read once per qualifying candidate.** `organization_id`
lives inside a lead's JSON payload, so finding one meant reading every lead row
and decoding every payload — and doing it again for the next candidate. On the
measured shape (396 candidates, 173 leads) that is **168 ms of pure JSON
decoding per run, now 0.7 ms: 230× faster**, and it was growing with the square
of the tenant. Built once per run instead, the way `prior_results` already was.
The index is kept current *inside* the run, because two candidates can resolve to
one organization and the second must update the first's lead rather than insert
a duplicate — there is a test for exactly that.

Not pushed into SQL: extracting from JSON is spelled differently on Postgres,
and one pass over a tenant's lead table costs nothing next to a single fetch.

**Superseded corpus rows were fetched, decoded and term-matched before being
discarded.** The version filter now sits in the query, so a tenant holding a
corrected corpus beside its original stops paying twice for every row to use
half of them. Measured on 10,940 rows across two versions: 1.4 ms → 1.1 ms, and
1.0 ms once SQLite has statistics — modest, but the wasted work is gone rather
than moved. Both columns are in the existing country index; nothing here needs a
new one, and after `ANALYZE` the planner picks that index over the primary key.

**The evidence-reuse read from C2 scanned every evidence row the tenant owns.**
The existing tenant index leads with `campaign_id`, which that lookup
deliberately does not filter on. A `(company_id, source_id, retrieved_at)` index
fixes it, and a test asserts the query plan actually names it rather than
trusting that it would.

Deliberately left alone: `save_evidence` checks for an existing row once per
evidence record. That is an N+1 in query *count*, but each one is a point lookup
on the table's own `UNIQUE` constraint — confirmed with `EXPLAIN QUERY PLAN` —
so it is O(log n) and free next to a fetch. The test that counts the reuse read
says so, so nobody re-discovers it as a defect.

These tests pin the *shape* of the work — how many times a table is read — not
its speed. A timing assertion would be flaky on a loaded machine and would not
say what broke.

Verified by reverting all three: three tests fail. 522 server tests pass across
three consecutive runs, plus six WebUI suites.

## Carried-over risks

- **Postgres paths stay under-tested.** `tests/server/` builds `Settings` with
  `database_path` only, so every test runs SQLite. Four fatal Postgres-only bugs
  have shipped so far. `tests/server/test_postgres_backend.py` is where
  contracts that need no live Postgres belong. Still unverified in that file:
  `_sql()` rewrites every `?` to `$n` including inside string literals, and
  `jsonb` columns are fed JSON strings rather than dicts.
- **`agent.tugrap.dev` does not exist** — no DNS, no cert. Fine while nothing
  sends, because unsubscribe URLs are baked absolute into delivered mail. Must
  be settled before the first outbound email. See `TODO.md`.
- **No OAuth credentials configured**, so Integrations cannot connect. Email
  demos are draft-mode only.
- **`/data/config.yaml` is hand-trimmed**, not the image default (2,295 vs
  3,357 bytes), and omits the `document_*` tuning keys, which therefore run on
  code defaults. `cors_origins` lists only `agent-rota.fly.dev`.
- **One machine, one volume.** Uploads land on that volume unless
  `SUPABASE_SERVICE_ROLE_KEY` is set; set it before scaling past one machine.
