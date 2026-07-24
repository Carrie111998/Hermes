# HTTP Status Playbook

## 401 Unauthorized — credentials missing or invalid

1. `Authorization` header actually present? (`curl -v` to confirm)
2. Token correct and unexpired?
3. Right auth scheme? (`Bearer` vs `Basic` vs `Token`)
4. Some APIs use query param (`?api_key=…`) instead of header.

## 403 Forbidden — authenticated but not authorized

1. Token has the required scopes/permissions?
2. Resource owned by a different account?
3. IP allowlist blocking you?
4. CORS in browser? (check `Access-Control-Allow-Origin`)

## 404 Not Found — resource doesn't exist or URL is wrong

1. Path correct? (trailing slash, typo, version prefix)
2. Resource ID exists?
3. Right API version (`/v1/` vs `/v2/`)?
4. Right base URL (staging vs prod)?

## 409 Conflict — state collision

1. Resource already exists (duplicate create)?
2. Stale `ETag` / `If-Match`?
3. Concurrent modification by another process?

## 422 Unprocessable Entity — valid JSON, invalid data

The error body usually names the bad fields. Check:
- Field types (string vs int, date format)
- Required vs optional
- Enum values inside the allowed set

## 429 Too Many Requests — rate limited

Check `Retry-After` and `X-RateLimit-*` headers. Exponential backoff:

```python
execute_code('''
import time, requests

def with_backoff(method, url, **kwargs):
    for attempt in range(5):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = int(resp.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait)
    return resp
''')
```

## 5xx — server-side, usually not your fault

- **500** — server bug. Capture correlation ID, file with provider.
- **502** — upstream down. Backoff + retry.
- **503** — overloaded / maintenance. Check status page.
- **504** — upstream timeout. Reduce payload or raise timeout.

For all 5xx: backoff with jitter, alert on persistence.

## Pagination & Idempotency

**Pagination.** Verify you're getting *all* results. Look for `next_cursor`, `next_page`, `total_count`. Two patterns:
- Offset (`?limit=100&offset=200`) — simple, can skip items if data shifts.
- Cursor (`?cursor=abc123`) — preferred for live or large datasets.

**Idempotency.** For non-idempotent operations (POST), send `Idempotency-Key: <uuid>` so retries don't double-charge / double-create. Mandatory for payments and orders.
