# Email OAuth Popup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Google Workspace and Microsoft 365 Connect buttons open a secure OAuth popup, automatically refresh the Integrations page after authorization, and prevent credentialless direct connections.

**Architecture:** Keep the existing server-owned authorization-code flow and add two narrow edges around it: a safe callback HTML page that reports a bounded status to its opener, and a dependency-injected browser coordinator that owns popup, message, polling, timeout, and cleanup state. The Integrations page remains a thin consumer. The signed OAuth `state` remains the tenant/CSRF authorization boundary, and provider credentials remain encrypted server-side.

**Tech Stack:** Python 3.11+, FastAPI, httpx, existing SQLite/Postgres-compatible database and credential cipher, pytest, vanilla browser ES modules, Node.js 20+ built-in `node:test`.

## Global Constraints

- Implement only Google and Microsoft email OAuth. Do not change SMTP, browser-webmail, WhatsApp, email-send behavior, demo data, or database schemas.
- Add no Hermes core model tool, dependency, npm package, user-facing non-secret environment variable, or provider-specific client secret in browser code.
- Preserve the existing OAuth start and callback URLs and the legacy direct-connect route paths.
- Verify signed `state` before accepting either provider success or provider denial.
- Never render provider error text, token response bodies, access tokens, refresh tokens, client secrets, or encryption errors in callback HTML.
- Accept popup messages only from the expected origin, expected popup window, matching provider, exact message type, and allowed status.
- Make completion idempotent so a callback message and a polling response cannot refresh or toast twice.
- Open the blank popup synchronously before starting any asynchronous request.
- Bound polling by the server-provided `expires_in` and release every listener/timer on all terminal paths and page disposal.
- Keep the five existing unrelated dirty WebUI/document files untouched.
- Use TDD for each behavior slice and commit only the files named in that task.

---

## Task 1: Secure callback pages and prove tenant-bound encrypted storage

**Files:**

- Modify: `tests/server/test_oauth.py`
- Modify: `server/routes/oauth.py`

### 1.1 Write the failing callback contract tests

- [ ] Add these imports and helpers to `tests/server/test_oauth.py`:

```python
class _TokenResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _configured_client(**overrides):
    return make_client(
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        microsoft_oauth_client_id="microsoft-client",
        microsoft_oauth_client_secret="microsoft-secret",
        public_base_url="https://api.example.test",
        **overrides,
    )


def _callback(client, provider: str, state: str, **query):
    params = {"state": state, **query}
    return client.get(
        f"/api/v1/integrations/email/oauth/{provider}/callback",
        params=params,
    )
```

- [ ] Replace `test_callback_reports_provider_denial_without_erroring` so denial also carries a valid signed state and proves a bounded, non-closing message:

```python
def test_callback_reports_provider_denial_without_rendering_provider_text():
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    provider_error = '<script>window.opener.pwned="provider-secret"</script>'

    res = _callback(client, "google", state, error=provider_error)

    assert res.status_code == 200
    assert "Authorization cancelled" in res.text
    assert '"type": "interfaze:oauth"' in res.text
    assert '"provider": "google"' in res.text
    assert '"status": "cancelled"' in res.text
    assert provider_error not in res.text
    assert "provider-secret" not in res.text
    assert "window.close()" not in res.text
```

- [ ] Add state, escaping, token exchange, and storage behavior tests:

