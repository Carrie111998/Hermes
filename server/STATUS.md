# Sales Agent backend status

Status as of 2026-08-17.

## Code-complete surfaces

- All 216 API method/path contracts in PRODUCT.md are exposed and checked
  against generated OpenAPI.
- Local auth plus Supabase GoTrue login/token validation/refresh/logout/reset.
- Admin-managed companies and users with tenant-scoped customer access.
- Onboarding, documents, products, versioned Company Brain snapshots.
- Lead map, scans, leads, research, scoring, and contact discovery.
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

## Run types

| Run type | Product path |
|---|---|
| document_processing | uploaded document → Hermes skill → validated records/rejects |
| product_extraction | selected documents → deduplicated products |
| company_brain_build | tenant DB context → versioned draft → human approval |
| lead_scan | server market gate → Hermes discovery → tenant leads |
| lead_research | lead + approved brain → persisted insights/score inputs |
| contact_discovery | buyer roles → passively validated contacts |
| outreach_generation | research context → deterministic QA → pending approval |
| email_send | approved revision → provider adapter → recorded deterministic run |
| whatsapp_send | approved revision → Cloud API → recorded deterministic run |
| linkedin_note_generation | canonical profile + note → manual action record |
| analytics_refresh | deterministic database aggregation; no model |

## Local release evidence

- `tests/server/test_clean_demo_e2e.py` provisions a fresh database, logs in as
  the customer, imports one product and a backend corpus, proves candidate
  isolation, runs an injected verifier, and asserts sourced active/rejected
  result separation.
- `scripts/ci/interfaze_clean_demo_smoke.py` repeats the public-HTTP boundary
  against a deployed service using an owner-restricted password file.
- The Interfaze workflow runs the complete server suite and five focused WebUI
  suites, checks `uv.lock`, installs wheel and sdist in clean environments, and
  builds/boots the Docker image.
- Wheel, sdist, and image gates reject `server/demo_seed.py`, Silverline demo
  data, WebUI mock handlers, and a production fixture provider.

## External release gates

These require credentials or infrastructure and cannot be claimed from a
credential-free checkout:

1. Apply the Supabase migration and run cross-tenant RLS tests on a hosted
   project.
2. Complete OAuth sandbox delivery tests for Gmail and Microsoft Graph:
   create draft, send approved message, refresh token, read reply/status.
3. Complete WhatsApp test-number delivery/webhook/ambiguous-timeout tests.
4. Run the clean Silverline demo account with the supplied product catalog,
   private kitchen-appliance corpus, and Bright Data credential; record the
   post-deploy smoke result without committing the inputs or credentials.
5. Record and qualify live official-source adapters independently; catalog-only,
   manual-import, credentialed, and retired entries do not claim live acquisition.
