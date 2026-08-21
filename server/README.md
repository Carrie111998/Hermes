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

Generate the Fernet credential-encryption key once, outside the runtime start
command:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store the printed value in a durable deployment secret manager. Every restart
and every replica must load the same stable `INTERFAZE_CREDENTIAL_KEY`.
Replacing it without first re-encrypting stored credentials makes existing
integrations unreadable.

At runtime, load that stable key together with the public origin and
provider-issued OAuth credentials:

```bash
export INTERFAZE_PUBLIC_BASE_URL='https://interfaze.example.com'
export INTERFAZE_CREDENTIAL_KEY="${INTERFAZE_CREDENTIAL_KEY:?load the stable Fernet key from the deployment secret manager}"

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

## Provision a clean demo account

For local product testing, provision a customer account with a completed setup
profile but no products, selected markets, leads, contacts, research, campaigns,
or other operational tenant data. The command is idempotent and refuses to
replace a tenant once operational data exists.

Store the account password in an owner-readable file and restrict it before
running the command. A repository-local `.interfaze-credentials/` directory is
ignored for operator convenience, but a deployment secret manager is preferred:

```bash
mkdir -p .interfaze-credentials
chmod 700 .interfaze-credentials
printf '%s\n' 'load-this-from-a-secret-manager' > .interfaze-credentials/demo-password
chmod 600 .interfaze-credentials/demo-password
python -m server provision-demo \
  --email demo-user@example.com \
  --password-file .interfaze-credentials/demo-password \
  --profile /secure/path/demo-profile.json
```

The profile file is a JSON object containing the public company profile and
the public sources used during onboarding:

```json
{
  "company_profile": {"name": "Example Company", "website": "https://example.invalid"},
  "onboarding_sources": [{"url": "https://example.invalid", "retrieved_at": 1.0}]
}
```

The command output contains account identifiers and onboarding status only; it
never includes the password. Provisioning does not import products, countries,
leads, contacts, research, campaigns, messages, or outreach.

## Import catalogs and the private candidate corpus

Customers import their own product catalog from the WebUI. The accepted CSV
columns include `product_name`, `category`, and `aliases`; JSON uses a
top-level `products` array. Import is tenant-scoped and atomic.

The kitchen-appliance candidate corpus is different: it is shared backend
input, never tenant seed data and never uploaded by the customer WebUI. Load it
on the application host before a research run:

```bash
python -m server import-candidates \
  --dataset-id kitchen-appliances \
  --version 2026-08 \
  --file /secure/path/kitchen-appliance-candidates.csv
```

Importing candidates alone creates no tenant leads, countries, campaigns, or
research results. A campaign materializes only evidence-backed results after a
configured verifier succeeds.

Import computes each row's `search_text` — the normalised string a product term
matches against — so selection reads it instead of rebuilding it per row on
every run. A corpus imported before that column existed still selects correctly,
by computing the value at read time; it just does not get the speedup. Fill it in
once, on the application host:

```bash
python -m server backfill-candidate-search
```

Idempotent and safe to skip: it buys speed, never correctness. It prints counts
only, never corpus rows. Add `--batch` to shrink the transaction size on a very
large corpus.

Bright Data is the optional live verifier. Put its API key in the deployment
secret manager as `BRIGHTDATA_API_KEY`; enable it and select its non-secret
zone in `interfaze_server` config:

```yaml
interfaze_server:
  brightdata_enabled: true
  brightdata_unlocker_zone: cli_unlocker
```

No Bright Data credential, candidate corpus, or customer catalog is bundled in
the wheel or container.

## Clean-demo release smoke

Before any WebUI product import or research action, the default post-deploy gate
can verify that the real demo account is still clean. This mode is read-only.
The password value stays out of arguments and process listings:

```bash
python scripts/ci/interfaze_clean_demo_smoke.py \
  --base-url https://interfaze.example.com \
  --email demo-user@example.com \
  --password-file .interfaze-credentials/demo-password
```

The default gate requires empty products, countries, leads, contacts, research,
campaigns, messages, and outreach, and makes no product or operational tenant
changes.

Full rehearsal intentionally imports one synthetic product and starts a
campaign. Started campaigns cannot be cleaned up, so **never run full mode
against Silverline, a customer demo account, or any reusable tenant**. Provision
a new disposable smoke tenant for every rehearsal, load the backend candidate
corpus, then repeat that disposable email as the explicit confirmation:

```bash
smoke_email='release-smoke-20260817@example.test'
python -m server provision-demo \
  --email "${smoke_email}" \
  --password-file .interfaze-credentials/disposable-smoke-password \
  --profile /secure/path/disposable-smoke-profile.json

python scripts/ci/interfaze_clean_demo_smoke.py \
  --base-url https://interfaze.example.com \
  --email "${smoke_email}" \
  --password-file .interfaze-credentials/disposable-smoke-password \
  --mode full \
  --confirm-disposable-tenant "${smoke_email}"
```

Full mode verifies product import, candidate isolation, research completion,
active/rejected separation, and HTTPS evidence metadata. The command refuses
full mode when the confirmation does not exactly match the login email, and it
unconditionally protects the Silverline demo email. CI executes the same full
script branch against the real FastAPI routes with a test-only verifier in
`tests/server/test_clean_demo_e2e.py`; no fake provider is wired into production.

## Production / Supabase

Apply every file in `server/supabase/migrations/` in numeric order, on a fresh
database and an existing one alike. The directory is the list — do not enumerate
it here, because an enumerated list goes stale and a skipped migration is how a
database ends up with document tables that have no RLS.

The server refuses to serve traffic when any of them is missing, naming the gap
(`REQUIRED_MIGRATIONS`, `server/postgres.py`). If boot fails that way, apply the
named files and restart; each migration records itself in `schema_migrations` and
is safe to re-run.

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
  Bright Data verification. Cataloged external sources stay gated by their
  real access mode instead of falling back to fixtures or unsupported scraping.
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

The local release gate is credential-free:

```bash
python -m pytest tests/server -q
node --test tests/server/webui/test_oauth_popup.mjs \
  tests/server/webui/test_admin_provisioning.mjs \
  tests/server/webui/test_product_import.mjs \
  tests/server/webui/test_research_scoring.mjs \
  tests/server/webui/test_research_results.mjs
uv lock --check
uv build
```

CI also installs the wheel and sdist outside the checkout, asserts removed
demo/fixture paths are absent, and builds and boots `Dockerfile.interfaze-api`.
Live Bright Data, OAuth/delivery providers, and hosted Supabase remain external
qualification gates and require their own credentials.
