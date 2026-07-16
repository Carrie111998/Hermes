# Sales Agent backend status

Status as of 2026-07-16.

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
  impact/purge handling. The offline fixture provider qualifies the full path;
  public/credentialed source entries remain access-gated until adapters are configured.
- Campaigns, custom outreach, revision-bound approvals, deterministic QA,
  draft/send modes, market CC rules, send limits/windows, and delivery
  idempotency.
- Gmail, Microsoft Graph, and WhatsApp Business Cloud adapters.
- Manual-only LinkedIn profile/note workflow.
- Analytics, CSV exports, data sources, activity logs, run logs/events.
- SQLite local backend and Supabase Postgres/RLS/Storage deployment path.
- Installable `interfaze-api` entry point and packaged server/company packs.
- Packaged same-origin WebUI that uses the real API by default, with mock mode
  available only when explicitly selected for frontend development.
- Tenant-scoped Ask Hermes SSE bridge with restricted agent turns, durable
  chat history, single-use stream capabilities, and concurrent-turn control.
- Real WebUI multipart uploads, authenticated CSV downloads, dashboard-shaped
  analytics, promoted onboarding/admin/WhatsApp routes, and server-side list filters.

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

## Local evidence

- `tests/server/test_api_mvp.py`: 9 API qualification checks.
- `tests/server/test_run_harness.py`: 7 production run-service checks.
- `tests/server/test_webui.py`: 16 serving, security, connection, core-flow,
  chat-isolation, and long-tail checks.
- `tests/server/test_demo_seed.py`: 2 tenant seed and local draft-flow checks.
- `tests/server/lead_research/` and `tests/server/test_research_webui.py`: 10
  evidence-contract, tenant-isolation, refresh, source-removal, API, and WebUI checks.
- Full `tests/server` qualification: 40 passed.
- PRODUCT route comparison: 216/216.
- Python compile pass for `server/` and `tests/server/`.
- The persisted local Silverine profile passed an API smoke test for customer
  login, tenant scoping, 25 seeded leads, approval-to-local-draft, health, and
  browser security headers. The database was reset afterward for clean testing.

## External release gates

These require credentials or infrastructure and cannot be claimed from a
credential-free checkout:

1. Apply the Supabase migration and run cross-tenant RLS tests on a hosted
   project.
2. Complete OAuth sandbox delivery tests for Gmail and Microsoft Graph:
   create draft, send approved message, refresh token, read reply/status.
3. Complete WhatsApp test-number delivery/webhook/ambiguous-timeout tests.
4. Run the Silverine document→brain→scan→research→contact→email acceptance
   chain with the production model and record its fixtures.
5. Refresh `uv.lock`; this workspace could not access uv's cache because its
   approval/credit gate rejected the operation.
6. Record and qualify live official-source adapters independently; catalog-only,
   manual-import, credentialed, and retired entries do not claim live acquisition.