```python
def test_callback_rejects_denial_without_valid_state_as_html_failure():
    _app, client = _configured_client()

    res = client.get(
        "/api/v1/integrations/email/oauth/google/callback",
        params={"error": "access_denied"},
    )

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert '"status": "failed"' in res.text
    assert "Return to Interfaze and start again" in res.text


def test_callback_missing_code_is_an_html_failure_after_state_validation():
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")

    res = _callback(client, "google", state)

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert '"status": "failed"' in res.text


def test_callback_page_escapes_visible_content_and_serializes_message_data():
    res = oauth._page(
        '<script id="title">bad</script>',
        '<img src=x onerror="bad()">',
        provider="google",
        status="failed",
        status_code=400,
    )

    assert res.status_code == 400
    html = res.body.decode()
    assert '<script id="title">' not in html
    assert '<img src=x' not in html
    assert "&lt;script" in html
    assert "&lt;img" in html
    assert '"provider": "google"' in html


def test_successful_callback_stores_only_encrypted_credentials_for_state_tenant(monkeypatch):
    app, client = _configured_client()
    admin_headers, _headers_a, company_a = chat_tenant(client, "Tenant A")
    company_b_res = client.post(
        "/api/v1/admin/companies",
        headers=admin_headers,
        json={"name": "Tenant B"},
    )
    company_b = company_b_res.json()["id"]
    state = oauth.sign_state(SECRET, company_a, "google")
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {
            "refresh_token": "refresh-secret",
            "access_token": "access-secret",
        }),
    )

    res = _callback(client, "google", state, code="authorization-code")

    assert res.status_code == 200
    assert '"status": "connected"' in res.text
    assert "window.close()" in res.text
    assert "refresh-secret" not in res.text
    assert "access-secret" not in res.text
    assert "google-secret" not in res.text
    row = app.state.db.one(
        "SELECT * FROM integrations WHERE company_id=? AND kind='email' AND provider='google'",
        (company_a,),
    )
    assert row is not None
    assert row["status"] == "connected"
    assert "refresh-secret" not in row["encrypted_credentials"]
    assert app.state.cipher.decrypt(row["encrypted_credentials"]) == {
        "refresh_token": "refresh-secret",
        "access_token": "access-secret",
        "client_id": "google-client",
        "client_secret": "google-secret",
    }
    assert app.state.db.one(
        "SELECT id FROM integrations WHERE company_id=? AND provider='google'",
        (company_b,),
    ) is None


def test_callback_sanitizes_provider_http_and_invalid_json_failures(monkeypatch):
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    provider_body = "upstream refresh_token=leaked client_secret=leaked"
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(401, text=provider_body),
    )

    res = _callback(client, "google", state, code="bad-code")

    assert res.status_code == 502
    assert '"status": "failed"' in res.text
    assert provider_body not in res.text
    assert "refresh_token" not in res.text
    assert "client_secret" not in res.text

    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, payload=None),
    )
    res = _callback(client, "google", state, code="bad-json")
    assert res.status_code == 502
    assert "not json" not in res.text


def test_callback_sanitizes_network_and_missing_refresh_token_failures(monkeypatch):
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "microsoft")

    def fail_network(*args, **kwargs):
        raise oauth.httpx.ConnectError("network-secret")

    monkeypatch.setattr(oauth.httpx, "post", fail_network)
    res = _callback(client, "microsoft", state, code="code")
    assert res.status_code == 502
    assert "network-secret" not in res.text

    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {"access_token": "only-access"}),
    )
    res = _callback(client, "microsoft", state, code="code")
    assert res.status_code == 502
    assert "only-access" not in res.text


def test_callback_reports_unconfigured_encryption_as_sanitized_html(monkeypatch):
    app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {
            "refresh_token": "refresh-secret",
            "access_token": "access-secret",
        }),
    )
    app.state.cipher = CredentialCipher("")

    res = _callback(client, "google", state, code="code")

    assert res.status_code == 503
    assert '"status": "failed"' in res.text
    assert "refresh-secret" not in res.text
    assert "access-secret" not in res.text
```

- [ ] Import `CredentialCipher` with `from server.crypto import CredentialCipher`, and update `test_callback_rejects_a_forged_state_before_any_token_exchange` to also assert HTML content type and the `failed` popup status.

### 1.2 Run the focused tests and confirm the red state

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/server/test_oauth.py -q
```

Expected: the new callback tests fail because callback errors are JSON, denial skips state verification, `_page` has no status/message arguments, provider bodies leak, and success has no opener notification.

### 1.3 Implement bounded callback HTML and sanitized error mapping

- [ ] In `server/routes/oauth.py`, import `html` and add an allowed status constant:

```python
import html

CALLBACK_STATUSES = frozenset({"connected", "cancelled", "failed"})
```

- [ ] Replace `_page` with this contract:

```python
def _page(title: str, body: str, *, provider: str, status: str,
          status_code: int = 200, close: bool = False) -> HTMLResponse:
    if provider not in PROVIDERS or status not in CALLBACK_STATUSES:
        raise ValueError("invalid OAuth callback message")
    message = json.dumps({
        "type": "interfaze:oauth",
        "provider": provider,
        "status": status,
    }).replace("</", "<\\/")
    close_script = "window.close();" if close else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:14vh auto;padding:0 1.5rem}}</style>
