# Sales Agent backend status

Status as of 2026-07-11.

## Code-complete surfaces

- All 207 API method/path contracts in PRODUCT.md are exposed and checked
  against generated OpenAPI.
- Local auth plus Supabase GoTrue login/token validation/refresh/logout/reset.
- Admin-managed companies and users with tenant-scoped customer access.
- Onboarding, documents, products, versioned Company Brain snapshots.
- Lead map, scans, leads, research, scoring, and contact discovery.
- Campaigns, custom outreach, revision-bound approvals, deterministic QA,
  draft/send modes, market CC rules, send limits/windows, and delivery
  idempotency.
- Gmail, Microsoft Graph, and WhatsApp Business Cloud adapters.
- Manual-only LinkedIn profile/note workflow.
- Analytics, CSV exports, data sources, activity logs, run logs/events.
- SQLite local backend and Supabase Postgres/RLS/Storage deployment path.
- Installable `interfaze-api` entry point and packaged server/company packs.

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
- PRODUCT route comparison: 207/207.
- Python compile pass for `server/` and `tests/server/`.

## External release gates

These require credentials or infrastructure and cannot be claimed from a
credential-free checkout:

1. Apply the Supabase migration and run cross-tenant RLS tests on a hosted
   project.
2. Complete OAuth sandbox delivery tests for Gmail and Microsoft Graph:
   create draft, send approved message, refresh token, read reply/status.
3. Complete WhatsApp test-number delivery/webhook/ambiguous-timeout tests.
4. Run the Silverline document→brain→scan→research→contact→email acceptance
   chain with the production model and record its fixtures.
5. Refresh `uv.lock`; this workspace could not access uv's cache because its
   approval/credit gate rejected the operation.
