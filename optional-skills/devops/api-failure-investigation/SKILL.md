---
name: api-failure-investigation
description: |-
  Systematic API connectivity troubleshooting — a 5-phase methodology for diagnosing
  API failures when an AI agent or developer encounters connection errors, timeouts,
  or unexpected responses. Covers environment checks, test matrix, minimal GET discovery,
  targeted POST verification, and pattern analysis with clear stop/retry rules.
version: 1.0.0
author: ligl0325
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [api, debugging, connectivity, troubleshooting, rest, grpc, network]
    category: devops
    requires_toolsets: [terminal, web]
---

# 🔌 API Failure Investigation

> **One-liner**: When an API call fails and you — or your AI agent — don't know why,
> follow this 5-phase script. It minimizes token waste, avoids infinite retry loops,
> and always ends with a clear verdict: *what broke and what to do next.*

**Target audience**: AI agents debugging API calls, and the developers who guide them
**Time per run**: 2-5 minutes
**Output**: Verdict (one of 7 types) + next step

---

## ⚠️ Anti-Hallucination Guardrails

1. **Show the raw response.** Never summarize a 4xx/5xx error without quoting the exact
   response body and HTTP status code. "The API returned an error" is not a diagnosis.
2. **One hypothesis at a time.** If Phase 4 finds multiple anomalies, investigate the
   MOST LIKELY cause first before branching. Parallel guessing wastes tokens.
3. **Stop after 3 failed attempts.** If 3 different fix attempts all fail, escalate — do
   not try a 4th. The issue needs human inspection of upstream docs or source.
4. **Token budget: cap each phase at 5 tool calls.** If Phase 2's test matrix exceeds 5
   curl commands, stop and ask the user which endpoints to prioritize.
5. **Never retry a 401/403 without updated credentials.** Replaying an auth failure is
   guaranteed to fail. Ask the user for new credentials first.

---

## Phase 1: Environment Check (1-2 tool calls)

Goal: Rule out the obvious — wrong URL, wrong port, network issue, missing tool.

```bash
# 1a. Is curl installed and functional?
curl --version 2>/dev/null || echo "missing-curl"

# 1b. Is the target reachable at all? (connectivity)
curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://api.example.com" 2>&1

# 1c. DNS resolution
nslookup api.example.com 2>/dev/null || host api.example.com 2>/dev/null
```

**Decision tree after Phase 1:**

| Observation | Verdict | Next Step |
|-------------|---------|-----------|
| curl not installed | `TOOL_MISSING` | `apt/brew install curl`, then retry |
| Connection refused | `NETWORK_UNREACHABLE` | Check if service is up; verify URL |
| DNS fails | `DNS_FAILURE` | Check `/etc/hosts` and DNS config |
| HTTP response (any code) | — | Proceed to Phase 2 |

---

## Phase 2: Test Matrix (1-3 tool calls)

Goal: Map the failure boundaries — does it fail on ALL methods, or only specific ones?
Run a minimal 3×1 matrix (method × endpoint):

```bash
# GET the root/health endpoint — the most permissive test
curl -sv https://api.example.com/health 2>&1 | head -30

# GET the failing endpoint
curl -sv https://api.example.com/v1/resource 2>&1 | head -30

# OPTIONS (reveals allowed methods, CORS policy)
curl -sv -X OPTIONS https://api.example.com/v1/resource 2>&1 | head -30
```

**Record for each call:**
- HTTP status code
- Response body (first 200 chars)
- Response time (from `-w %{time_total}`)
- Any TLS/certificate warnings

**Decision tree after Phase 2:**

| Matrix Pattern | Likely Cause | Proceed To |
|----------------|--------------|------------|
| All calls fail (connection error) | Phase 1 missed something | Re-check Phase 1 |
| GET /health OK, GET /resource fails | Endpoint-specific issue | Phase 3 |
| OPTIONS returns 405 | Method restriction | Check API docs |
| All return 200 but wrong data | Schema mismatch | Phase 4 |

---

## Phase 3: Minimal GET Discovery (1-2 tool calls)

Goal: Find a **known-good** GET endpoint to establish a baseline.

