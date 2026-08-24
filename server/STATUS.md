# Sales Agent backend status

Status as of 2026-08-24.

## Code-complete surfaces

- All 216 API method/path contracts in PRODUCT.md are exposed and checked
  against generated OpenAPI.
- Local auth plus Supabase GoTrue login/token validation/refresh/logout/reset.
- Admin-managed companies and users with tenant-scoped customer access.
- Onboarding, documents, products, versioned Company Brain snapshots.
- Lead map, scans, versioned company research profiles, leads, research,
  weighted scoring with explicit unknown weight, and on-demand contact discovery.
- Evidence-first Research workspace with tenant campaign drafts, source catalog,
  immutable local snapshots, canonical claims, separate fit/confidence scoring,
  ordered funnel metrics, evidence inspection, CSV export, and source lifecycle
  impact/purge handling. Active lists exclude rejects; rejected results load in
  their own selected tab. Production source entries remain access-gated until
  adapters are configured; deterministic provider fakes exist only under tests.
- Shared, versioned kitchen-appliance candidate corpus import that creates no
  tenant rows until evidence verification, plus opt-in Bright Data verification.
- Tenant-scoped atomic CSV/JSON product catalog import from the WebUI.
- Five-point dynamic scoring weights that preserve a total of 100.
- Campaigns, custom outreach, revision-bound approvals, deterministic QA,
  draft/send modes, market CC rules, send limits/windows, and delivery
  idempotency.
- Gmail, Microsoft Graph, and WhatsApp Business Cloud adapters.
- Manual-only LinkedIn profile/note workflow.
- Analytics, CSV exports, data sources, activity logs, run logs/events.
- SQLite local backend and Supabase Postgres/RLS/Storage deployment path.
- Installable `interfaze-api` entry point and packaged server assets. Tenant
  company packs, demo seeds, WebUI mocks, and fixture providers are excluded
  from release artifacts.
- Packaged same-origin WebUI that uses the real API only; frontend development
  exercises the same backend contract as production.
- Tenant-scoped Ask Hermes SSE bridge with restricted agent turns, durable
  chat history, single-use stream capabilities, and concurrent-turn control.
- Real WebUI multipart uploads, authenticated CSV downloads, dashboard-shaped
  analytics, promoted onboarding/admin/WhatsApp routes, and server-side list filters.
- Clean demo provisioning completes onboarding but starts with zero products,
  countries, leads, contacts, research, campaigns, messages, or outreach.
- Research results present verdicts, scores, claims, citations, snapshot IDs,
  hashes, and retrieval timestamps without starting outreach from the workspace.
- Lead research now unions service-public candidates with only the authenticated
  tenant's private uploads, verifies identity before scoring, reuses only fresh
  mechanically validated facts, routes unresolved weighted criteria through
  bounded durable agent runs, and stores immutable result/score snapshots.
- Public official/registry facts are content-addressed and reusable across
  tenants; profiles, candidate uploads, weights, labels, scores, lead lists,
  suppressions, and licensed/customer facts remain tenant-scoped. Shared-fact
  correction impact is previewable and appends corrected result snapshots.
- Empty campaigns expose one named outcome (`no_candidate_source_runnable`,
  `product_terms_missing_local_mapping`, `sources_named_no_candidate`,
  `candidates_excluded_by_range`, `candidates_failed_eligibility`,
  `researched_below_threshold`, `sources_failed`, or `campaign_cancelled`).
- Contact evidence is stored as green/yellow/red with person/generic kind.
  Outreach never sends a verification email, never auto-selects red contacts,
  and allows only unsuppressed green person contacts in CC.

## Run types

| Run type | Product path |
|---|---|
| document_processing | uploaded document → Hermes skill → validated records/rejects |
| product_extraction | selected documents → deduplicated products |
| company_brain_build | tenant DB context → versioned draft → human approval |
| lead_scan | server market gate → Hermes discovery → tenant leads |
| lead_research | lead + approved brain → persisted insights/score inputs |
| lead_research_gap | unresolved weighted criteria → bounded cited pages/facts |
| lead_research_refresh | stale consumed fact → bounded read-only refresh run |
| contact_discovery | buyer roles → passively validated contacts |
| outreach_generation | research context → deterministic QA → pending approval |
| email_send | approved revision → provider adapter → recorded deterministic run |
| whatsapp_send | approved revision → Cloud API → recorded deterministic run |
| linkedin_note_generation | canonical profile + note → manual action record |
| analytics_refresh | deterministic database aggregation; no model |

## Local release evidence

- `tests/server/test_clean_demo_e2e.py` provisions a fresh database, logs in as
  the customer, explicitly confirms the research profile, imports one product
  and a backend corpus, proves candidate isolation, runs an injected verifier,
  and asserts sourced active/rejected result separation and the complete result
  scoring/evidence contract.
- `tests/server/test_lead_research_contract_e2e.py` proves two authenticated
  tenants on a clean database: public/private candidate union, shared public
  fact reuse, different tenant-weighted decisions, durable agentic fallback,
  exact spans, hidden-label and suppression isolation, correction propagation,
  contact/CC safeguards, cancellation, and every named zero-result outcome.
- `scripts/ci/interfaze_clean_demo_smoke.py` defaults to a read-only clean-state
  check against a deployed service using an owner-restricted password file.
  Its mutating full rehearsal requires an email-matched disposable-tenant
  confirmation and refuses the reusable Silverline demo account.
- The clean-demo E2E executes the full operational smoke branch against the
  real FastAPI routes with only the external verifier injected from test code.
- The Interfaze workflow runs the complete server suite and five focused WebUI
  suites, checks `uv.lock`, installs wheel and sdist in clean environments, and
  builds/boots the Docker image.
- Wheel, sdist, and image gates reject `server/demo_seed.py`, Silverline demo
  data, WebUI mock handlers, and a production fixture provider.

## External release gates

These require credentials or infrastructure and cannot be claimed from a
credential-free checkout:

1. Apply Supabase migrations through `020_lead_research_contract_backfill.sql`,
   run `python -m server backfill-lead-research-contract`, then execute
   `server/supabase/verify.sql` and hosted cross-tenant RLS tests. Rollback is
   application-first; do not drop compatibility columns before the old binary
   is restored.
2. Complete OAuth sandbox delivery tests for Gmail and Microsoft Graph:
   create draft, send approved message, refresh token, read reply/status.
3. Complete WhatsApp test-number delivery/webhook/ambiguous-timeout tests.
4. Run the clean Silverline demo account with the supplied product catalog,
   private kitchen-appliance corpus, and Bright Data credential; record the
   post-deploy smoke result without committing the inputs or credentials.
5. Record and qualify live official-source adapters independently; catalog-only,
   manual-import, credentialed, and retired entries do not claim live acquisition.
