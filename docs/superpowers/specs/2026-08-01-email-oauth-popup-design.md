# Email OAuth Popup Design

## Goal

Connect Google Workspace and Microsoft 365 mailboxes from the packaged
Interfaze WebUI through the existing server-owned OAuth authorization-code
flow. Authorization opens in a popup, successful completion refreshes the
Integrations page automatically, and a polling fallback recovers when the
browser drops the callback message.

## Scope

This change covers only Google and Microsoft email OAuth. It does not change
database schemas, email delivery semantics, SMTP or browser-webmail providers,
WhatsApp, demo data, or the wider WebUI design.

## Architecture

### WebUI coordinator

A focused `server/webui/js/oauth-popup.js` module owns one OAuth attempt. The
Integrations page supplies the provider name, provider label, API functions,
and completion callbacks. The coordinator:

1. Opens a blank popup synchronously from the Connect click so popup blockers
   do not reject the provider window.
2. Calls `POST /api/v1/integrations/email/oauth/{provider}/start`.
3. Navigates the popup to the returned `authorize_url`.
4. Listens for a same-origin `interfaze:oauth` completion message.
5. Polls the email-integration list once per second as a fallback.
6. Stops listeners and timers on success, failure, cancellation, popup close,
   expiry, page disposal, or replacement by a newer attempt.

The attempt expires at the `expires_in` value returned by the start endpoint.
Only one active attempt per provider is retained by the mounted Integrations
page. Its cleanup function is called when the page is disposed.

### Callback page

The existing unauthenticated provider callback remains protected by the signed,
tenant-scoped, expiring `state` value. Its HTML response sends this message to
the opener with `window.location.origin` as the target origin:

```json
{
  "type": "interfaze:oauth",
  "provider": "google",
  "status": "connected"
}
```

Google and Microsoft are the only accepted provider values. Status is one of
`connected`, `cancelled`, or `failed`.

On success, the page posts the message and closes itself. Cancellation and
failure pages post their status but remain visible so the user can read the
explanation and close the popup. All title and body content is HTML-escaped,
and message data is serialized as JSON rather than interpolated as executable
source.

### Integration refresh

The opener validates all of the following before accepting a callback:

- `event.origin === window.location.origin`
- `event.source` is the popup opened for this attempt
- message type is `interfaze:oauth`
- message provider matches the attempted provider
- status is an allowed value

For `connected`, the Integrations page reloads the email-integration list,
renders the connected mailbox, and shows one success toast. The polling path
uses the same completion function, so message and polling races cannot refresh
or toast twice.

## Error Handling

- **Popup blocked:** do not call the OAuth start endpoint; show an instruction
  to allow popups for the site.
- **OAuth not configured or start request fails:** close the blank popup and
  display the API error on the Integrations page.
- **Provider denial:** stop the attempt, show a cancellation toast, and leave
  the callback explanation visible in the popup.
- **Invalid or expired state:** preserve the appropriate 400 response, render a
  sanitized failure page, and notify the opener when one exists.
- **Token exchange or encrypted storage failure:** preserve the appropriate
  502 or 503 response, render a generic failure page, and never expose provider
  response bodies, access tokens, refresh tokens, or client secrets.
- **Foreign-origin, wrong-window, or wrong-provider message:** ignore it and
  continue the bounded fallback poll.
- **Popup manually closed before completion:** stop all resources and show a
  cancellation toast.
- **Callback message missed:** polling detects the connected provider and runs
  normal completion.
- **Attempt expiry:** stop all resources and show an expired-attempt message.

The legacy direct-connect endpoints for Google and Microsoft continue to exist
for API-contract compatibility but reject requests with an actionable response
that points clients to the OAuth start route. They must never create a
credentialless `connected` integration.

## File Boundaries

- `server/webui/js/oauth-popup.js`: popup lifecycle and completion coordinator.
- `server/webui/js/pages/integrations.js`: provider button integration, mounted
  attempt ownership, refresh, and toast behavior.
- `server/webui/js/api.js`: OAuth start route declaration.
- `server/routes/oauth.py`: safe callback HTML, opener notification, and
  sanitized failure responses.
- `server/routes/integrations.py`: rejection of direct credentialless Google
  and Microsoft connections.
- `tests/server/test_oauth.py`: OAuth endpoint, callback, escaping, storage, and
  legacy-route regressions.
- `tests/server/webui/test_oauth_popup.mjs`: popup coordinator unit tests using
  Node's built-in test runner and dependency fakes.
- `.github/workflows/interfaze-api.yml`: focused Node test command.
- `server/README.md`: OAuth application settings and exact callback URLs.

## Testing

Backend tests use FastAPI's test client, a fake token response, and the real
credential cipher/database boundary. They prove that a valid callback stores
encrypted credentials only for the state-bound tenant, returns a notifying
success page, and does not expose secrets. Separate cases cover provider denial,
missing or invalid state, escaped error content, sanitized token failures, and
legacy direct-connect rejection.

The coordinator test uses Node's built-in test runner with fake windows,
timers, and API functions. It covers popup blocking, start failure, secure
message validation, message success, polling fallback, message/poll races,
manual popup close, expiry, cancellation/failure notification, and explicit
cleanup on page disposal.

After focused tests pass, the full `tests/server` suite and JavaScript syntax
checks run. A local browser smoke test verifies that clicking Connect opens a
popup and that an unconfigured server fails visibly without creating an
integration. Live Gmail and Microsoft consent remain separate sandbox release
qualification because they require external OAuth applications and accounts.

## Documentation and Configuration

The runbook documents these server credentials:

- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `MICROSOFT_OAUTH_CLIENT_ID`
- `MICROSOFT_OAUTH_CLIENT_SECRET`
- optional `MICROSOFT_OAUTH_TENANT`, defaulting to `common`
- `INTERFAZE_CREDENTIAL_KEY`
- a public HTTPS `INTERFAZE_PUBLIC_BASE_URL`

The registered callbacks are:

- `{INTERFAZE_PUBLIC_BASE_URL}/api/v1/integrations/email/oauth/google/callback`
- `{INTERFAZE_PUBLIC_BASE_URL}/api/v1/integrations/email/oauth/microsoft/callback`

Provider secrets remain server-side. The browser receives only the authorize
URL and non-secret completion status.