</head><body><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>
<script>(()=>{{const message={message};if(window.opener&&!window.opener.closed){{window.opener.postMessage(message,window.location.origin);}}{close_script}}})();</script>
</body></html>""",
        status_code=status_code,
    )
```

- [ ] Add small helpers immediately above `oauth_callback` so every failure uses fixed, non-provider copy:

```python
def _failure_page(provider: str, status_code: int, body: str) -> HTMLResponse:
    return _page("Authorization failed", body, provider=provider,
                 status="failed", status_code=status_code)


def _invalid_request_page(provider: str) -> HTMLResponse:
    return _failure_page(
        provider,
        400,
        "This authorization request is invalid or expired. Return to Interfaze and start again.",
    )


def _exchange_failure_page(provider: str) -> HTMLResponse:
    return _failure_page(
        provider,
        502,
        "The provider could not complete the connection. Return to Interfaze and try again.",
    )
```

- [ ] Restructure `oauth_callback` in this exact order:

  1. Reject unsupported providers with the existing 404.
  2. Resolve `_secret(settings)` and call `verify_state(...)` before inspecting `error` or `code`.
  3. Convert any `HTTPException` from secret/state validation to `_failure_page` with its status; use the fixed invalid-request copy for status 400 and `OAuth is not configured on this server.` for status 503.
  4. For `error`, return a 200 `cancelled` page with `Authorization cancelled` and `The provider authorization was cancelled. Return to Interfaze to try again.` Do not include the query value.
  5. For missing `code`, return `_invalid_request_page(provider)`.
  6. Resolve app credentials. Convert configuration `HTTPException` to a fixed 503 failure page.
  7. Wrap `httpx.post` in `except httpx.HTTPError` and return `_exchange_failure_page(provider)`.
  8. Treat HTTP status 400+, invalid JSON, a non-dict JSON body, or missing `refresh_token` as `_exchange_failure_page(provider)`.
  9. Build the four-field credential mapping already used today and call `_store`.
  10. Convert `_store` `HTTPException` to a fixed failure page preserving its 503 status.
  11. Return a `connected` page with `close=True` and the visible text `Your mailbox is connected. Return to Interfaze if this window does not close.`

- [ ] Do not catch an unrecognized programming exception. Tests must still expose genuine regressions rather than converting all exceptions into a success-looking browser response.

### 1.4 Run focused and adjacent backend tests

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/server/test_oauth.py tests/server/test_webui.py -q
```

Expected: all OAuth and WebUI serving tests pass; callback HTML has the normal security headers from `server/app.py`.

### 1.5 Commit the callback slice

- [ ] Stage only `server/routes/oauth.py` and `tests/server/test_oauth.py`, inspect the staged diff, then commit:

```bash
git add server/routes/oauth.py tests/server/test_oauth.py
git diff --cached --check
git diff --cached --stat
git commit -m "fix(interfaze): secure email OAuth callback"
```

---

## Task 2: Make legacy Google/Microsoft direct routes fail safely

**Files:**

- Modify: `tests/server/test_oauth.py`
- Modify: `server/routes/integrations.py`

### 2.1 Write the failing legacy-route regression

- [ ] Add this parametrized test to `tests/server/test_oauth.py` and import `pytest`:

```python
import pytest


@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("payload", [None, {}])
def test_legacy_direct_connect_requires_oauth_and_creates_no_integration(provider, payload):
    app, client = _configured_client()
    _admin, headers, company_id = chat_tenant(client)
    kwargs = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload

    res = client.post(f"/api/v1/integrations/email/connect/{provider}", **kwargs)

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["message"] == f"Connect {provider} mailboxes with OAuth"
    assert detail["oauth_start"] == (
        f"/api/v1/integrations/email/oauth/{provider}/start"
    )
    assert app.state.db.one(
        "SELECT id FROM integrations WHERE company_id=? AND kind='email' AND provider=?",
        (company_id, provider),
    ) is None
```

### 2.2 Confirm the unsafe current behavior

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/server/test_oauth.py -q -k legacy_direct_connect
```

Expected: empty JSON currently creates a credentialless `connected` row and a missing body currently returns 422.

### 2.3 Replace the two direct-connect handlers with OAuth instructions

- [ ] Add this helper in `server/routes/integrations.py`:

```python
def _oauth_connect_required(provider: str) -> None:
    raise HTTPException(409, {
        "message": f"Connect {provider} mailboxes with OAuth",
        "oauth_start": f"/api/v1/integrations/email/oauth/{provider}/start",
    })
```

- [ ] Change both handlers so their request body is optional and ignored, while authentication and tenant dependency resolution remain unchanged:

```python
@router.post("/integrations/email/connect/google", status_code=201)
def connect_google(request: Request, body: IntegrationConnect | None = None,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    _oauth_connect_required("google")


@router.post("/integrations/email/connect/microsoft", status_code=201)
def connect_microsoft(request: Request, body: IntegrationConnect | None = None,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    _oauth_connect_required("microsoft")
```

- [ ] Keep `request`, `principal`, and `x_company_id` in the signatures so the routes remain protected and OpenAPI continues to describe the same authenticated tenant surface. Do not change `_connect`; SMTP, browser, WhatsApp, and fixture integrations still use it.

### 2.4 Verify and commit

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/server/test_oauth.py tests/server/test_api_mvp.py tests/server/test_webui.py -q
```

- [ ] Stage only the two files and commit:

```bash
git add server/routes/integrations.py tests/server/test_oauth.py
git diff --cached --check
git commit -m "fix(interfaze): require OAuth for hosted mailboxes"
```

---

## Task 3: Build the popup coordinator with deterministic browser-state tests

**Files:**

- Create: `tests/server/webui/test_oauth_popup.mjs`
- Create: `server/webui/js/oauth-popup.js`
- Modify: `tests/server/test_webui.py`

### 3.1 Write the failing packaged-asset regression

- [ ] Extend the asset tuple in `test_static_assets_resolve_from_relative_hrefs` in `tests/server/test_webui.py` to include `"/js/oauth-popup.js"`.

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/server/test_webui.py -q -k static_assets
```

Expected: 404 because the production popup module does not exist yet.

### 3.2 Create a test harness that imports the production ES module

- [ ] Create `tests/server/webui/test_oauth_popup.mjs`. Load the real browser file as a data-URL ES module so the root package does not need a new `type: module` setting:

```javascript
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../../../server/webui/js/oauth-popup.js', import.meta.url),
  'utf8',
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { startEmailOAuth } = await import(moduleUrl);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function fakeTimers() {
  let nextId = 1;
  const intervals = new Map();
  const timeouts = new Map();
  return {
    api: {
      setInterval(fn) { const id = nextId++; intervals.set(id, fn); return id; },
      clearInterval(id) { intervals.delete(id); },
      setTimeout(fn) { const id = nextId++; timeouts.set(id, fn); return id; },
      clearTimeout(id) { timeouts.delete(id); },
    },
    async tickIntervals() {
      for (const fn of [...intervals.values()]) await fn();
      await Promise.resolve();
    },
    async fireTimeouts() {
      for (const [id, fn] of [...timeouts]) { timeouts.delete(id); fn(); }
      await Promise.resolve();
    },
    counts() { return { intervals: intervals.size, timeouts: timeouts.size }; },
  };
}

function harness(overrides = {}) {
  const listeners = new Map();
  const popup = {
    closed: false,
    closeCalls: 0,
    location: { urls: [], replace(url) { this.urls.push(url); } },
    close() { this.closed = true; this.closeCalls += 1; },
  };
  const windowRef = {
    location: { origin: 'https://app.example.test' },
    openCalls: [],
    open(...args) { this.openCalls.push(args); return popup; },
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) {
      if (listeners.get(type) === fn) listeners.delete(type);
    },
  };
  const timers = fakeTimers();
  const statuses = [];
  let connected = 0;
  let listResult = { items: [] };
  const startResult = {
    authorize_url: 'https://provider.example.test/authorize',
    expires_in: 600,
  };
  const options = {
    provider: 'google',
    windowRef,
    timerApi: timers.api,
    pollMs: 1000,
    startOAuth: async () => startResult,
    listIntegrations: async () => listResult,
    onConnected: () => { connected += 1; },
    onStatus: status => statuses.push(status),
    ...overrides,
  };
  return {
    options, popup, windowRef, timers, statuses, listeners,
    setListResult(value) { listResult = value; },
    connectedCount() { return connected; },
    message(event) { listeners.get('message')?.(event); },
  };
}
```

### 3.3 Add behavior tests before production code

- [ ] Add these named tests using the harness. Each assertion is a behavior contract, not an implementation snapshot:

```javascript
test('blocked popup reports blocked and never starts OAuth', async () => {
  let starts = 0;
  const h = harness({
    startOAuth: async () => { starts += 1; },
  });
  h.windowRef.open = () => null;
  const attempt = startEmailOAuth(h.options);
  await Promise.resolve();
  assert.equal(attempt, null);
  assert.equal(starts, 0);
  assert.deepEqual(h.statuses, [{ status: 'blocked' }]);
});

test('opens synchronously then navigates after the start request', async () => {
  const started = deferred();
  const h = harness({ startOAuth: () => started.promise });
  const attempt = startEmailOAuth(h.options);
  assert.equal(h.windowRef.openCalls.length, 1);
  assert.deepEqual(h.popup.location.urls, []);
  started.resolve({ authorize_url: 'https://provider.test/auth', expires_in: 30 });
  assert.equal(await attempt.ready, true);
  assert.deepEqual(h.popup.location.urls, ['https://provider.test/auth']);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
});

test('closing the popup while start is pending reports cancellation', async () => {
  const started = deferred();
  const h = harness({ startOAuth: () => started.promise });
  const attempt = startEmailOAuth(h.options);
  h.popup.closed = true;
  started.resolve({ authorize_url: 'https://provider.test/auth', expires_in: 30 });
  assert.equal(await attempt.ready, false);
  assert.deepEqual(h.statuses, [{ status: 'cancelled' }]);
  assert.deepEqual(h.popup.location.urls, []);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('start failure closes the blank popup and releases resources', async () => {
  const error = new Error('OAuth is not configured');
  const h = harness({ startOAuth: async () => { throw error; } });
  const attempt = startEmailOAuth(h.options);
  assert.equal(await attempt.ready, false);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.statuses, [{ status: 'start_failed', error }]);
  assert.equal(h.listeners.has('message'), false);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('accepts only the expected origin window provider type and status', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  const valid = { type: 'interfaze:oauth', provider: 'google', status: 'connected' };
  h.message({ origin: 'https://evil.test', source: h.popup, data: valid });
  h.message({ origin: h.windowRef.location.origin, source: {}, data: valid });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, provider: 'microsoft' } });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, type: 'other' } });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, status: 'unknown' } });
  assert.equal(h.connectedCount(), 0);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
  h.message({ origin: h.windowRef.location.origin, source: h.popup, data: valid });
  assert.equal(h.connectedCount(), 1);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('polling fallback completes once and ignores a later message race', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  h.setListResult({ items: [{ provider: 'google', status: 'connected' }] });
  await h.timers.tickIntervals();
  h.message({
    origin: h.windowRef.location.origin,
    source: h.popup,
    data: { type: 'interfaze:oauth', provider: 'google', status: 'connected' },
  });
  assert.equal(h.connectedCount(), 1);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('transient polling failures do not abort the bounded attempt', async () => {
  const h = harness({ listIntegrations: async () => { throw new Error('temporary'); } });
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  await h.timers.tickIntervals();
  assert.deepEqual(h.statuses, []);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
});

test('manual popup close reports cancellation and cleans up', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  h.popup.closed = true;
  await h.timers.tickIntervals();
  assert.deepEqual(h.statuses, [{ status: 'cancelled' }]);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('callback cancellation and failure notify once without closing the popup', async () => {
  for (const status of ['cancelled', 'failed']) {
    const h = harness();
    const attempt = startEmailOAuth(h.options);
    await attempt.ready;
    h.message({
      origin: h.windowRef.location.origin,
      source: h.popup,
      data: { type: 'interfaze:oauth', provider: 'google', status },
    });
    assert.deepEqual(h.statuses, [{ status }]);
    assert.equal(h.popup.closeCalls, 0);
    assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
  }
});

test('expiry reports expired and explicit disposal is silent', async () => {
  const expired = harness();
  const expiringAttempt = startEmailOAuth(expired.options);
  await expiringAttempt.ready;
  await expired.timers.fireTimeouts();
  assert.deepEqual(expired.statuses, [{ status: 'expired' }]);
  assert.deepEqual(expired.timers.counts(), { intervals: 0, timeouts: 0 });

  const disposed = harness();
  const disposedAttempt = startEmailOAuth(disposed.options);
  await disposedAttempt.ready;
  disposedAttempt.cancel();
  assert.deepEqual(disposed.statuses, []);
  assert.deepEqual(disposed.timers.counts(), { intervals: 0, timeouts: 0 });
  assert.equal(disposed.listeners.has('message'), false);
});
```

### 3.4 Confirm the coordinator test is red because the production module is absent

- [ ] Run:

```bash
node --test tests/server/webui/test_oauth_popup.mjs
```

Expected: `ENOENT` for `server/webui/js/oauth-popup.js`.

### 3.5 Implement the coordinator as one explicit state machine

- [ ] Create `server/webui/js/oauth-popup.js` with one export:

```javascript
const MESSAGE_TYPE = 'interfaze:oauth';
const MESSAGE_STATUSES = new Set(['connected', 'cancelled', 'failed']);

const defaultTimerApi = {
  setInterval: (...args) => globalThis.setInterval(...args),
  clearInterval: id => globalThis.clearInterval(id),
  setTimeout: (...args) => globalThis.setTimeout(...args),
  clearTimeout: id => globalThis.clearTimeout(id),
};

export function startEmailOAuth({
  provider,
  startOAuth,
  listIntegrations,
  onConnected,
  onStatus,
  windowRef = window,
  timerApi = defaultTimerApi,
  pollMs = 1000,
}) {
  const popup = windowRef.open(
    '',
    `interfaze-oauth-${provider}`,
    'popup=yes,width=620,height=760',
  );
  if (!popup) {
    onStatus({ status: 'blocked' });
    return null;
  }

  let settled = false;
  let polling = false;
  let pollTimer = null;
  let expiryTimer = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function cleanup() {
    windowRef.removeEventListener('message', receiveMessage);
    if (pollTimer !== null) timerApi.clearInterval(pollTimer);
    if (expiryTimer !== null) timerApi.clearTimeout(expiryTimer);
    pollTimer = null;
    expiryTimer = null;
  }

  function stopSilently() {
    if (settled) return;
    settled = true;
    cleanup();
  }

  function finish(status, error = undefined, { closePopup = false } = {}) {
    if (settled) return;
    settled = true;
    cleanup();
    if (closePopup && !popup.closed) popup.close();
    if (status === 'connected') onConnected();
    else onStatus(error === undefined ? { status } : { status, error });
  }

  function receiveMessage(event) {
    const data = event.data;
    if (event.origin !== windowRef.location.origin
        || event.source !== popup
        || !data
        || data.type !== MESSAGE_TYPE
        || data.provider !== provider
        || !MESSAGE_STATUSES.has(data.status)) return;
    finish(data.status, undefined, { closePopup: data.status === 'connected' });
  }

  async function poll() {
    if (settled || polling) return;
    if (popup.closed) {
      finish('cancelled');
      return;
    }
    polling = true;
    try {
      const result = await listIntegrations();
      const connected = result.items?.some(
        item => item.provider === provider && item.status === 'connected',
      );
      if (connected) finish('connected', undefined, { closePopup: true });
    } catch {
      // A transient list failure must not abort an authorization in progress.
    } finally {
      polling = false;
    }
  }

  windowRef.addEventListener('message', receiveMessage);
  Promise.resolve().then(async () => {
    try {
      const result = await startOAuth(provider);
      if (settled) {
        resolveReady(false);
        return;
      }
      if (popup.closed) {
        finish('cancelled');
        resolveReady(false);
        return;
      }
      popup.location.replace(result.authorize_url);
      pollTimer = timerApi.setInterval(poll, pollMs);
      expiryTimer = timerApi.setTimeout(
        () => finish('expired'),
        Number(result.expires_in) * 1000,
      );
      resolveReady(true);
    } catch (error) {
      finish('start_failed', error, { closePopup: true });
      resolveReady(false);
    }
  });

  return {
    popup,
    ready,
    cancel({ notify = false } = {}) {
      if (notify) finish('cancelled');
      else stopSilently();
    },
  };
}
```

- [ ] Add this validation after `await startOAuth(provider)` and before `popup.location.replace(...)`:

```javascript
const expiresIn = Number(result?.expires_in);
if (typeof result?.authorize_url !== 'string'
    || result.authorize_url.length === 0
    || !Number.isFinite(expiresIn)
    || expiresIn <= 0) {
  throw new Error('OAuth start returned an invalid response');
}
```

- [ ] Use `expiresIn * 1000` when scheduling the timeout, then add this test:

```javascript
test('invalid start responses fail closed and clean up', async () => {
  const invalidResults = [
    { authorize_url: '', expires_in: 600 },
    { authorize_url: 'https://provider.test/auth', expires_in: 0 },
  ];
  for (const result of invalidResults) {
    const h = harness({ startOAuth: async () => result });
    const attempt = startEmailOAuth(h.options);
    assert.equal(await attempt.ready, false);
    assert.equal(h.popup.closeCalls, 1);
    assert.equal(h.statuses.length, 1);
    assert.equal(h.statuses[0].status, 'start_failed');
    assert.equal(h.statuses[0].error.message, 'OAuth start returned an invalid response');
    assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
    assert.equal(h.listeners.has('message'), false);
  }
});
```

### 3.6 Run the coordinator suite and syntax checks

- [ ] Run:

```bash
node --test tests/server/webui/test_oauth_popup.mjs
node --input-type=module --check < server/webui/js/oauth-popup.js
.venv/bin/python -m pytest tests/server/test_webui.py -q -k static_assets
```

Expected: all coordinator tests pass with no network, DOM, or real timers.

### 3.7 Commit the coordinator slice

- [ ] Stage only the new module and its test, then commit:

```bash
git add server/webui/js/oauth-popup.js tests/server/webui/test_oauth_popup.mjs tests/server/test_webui.py
git diff --cached --check
git commit -m "feat(interfaze): add email OAuth popup coordinator"
```

---

## Task 4: Wire Google and Microsoft Connect buttons to OAuth and automatic refresh

**Files:**

- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/pages/integrations.js`

### 4.1 Declare the start route in the central API catalog

- [ ] Add this route directly after `emailIntegrations.list` in `server/webui/js/api.js`:

```javascript
'emailIntegrations.startOAuth':    ['POST',   '/integrations/email/oauth/:provider/start'],
```

- [ ] Retain `connectGoogle` and `connectMicrosoft` declarations for old clients. The packaged page must stop calling them.

### 4.2 Make the Integrations page own active attempts

- [ ] Import the coordinator in `server/webui/js/pages/integrations.js`:

```javascript
import { startEmailOAuth } from '../oauth-popup.js';
```

- [ ] Change the two provider descriptors to mark OAuth behavior instead of naming the legacy route:

```javascript
{ key: 'google', title: 'Google Workspace', oauth: true, logo: 'G' },
{ key: 'microsoft', title: 'Microsoft 365', oauth: true, logo: 'M' },
```

- [ ] At the top of `mount`, after `disposed`, add:

```javascript
const oauthAttempts = new Map();
```

- [ ] Inside `mount`, before `render`, add this owner function:

```javascript
function connectOauth(provider) {
  oauthAttempts.get(provider.key)?.cancel();
  const attempt = startEmailOAuth({
    provider: provider.key,
    startOAuth: key => call('emailIntegrations.startOAuth', {
      params: { provider: key },
    }),
    listIntegrations: () => call('emailIntegrations.list'),
    onConnected: () => {
      oauthAttempts.delete(provider.key);
      toast(`${provider.title} connected`, 'success');
      render().catch(err => toast(err.message || 'Could not refresh integrations', 'error'));
    },
    onStatus: ({ status, error }) => {
      oauthAttempts.delete(provider.key);
      const messages = {
        blocked: ['Allow popups for this site, then try Connect again.', 'warning'],
        start_failed: [error?.message || `${provider.title} OAuth could not start`, 'error'],
        cancelled: [`${provider.title} authorization was cancelled`, 'warning'],
        failed: [`${provider.title} authorization failed. Read the popup, then try again.`, 'error'],
        expired: [`${provider.title} authorization expired. Start again.`, 'warning'],
      };
      const [message, kind] = messages[status] || [`${provider.title} authorization stopped`, 'warning'];
      toast(message, kind);
    },
  });
  if (attempt) oauthAttempts.set(provider.key, attempt);
}
```

- [ ] Pass `connectOauth` through the render helpers:

```javascript
emailSection(emailRes.items, render, connectOauth)

function emailSection(items, onChange, onOauth) {
  // existing body
  // Connect onClick becomes:
  onClick: () => connectProvider(provider, onChange, onOauth)
}

function connectProvider(provider, onChange, onOauth) {
  if (provider.oauth) return onOauth(provider);
  if (provider.credential === 'smtp') return connectSmtp(provider, onChange);
  if (provider.credential === 'browser') return connectBrowserWebmail(provider, onChange);
}
```

- [ ] Remove the old generic `call(provider.route).then(...)` tail. Every remaining non-stub provider is handled by one of `oauth`, `smtp`, or `browser`; the connected local stub has no Connect button.

- [ ] Expand the mount disposer so attempts release message listeners and timers without displaying navigation-time toasts:

```javascript
return () => {
  disposed = true;
  unsub();
  for (const attempt of oauthAttempts.values()) attempt.cancel();
  oauthAttempts.clear();
};
```

- [ ] Keep the map assignment guarded by `if (attempt)` so a blocked popup is never retained. `startEmailOAuth` defers API startup to a microtask, so start-failure callbacks run after the returned attempt has been stored and can delete it.

### 4.3 Run browser-module and server packaging checks

- [ ] Run:

```bash
node --test tests/server/webui/test_oauth_popup.mjs
node --input-type=module --check < server/webui/js/oauth-popup.js
node --input-type=module --check < server/webui/js/pages/integrations.js
node --input-type=module --check < server/webui/js/api.js
.venv/bin/python -m pytest tests/server/test_webui.py tests/server/test_oauth.py -q
```

Expected: no syntax errors; the coordinator suite remains green; the packaged server serves the module.

### 4.4 Commit the WebUI wiring slice

- [ ] Stage only these files and commit:

```bash
git add server/webui/js/api.js server/webui/js/pages/integrations.js
git diff --cached --check
git commit -m "feat(interfaze): connect email OAuth from integrations"
```

---

## Task 5: Add CI coverage, operator configuration, and demo qualification

**Files:**

- Modify: `.github/workflows/interfaze-api.yml`
- Modify: `server/README.md`

### 5.1 Add the dependency-free browser unit suite to CI

- [ ] Add this step immediately after `Run server tests` in `.github/workflows/interfaze-api.yml`:

```yaml
      - name: Run WebUI OAuth popup tests
        run: |
          set -euo pipefail
          node --test tests/server/webui/test_oauth_popup.mjs
          node --input-type=module --check < server/webui/js/oauth-popup.js
          node --input-type=module --check < server/webui/js/pages/integrations.js
          node --input-type=module --check < server/webui/js/api.js
```

- [ ] Do not add setup-node or npm install. `ubuntu-latest` supplies Node 20+, and this suite uses only Node built-ins.

### 5.2 Document exact OAuth configuration and callbacks

- [ ] Add an `## Connect Gmail and Microsoft 365` section in `server/README.md` after local startup. Include this exact configuration block:

```bash
export INTERFAZE_PUBLIC_BASE_URL='https://interfaze.example.com'
export INTERFAZE_CREDENTIAL_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

export GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:?load the provider-issued id from the deployment secret manager}"
export GOOGLE_OAUTH_CLIENT_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET:?load the provider-issued secret from the deployment secret manager}"

export MICROSOFT_OAUTH_CLIENT_ID="${MICROSOFT_OAUTH_CLIENT_ID:?load the provider-issued id from the deployment secret manager}"
export MICROSOFT_OAUTH_CLIENT_SECRET="${MICROSOFT_OAUTH_CLIENT_SECRET:?load the provider-issued secret from the deployment secret manager}"
export MICROSOFT_OAUTH_TENANT='common'
```

- [ ] State that `INTERFAZE_PUBLIC_BASE_URL` must be the public HTTPS origin serving both the packaged WebUI and API. Register these exact redirect URIs with the provider:

```text
https://interfaze.example.com/api/v1/integrations/email/oauth/google/callback
https://interfaze.example.com/api/v1/integrations/email/oauth/microsoft/callback
```

- [ ] Explain that Google requires the Gmail modify scope and offline consent; Microsoft requires `offline_access Mail.ReadWrite Mail.Send User.Read`. Provider secrets are deployment secrets, never browser configuration. `MICROSOFT_OAUTH_TENANT` defaults to `common`.

- [ ] Add a demo checklist:

  1. Start the API on the same public origin registered with the provider.
  2. Sign in and open Integrations.
  3. Click Google Workspace or Microsoft 365 Connect and allow the popup.
  4. Complete consent and verify the popup closes.
  5. Verify the card changes to `connected` without reloading the browser page.
  6. Click Test to verify provider scopes.

- [ ] Keep live-provider consent outside automated CI; the automated suite must never call Google or Microsoft.

### 5.3 Run the complete automated qualification

- [ ] Run the focused frontend checks:

```bash
node --test tests/server/webui/test_oauth_popup.mjs
node --input-type=module --check < server/webui/js/oauth-popup.js
node --input-type=module --check < server/webui/js/pages/integrations.js
node --input-type=module --check < server/webui/js/api.js
```

- [ ] Run the full server suite through the project wrapper so it selects the configured environment:

```bash
scripts/run_tests.sh tests/server/
```

- [ ] Run formatting/patch hygiene checks:

```bash
git diff --check
git status --short
```

- [ ] Confirm the only uncommitted paths are the five pre-existing user-owned changes. If any OAuth implementation path is uncommitted, inspect and commit it before browser qualification.

### 5.4 Run a local packaged-browser smoke test

- [ ] Start a temporary local API with OAuth credentials intentionally unset and a temporary database. Generate only the credential key; do not print it:

```bash
interfaze_tmp="$(mktemp -d)"
INTERFAZE_DATABASE_PATH="$interfaze_tmp/interfaze.db" \
INTERFAZE_UPLOAD_DIR="$interfaze_tmp/uploads" \
INTERFAZE_BOOTSTRAP_ADMIN_EMAIL="demo@example.test" \
INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD="demo-password-123" \
INTERFAZE_CREDENTIAL_KEY="$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
INTERFAZE_PUBLIC_BASE_URL="http://127.0.0.1:8765" \
.venv/bin/interfaze-api --host 127.0.0.1 --port 8765
```

- [ ] Using the `browser:control-in-app-browser` skill, open `http://127.0.0.1:8765`, sign in, create/select a company if required, and open Integrations.

- [ ] Click Google Workspace Connect and verify all four points:

  - a blank popup is created synchronously;
  - the start request fails visibly with the Google OAuth configuration message;
  - the blank popup closes;
  - no Google integration row appears after refreshing the Integrations page.

- [ ] Repeat for Microsoft 365. Then stop the local process. The temporary directory may be removed only after resolving its exact path and confirming the process has stopped.

- [ ] If sandbox OAuth apps are available, run the separate release qualification over HTTPS and verify a real callback closes the popup and auto-refreshes the card. Absence of external test accounts does not block the automated/local MVP result; record it as external release qualification still required.

### 5.5 Commit CI and runbook changes

- [ ] Stage only workflow and README, inspect them, and commit:

```bash
git add .github/workflows/interfaze-api.yml server/README.md
git diff --cached --check
git commit -m "docs(interfaze): qualify email OAuth popup flow"
```

---

## Final Acceptance Checklist

- [ ] Google and Microsoft Connect open a popup before any asynchronous work.
- [ ] A blocked popup never calls the start endpoint and produces actionable UI copy.
- [ ] The callback validates signed tenant state for success and denial.
- [ ] Callback pages report only `connected`, `cancelled`, or `failed` to the expected same-origin opener.
- [ ] Invalid state, provider failure, network failure, bad JSON, missing refresh token, and encryption failure expose no secrets.
- [ ] Successful credentials are encrypted and stored only for the state-bound company.
- [ ] Direct Google/Microsoft connect routes always return 409 and create no row.
- [ ] The opener rejects wrong origin, wrong popup, wrong provider, wrong type, and unknown status.
- [ ] Callback message and polling races complete exactly once.
- [ ] Manual close, expiry, start failure, disposal, success, cancellation, and failure release all listeners and timers.
- [ ] Success refreshes the Integrations card and shows exactly one success toast without a browser-page reload.
- [ ] The focused Node suite and complete `tests/server` suite pass.
- [ ] Local browser smoke proves popup-block/start-failure behavior; sandbox provider consent is recorded separately if credentials are unavailable.
- [ ] No unrelated dirty file was staged, edited, or overwritten.
