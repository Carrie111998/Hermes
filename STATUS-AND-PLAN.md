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
2. **Import at least one product.** Candidates are matched against product
   terms, so an empty catalog produces an empty funnel no matter how good the
   corpus is.

### Scope the campaign, always

`MAX_SEARCH_PAGES = 3`, so each candidate costs up to three Web Unlocker
fetches. All 5,470 is roughly 16,500 requests. `select()` takes both a country
filter and a limit — use them. Thirty candidates in one market is about 90
requests and finishes while you watch.

### Housekeeping

`rm /data/secure/demo-password` once the password is in the manager. The volume
is snapshotted.

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

### 1–2. Closed state and incremental selection — DONE, not deployed

`2ec40dcad6`. Selection skips identities the tenant has settled. Settled means
country and buyer role both claimed inside the source's declared
`freshness_days` (30), or a `lifecycle_status` claim saying closed.

- Closure is read latest-first, so a later `operating` claim reopens a wrongly
  retired company. Closure is not a one-way door.
- The verifier emits the claim from 17 narrow multi-word phrases, gated on an
  identity match. Bare "closed" is ordinary prose ("closed on Sundays", "closed
  a funding round") and a false positive removes a live company from every
  future run.
- Skip counts are `excluded_closed` and `skipped_validated`, deliberately
  outside `FUNNEL_KEYS` — that funnel is monotonic and these describe work never
  started.
- No migration. A new column would have duplicated `feature_claims` and lost the
  evidence binding.

Two known limits. Nothing writes `operating` yet, so the reopen path needs a
human or a future extraction — an operator override in the UI is a small
follow-up. And closure is only detected when a campaign actually reaches a
company, so this makes reruns cheap, not the first pass.

### 3. Buyer-type vocabulary — NOT STARTED

Generalised across industries, not kitchen-appliance specific: distributor, OEM,
importer, wholesaler, retailer, HORECA supplier, contractor, e-commerce.
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
