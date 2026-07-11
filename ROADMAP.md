# interfaze-agent — remaining release work

The agent/backend implementation of PRODUCT.md is now in `server/`; the
customer dashboard is developed in a separate repository.

## Implemented in this repository

- Hermes runtime plus eight company-agnostic Sales Agent skills.
- Company configuration schema and scrubbed Silverline demo pack.
- FastAPI `/api/v1` service with every PRODUCT route.
- Local SQLite and Supabase Postgres/RLS/Storage deployment backends.
- Local and Supabase authentication, admin-managed companies/users, tenant
  enforcement, onboarding, and activity history.
- All 11 run types with validated payloads/outputs, background execution,
  streamed events, cancellation, retry, and deterministic persistence.
- Documents, products, Company Brain versions/approval, lead scans, research,
  contacts, scoring, campaigns, custom outreach, analytics, and CSV exports.
- Gmail, Microsoft Graph, WhatsApp Business Cloud, and manual LinkedIn paths.
- Immutable outreach revisions, deterministic preflight, approval-bound sends,
  provider idempotency, local send windows, daily caps, reply polling, and a
  bounce-rate campaign circuit breaker.
- Dashboard handoff contract in `server/UI_INTEGRATION.md`.

## Release gates requiring external systems

- [ ] Apply and verify `server/supabase/migrations/001_initial.sql` in staging,
      including customer-vs-customer and customer-vs-admin RLS tests.
- [ ] Complete Gmail OAuth sandbox tests: connect, refresh, draft, approved
      send, status lookup, and reply polling.
- [ ] Complete Microsoft Graph sandbox tests for the same lifecycle.
- [ ] Complete Meta test-number tests: template send, webhook status, opt-out,
      and ambiguous-timeout/no-duplicate behavior.
- [ ] Execute the Silverline acceptance chain against the selected production
      model and store scrubbed fixtures for every run output contract.
- [ ] Refresh `uv.lock` after adding the `interfaze` asyncpg extra. The current
      workspace approval system rejected cache access because it was out of
      credits; do not release with a stale lockfile.
- [ ] Rotate every credential that appeared in the prototype repository before
      production deployment.

## Dashboard handoff

- [ ] Generate TypeScript types from `/openapi.json`.
- [ ] Configure dashboard CORS origin under `interfaze_server.cors_origins`.
- [ ] Implement bearer-token refresh and `X-Company-ID` only for admin tenant
      views.
- [ ] Poll agent-run events/logs and render safety-gate `409/422/429` responses
      as actionable UI states.
- [ ] Make draft the default and direct send a separate confirmation action.

## Post-MVP

- Zoho and SMTP provider adapters (email v1.1).
- Managed sender providers (Resend, Mailgun, SendGrid, Brevo, SES) in v1.2.
- Licensed enrichment sources behind `data_sources` toggles.
- Additional Company Brain agent families described in
  `product-architecture.md`.