```bash
# Try common discovery/health endpoints
for path in / /health /ping /status /api/v1/status /v1/health; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://api.example.com$path" --connect-timeout 5 2>/dev/null)
  echo "$path → $code"
done
```

If any returns 200, use that response structure as a reference:
- Compare headers (Content-Type, API-Version, RateLimit-Remaining)
- Compare response body structure (JSON fields)

Document the working contract as: *"Baseline: GET <path> returns <status> with <shape>."*

---

## Phase 4: Targeted POST/PUT/Verification (1-2 tool calls)

Goal: Reproduce the failure with MINIMAL payload, then diagnose the exact error.

```bash
# 4a. Reproduce with minimal valid payload
curl -sv -X POST \
  -H "Content-Type: application/json" \
  -d '{"test": true}' \
  "https://api.example.com/v1/resource" \
  --connect-timeout 10 2>&1

# 4b. If auth-related: verify credentials (NEVER log the full token)
# Show only: token present? token format? (header: 'Bearer ***...***')
```

**Record:**
- Exact request sent (headers + body, with secrets redacted)
- Full response headers
- Full response body
- Timing

**Pattern analysis — map to one of these verdicts:**

| Response Pattern | Verdict | Description |
|-----------------|---------|-------------|
| `4xx` with `{"error":"..."}` | `API_ERROR` | API returned a specific application error |
| `401` / `403` | `AUTH_FAILURE` | Credentials missing, expired, or insufficient scope |
| `404` | `ENDPOINT_NOT_FOUND` | URL path or method is wrong |
| `405` | `METHOD_NOT_ALLOWED` | Wrong HTTP method for this endpoint |
| `429` / Rate-limit headers | `RATE_LIMITED` | Exceeded quota; check Retry-After header |
| `5xx` | `SERVER_ERROR` | Service-side failure; check status page |
| Timeout (>30s) | `TIMEOUT` | Request exceeded deadline |
| Connection reset mid-stream | `CONNECTION_DROPPED` | Proxy/LB closed the connection |
| 200 OK but unexpected body | `SCHEMA_MISMATCH` | API changed; response doesn't match docs |

---

## Phase 5: Verdict & Next Step

Output a single structured verdict. Example:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🔌 API Failure Investigation — Verdict
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Verdict:       AUTH_FAILURE
Endpoint:      POST https://api.example.com/v1/resource
Status:        401 Unauthorized
Response:      {"error":"invalid_token","message":"Token has expired"}

Evidence:
  - GET /health → 200 OK (API is reachable)
  - OPTIONS /v1/resource → 204 No Content (CORS configured)
  - POST /v1/resource → 401 (only this fails)

Next Step:
  🔑 Refresh the API token and retry.
  ⚠️ If the new token also fails, check that the token has the
     required scope: `resource:write`.

Token spent: 4 curl calls / 3 phases
```

### Verdict Types (7)

| Verdict | Means | Next Step |
|---------|-------|-----------|
| `TOOL_MISSING` | curl isn't installed | Install curl |
| `NETWORK_UNREACHABLE` | Can't connect at all | Check URL, port, VPN, firewall |
| `DNS_FAILURE` | Hostname won't resolve | Check DNS config |
| `AUTH_FAILURE` | 401/403 | Rotate credentials, check scope |
| `CLIENT_ERROR` | 4xx (non-auth) | Fix request per error message |
| `SERVER_ERROR` | 5xx | Check service status, retry later |
| `SCHEMA_MISMATCH` | 200 but wrong shape | Update client to match new schema |

### When to Escalate

- 3 consecutive failures with different approaches → document what was tried and ask for help
- Any 500 error the user can't fix → file a support ticket with the raw response
- Schema mismatch on a documented API → report a bug to the API provider

---

## Verification

After applying the suggested fix:

1. Re-run the **exact same request** from Phase 4 (same URL, method, headers).
2. Confirm the HTTP status is now `2xx`.
3. Confirm the response body matches the expected schema.
4. Report: *"Fixed. <verdict> → 200 OK. <N> attempts."*

If the fix didn't work:
- Check if the fix was applied correctly (e.g., did the env var actually get exported?)
- Try the next most likely cause from Phase 4's pattern analysis
- After 3 total attempts, escalate

---

## Pitfalls

- **Token leakage in logs**: Never paste a full `Authorization` header value into chat.
  Show only the first 4 and last 4 characters: `Bearer "sk-****...****"`.
- **CORS vs. server errors**: A browser CORS error does NOT mean the server is down.
  Test with `curl -v` (bypasses CORS entirely) before blaming the backend.
- **Rate-limit false positives**: A `429` response may include a useful `Retry-After`
  header. Parse it and report the wait time rather than assuming "try again now."
- **Redirect chains**: A `301`/`302` followed by a `404` on the new location looks like
  a missing page. Always use `-L` (follow redirects) or report the full redirect chain.
- **Proxy interference**: If behind a corporate proxy, `curl`'s connection to `127.0.0.1:7897`
  (Clash, mitmproxy) may succeed while the actual target is unreachable. Run
  `curl -v --noproxy '*' <url>` to bypass.
- **Clash/mitmproxy intercepting localhost calls**: Tools like Clash, mitmproxy, or
  Charles Proxy often set `http_proxy`/`https_proxy` env vars that redirect *all*
  traffic — including connections to `127.0.0.1` and `localhost`. A `curl http://127.0.0.1:8080`
  call meant for a local service may silently route through the proxy, hitting a different
  process or failing with an unexpected response. **Diagnosis**: Compare `curl -v` output
  (look for `Connected to 127.0.0.1:7897` instead of the expected address) and check
  `echo $http_proxy $https_proxy`. **Fix**: Use `--noproxy '*'` to bypass the proxy.
