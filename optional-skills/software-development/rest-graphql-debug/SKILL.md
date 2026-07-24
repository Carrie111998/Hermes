---
name: rest-graphql-debug
description: "Debug REST/GraphQL APIs: status codes, auth, schemas, repro."
version: 1.2.0
author: eren-karakus0
license: MIT
metadata:
  hermes:
    tags: [api, rest, graphql, http, debugging, testing, curl, integration]
    category: software-development
    related_skills: [systematic-debugging, test-driven-development]
---

# API Testing & Debugging

Drive REST and GraphQL diagnosis through Hermes tools — `terminal` for `curl`, `execute_code` for Python `requests`, `web_extract` for vendor docs. Isolate the failing layer before guessing at the fix.

## When to Use

- API returns unexpected status or body
- Auth fails (401/403 after token refresh, OAuth, API key)
- Works in Postman but fails in code
- Webhook / callback integration debugging
- Building or reviewing API integration tests
- Rate limiting or pagination issues

Skip for UI rendering, DB query tuning, or DNS/firewall infra (escalate).

## Core Principle

**Isolate the layer, then fix.** A 200 OK can hide broken data. A 500 can mask a one-character auth typo. Walk the chain in order; never skip a step.

```
1. Connectivity   → can we reach the host at all?
1.5 Timeouts      → connect-slow vs read-slow?
2. TLS/SSL        → cert valid and trusted?
3. Auth           → credentials correct and unexpired?
4. Request format → payload shape match server expectations?
5. Response parse → does our code accept what came back?
6. Semantics      → does the data mean what we assume?
```

## Routing — load the reference you need

| Intent | Read |
|---|---|
| First look / copy-paste curl + GraphQL starters | `references/quickstart.md` |
| Walk the 6-layer isolation chain (connectivity → TLS → auth → format → parse → semantics) | `references/layered-debug-flow.md` |
| Got a specific status code (401/403/404/409/422/429/5xx), pagination, or idempotency | `references/status-playbook.md` |
| Schema drift, correlation IDs, vendor bug report, pytest smoke suite, finding report format | `references/validation-and-tests.md` |
| Safe logging helper and the pre-review leak checklist | `references/security.md` |
| Hermes tool recipes: `terminal`, `execute_code`, `web_extract`, `delegate_task` | `references/hermes-tool-patterns.md` |

## Red Lines

- **Never log full tokens.** Redact: `Bearer <REDACTED>`.
- **Never hardcode tokens in scripts.** Read from env (`os.environ["API_TOKEN"]`) or `${HERMES_HOME:-~/.hermes}/.env`.
- **Rotate immediately** if a token surfaces in logs, error messages, or git history.
- **GraphQL returns HTTP 200 on failure.** Always inspect the `errors` field regardless of status code.
- **`requests` has no default timeout** and will hang forever — always pass `timeout=(connect, read)`.
- **`-k` / TLS verification off is ad-hoc debug only**, never in code.
- **Never skip a layer** in the isolation chain to jump to a guessed fix.

## Minimal End-to-End Skeleton

One request, fully observable — status, headers, body — before any deeper layer work:

```python
execute_code('''
import requests
resp = requests.get(
    "https://api.example.com/users/1",
    headers={"Authorization": "Bearer <TOKEN>"},
    timeout=(3.05, 30),  # (connect, read)
)
print(resp.status_code, dict(resp.headers))
print(resp.text[:500])
''')
```

From here: status wrong → `references/status-playbook.md`; unreachable/slow/auth → `references/layered-debug-flow.md`; parsed but suspicious → same file, Step 6.

## Related

- `systematic-debugging` — once the failing API layer is isolated, root-cause your code
- `test-driven-development` — write the regression test before shipping the fix
