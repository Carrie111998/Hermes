# interfaze-agent product API

The agent/backend implementation of the Sales Agent MVP in `PRODUCT.md`.
The customer dashboard is a separate repository and consumes this service at
`/api/v1`.

## Start locally

Configure non-secret behavior in `~/.hermes/config.yaml`:

```yaml
interfaze_server:
  auth_mode: local
  cors_origins:
    - http://localhost:3000
    - http://localhost:5173
```

Bootstrap the first administrator with deployment secrets, then start:

```bash
export INTERFAZE_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD='use-a-password-manager'
export INTERFAZE_CREDENTIAL_KEY='a-valid-Fernet-key'
interfaze-api --host 127.0.0.1 --port 8000
```

OpenAPI is available at `/openapi.json` and interactive API docs at `/docs`.

## Production / Supabase

Apply `server/supabase/migrations/001_initial.sql` and configure:

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
- `outreach_service.py` — immutable approval revisions, preflight QA,
  provider idempotency, send windows/caps, reply polling, and bounce circuit.
- `email_providers/` — Gmail, Microsoft Graph, and test adapter.
- `whatsapp_provider.py` — Meta WhatsApp Business Cloud API.
- `routes/` — all 207 method/path contracts specified by PRODUCT.md.
- `quality.py` — deterministic rules ported from the prototype QA scripts.

The Hermes runtime generates research and content. It never owns authorization,
tenant IDs, database IDs, approvals, or provider-send decisions.

## Qualification

```bash
python tests/server/test_api_mvp.py
python tests/server/test_run_harness.py
```

The local suite is credential-free. Release qualification additionally
requires sandbox runs against Gmail, Microsoft Graph, WhatsApp Cloud, and a
hosted Supabase project.