- **`--noproxy '*'` limitations**: The `--noproxy` flag works by matching the target
  hostname against a comma-separated list of patterns. `--noproxy '*'` should bypass all
  proxies, but some proxy configurations (notably mitmproxy in transparent mode, or
  Docker-level `iptables` redirects) intercept traffic at a lower OSI layer that curl
  cannot opt out of. **Diagnosis**: If `--noproxy '*'` still shows the proxy IP in
  `Connected to ...`, try `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY` in the
  same shell, or use `curl --proxy "" <url>` to explicitly clear the proxy.
- **Connection timeout vs. connection refused**: These two errors point to very different
  root causes and must not be confused.

  | Error | `curl` Signal | Meaning | Likely Cause |
  |-------|--------------|---------|-------------|
  | **Connection timeout** | `curl: (28) Connection timed out` | The OS sent a TCP SYN, waited N seconds, and never received a SYN-ACK. | Firewall dropping outbound packets; wrong IP/port; service not listening on that address; network routing issue. |
  | **Connection refused** | `curl: (7) Failed to connect to ... Connection refused` | The TCP handshake reached the host, but the host sent back a RST (reset) because no process is listening on that port. | Service is down; port mismatch; service listening on different interface (e.g., `127.0.0.1` only but you're hitting the external IP). |

  **How to diagnose each**:
  - **Timeout**: Run `curl -v --connect-timeout 5 <url>` and watch for the stall. Use
    `ping <host>` to check basic reachability. Use `mtr <host>` or `traceroute` to find
    where packets are dropped. A timeout on the first hop usually points to a local
    firewall (iptables, Windows Defender, VPN kill-switch).
  - **Refused**: Run `curl -v <url>` — you'll see `TCP_NODELAY set` and then immediately
    `Connection refused`, no delay. On the server, verify `ss -tlnp | grep <port>` or
    `netstat -tlnp | grep <port>` shows the expected PID. A common pitfall: the service
    binds to `127.0.0.1` only, but `curl` resolves the hostname to `::1` (IPv6 localhost)
    or a public IP — use `curl -v http://127.0.0.1:<port>` to test explicitly.
- **Example — bypass proxy for localhost diagnostics**:
  ```bash
  # Without bypass — may hit the proxy instead of local service
  curl -v http://127.0.0.1:9090/api/health
  
  # With noproxy bypass — forces direct connection
  curl -v --noproxy '*' http://127.0.0.1:9090/api/health
  
  # If noproxy still doesn't work, unset all proxy vars
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
  curl -v http://127.0.0.1:9090/api/health
  
  # Verify what proxy curl would use
  echo "http_proxy=$http_proxy https_proxy=$https_proxy no_proxy=$no_proxy"
  ```
