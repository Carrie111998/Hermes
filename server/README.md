# interfaze-agent product API

The agent/backend implementation of the Sales Agent MVP in `PRODUCT.md`.
The packaged customer dashboard is served by this process at `/` and consumes
the product API at `/api/v1`.

## Start locally

Configure non-secret behavior in `~/.hermes/config.yaml`:

```yaml
interfaze_server:
  auth_mode: local
  cors_origins:
    - http://localhost:3000
    - http://localhost:5173
  webui_enabled: true
  max_upload_bytes: 26214400  # 25 MiB; 0 disables the size limit
  chat_enabled: true
  chat_model: ""       # empty uses the configured Hermes default
  chat_toolset: none   # allowed: none, search, web; anything else fails closed
```

Bootstrap the first administrator with deployment secrets, then start:

```bash
export INTERFAZE_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD='use-a-password-manager'
export INTERFAZE_CREDENTIAL_KEY='a-valid-Fernet-key'
interfaze-api --host 127.0.0.1 --port 8000
```

OpenAPI is available at `/openapi.json` and interactive API docs at `/docs`.

## Connect Gmail and Microsoft 365

Configure the public origin, credential-encryption key, and provider-issued
OAuth credentials in the deployment environment:

```bash
export INTERFAZE_PUBLIC_BASE_URL='https://interfaze.example.com'
export INTERFAZE_CREDENTIAL_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

export GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:?load the provider-issued id from the deployment secret manager}"
export GOOGLE_OAUTH_CLIENT_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET:?load the provider-issued secret from the deployment secret manager}"

export MICROSOFT_OAUTH_CLIENT_ID="${MICROSOFT_OAUTH_CLIENT_ID:?load the provider-issued id from the deployment secret manager}"
export MICROSOFT_OAUTH_CLIENT_SECRET="${MICROSOFT_OAUTH_CLIENT_SECRET:?load the provider-issued secret from the deployment secret manager}"
export MICROSOFT_OAUTH_TENANT='common'
```

`INTERFAZE_PUBLIC_BASE_URL` must be the public HTTPS origin serving both the
packaged WebUI and API. Register these exact redirect URIs with the providers:

```text
https://interfaze.example.com/api/v1/integrations/email/oauth/google/callback
https://interfaze.example.com/api/v1/integrations/email/oauth/microsoft/callback
```

Google requires the Gmail modify scope and offline consent. Microsoft requires
`offline_access Mail.ReadWrite Mail.Send User.Read`. Provider client secrets
are deployment secrets and must never be configured in the browser.
`MICROSOFT_OAUTH_TENANT` defaults to `common`.

Demo checklist:

1. Start the API on the same public origin registered with the provider.
2. Sign in and open Integrations.
3. Click Google Workspace or Microsoft 365 Connect and allow the popup.
4. Complete consent and verify the popup closes.
5. Verify the card changes to `connected` without reloading the browser page.
6. Click Test to verify provider scopes.

Live-provider consent stays outside automated CI. The automated suite must
never call Google or Microsoft.

## Seed the tenant-backed test client

For local product testing, seed the deterministic Silverine client into the
configured local database. The command replaces only the `company_silverline`
tenant and leaves every other company untouched:

```bash
python -m server seed-demo \
  --email client@silverline.test \
  --password silverline-test-123
```

Then sign in at `/` with those credentials. The profile contains 7 products,
5 historical document records, 25 leads, 14 contacts, 2 campaigns, 10 outreach
messages, and 16 completed agent runs. Historical document records cannot be
reprocessed until their source files are uploaded again.

## Production / Supabase

Apply `server/supabase/migrations/001_initial.sql` for a fresh database. Existing
installations also apply `server/supabase/migrations/002_chat_sessions.sql` and
`server/supabase/migrations/003_lead_research.sql`.
Then configure:

```text
SUPABASE_DB_URL
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY
INTERFAZE_CREDENTIAL_KEY
```

Then set `interfaze_server.auth_mode: supabase`. Supabase bearer tokens are
validated against GoTrue; users must already have been provisioned by an
admin in the product database. Documents use the private
`interfaze-documents` bucket. See `server/supabase/README.md`.

## Architecture

- `app.py` — FastAPI factory and dependency wiring.
- `db.py` / `postgres.py` — local SQLite and Supabase Postgres backends.
- `agent_service.py` — queued runs, subprocess streaming, cancellation,
  retries, output contracts, and deterministic domain persistence.
- `chat_bridge.py` — tenant-scoped WebUI sessions, restricted one-turn agents,
  and single-use SSE stream capabilities.
- `lead_research/` — canonical sectors/evidence, provider registry, immutable
  tenant snapshots, identity/eligibility, fit/confidence scoring, metrics, and
  deterministic fixture acquisition. Cataloged external sources stay gated by
  their real access mode instead of falling back to unsupported scraping.
- `outreach_service.py` — immutable approval revisions, preflight QA,
  provider idempotency, send windows/caps, reply polling, and bounce circuit.
- `email_providers/` — Gmail, Microsoft Graph, and test adapter.
- `whatsapp_provider.py` — Meta WhatsApp Business Cloud API.
- `routes/` — all 216 PRODUCT.md contracts, including promoted WebUI convenience routes,
  and the service-gated chat bridge.
- `quality.py` — deterministic rules ported from the prototype QA scripts.

The Hermes runtime generates research and content. It never owns authorization,
tenant IDs, database IDs, approvals, or provider-send decisions.

## Qualification

```bash
scripts/run_tests.sh tests/server/
```

The local suite is credential-free. Release qualification additionally
requires sandbox runs against Gmail, Microsoft Graph, WhatsApp Cloud, and a
hosted Supabase project.
