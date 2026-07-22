# Exploration: Hermes Web Search/Extract via 9router

**Date:** 2026-07-18  
**Status:** Exploration concluded; implementation not started  
**Repositories inspected:**
- `O:\workspaces\oss\hermes-agent`
- `O:\workspaces\oss\9router`

## Mission

Enable Hermes' existing `web_search` and `web_extract` tools to use 9router's provider-routed web APIs:

- `POST /v1/search`
- `POST /v1/web/fetch`

The caller should select a 9router web backend such as `tavily`, `firecrawl`, `jina-reader`, `exa`, or `gemini` through Hermes configuration, while keeping the existing Hermes tool schemas and output contracts stable.

This document is analysis only. No application code, production config, credentials, or deployment state was changed.

## Current Hermes implementation

### Tool dispatch

`tools/web_tools.py` owns the public tool wrappers:

- `web_search_tool(query, limit)`
- `web_extract_tool(urls, char_limit, ...)`

Backend selection is capability-specific:

```text
web.search_backend → web.backend → auto-detection
web.extract_backend → web.backend → auto-detection
```

The public tool schema is stable and intentionally provider-neutral.

### Provider extension seam

`agent/web_search_provider.py` defines `WebSearchProvider`:

- `name`
- `display_name`
- `is_available()`
- `supports_search()`
- `supports_extract()`
- `search(query, limit)`
- `extract(urls, **kwargs)`

Providers register through `PluginContext.register_web_search_provider()` and are resolved by `agent/web_search_registry.py`. This is the correct integration point; no new core tool is required.

Existing providers include:

```text
plugins/web/firecrawl
plugins/web/tavily
plugins/web/exa
plugins/web/parallel
plugins/web/searxng
plugins/web/brave_free
plugins/web/ddgs
```

Existing provider implementations normalize their upstream responses into Hermes contracts:

```json
{
  "success": true,
  "data": {
    "web": [
      {
        "title": "...",
        "url": "...",
        "description": "...",
        "position": 1
      }
    ]
  }
}
```

For extraction, the provider returns a list of documents with `url`, `title`, `content`, `raw_content`, `metadata`, and optional per-URL `error`.

### Existing configuration

The repository defaults in `hermes_cli/config.py` are:

```yaml
web:
  backend: ""
  search_backend: ""
  extract_backend: ""
  extract_char_limit: 15000
```

The active installation may have a separate local configuration with Firecrawl/gateway values; that is not the repository default and must be inspected separately without recording secrets. `use_gateway` is meaningful to the Firecrawl provider's direct-vs-Nous-gateway selection, not generic routing for arbitrary HTTP web APIs.

### Important current behavior

The current Firecrawl plugin calls the Firecrawl SDK/API contract:

```python
client.search(query=query, limit=limit)
client.scrape(url=url, formats=[...])
```

It cannot be pointed at 9router merely by setting `FIRECRAWL_API_URL`, because 9router exposes a different contract:

```text
POST /v1/search
POST /v1/web/fetch
```

Therefore, `FIRECRAWL_API_URL=https://ai-router.../v1` would be an invalid integration unless the Firecrawl provider is modified to understand 9router (not recommended; it couples two distinct APIs).

## Current 9router implementation

### Routes

The Next.js route files are thin wrappers:

```text
src/app/api/v1/search/route.js
  → src/sse/handlers/search.js
  → open-sse/handlers/search/index.js

src/app/api/v1/web/fetch/route.js
  → src/sse/handlers/fetch.js
  → open-sse/handlers/fetch/index.js
```

The public paths are `/v1/search` and `/v1/web/fetch`.

### Search request contract

`handleSearch()` accepts either `provider` or `model`; the sample uses `model`:

```json
{
  "model": "tavily",
  "query": "What is the latest news about AI?",
  "search_type": "web",
  "max_results": 5
}
```

Forwarded/accepted optional fields include:

```text
country
language
time_range
offset
domain_filter
content_options
provider_options
```

9router resolves the provider through its internal `AI_PROVIDERS` registry. It then obtains provider credentials from its own account/connection store and calls the dedicated upstream or a chat fallback where supported.

The core success payload has this shape (before route/auth wrapping):

```json
{
  "provider": "tavily",
  "query": "...",
  "results": [
    {
      "title": "...",
      "url": "...",
      "snippet": "..."
    }
  ],
  "answer": null,
  "usage": {"queries_used": 1, "search_cost_usd": 0},
  "metrics": {
    "response_time_ms": 123,
    "upstream_latency_ms": 120,
    "total_results_available": 5
  },
  "errors": []
}
```

The outer route returns JSON. Authentication is handled by 9router using the request bearer token if API-key enforcement is enabled. The request credential must never be placed in `config.yaml` or source code.

### Fetch request contract

The following is the raw 9router API sample contract. In this upstream contract, `max_characters: 0` means unlimited. It is not the proposed Hermes adapter mapping; the adapter mapping appears later under **Provider behavior specification** and always uses Hermes' effective character limit.

`handleFetch()` accepts:

```json
{
  "model": "firecrawl",
  "url": "https://example.com",
  "format": "markdown",
  "max_characters": 0
}
```

Accepted aliases are `provider` or `model`; provider-as-model is intentional.

9router validates the URL and applies an SSRF/public-URL guard before calling the selected provider. Supported fetch implementations visible in the inspected core include:

```text
firecrawl
jina-reader
 tavily
exa
```

The normalized success data is:

```json
{
  "provider": "firecrawl",
  "url": "https://example.com",
  "title": "Example Domain",
  "content": {
    "format": "markdown",
    "text": "...",
    "length": 1250
  },
  "metadata": {
    "author": null,
    "published_at": null,
    "language": null
  },
  "usage": {"fetch_cost_usd": null},
  "metrics": {
    "response_time_ms": 200,
    "upstream_latency_ms": 180
  }
}
```

### Provider naming

9router uses provider/model IDs as the routing selector. Hermes must preserve these as configurable request values, not assume that the selected provider is a model-generation LLM.

Capability truth from the inspected 9router provider registry:

| 9router selector | Search | Fetch |
|---|---:|---:|
| `tavily` | yes | yes |
| `firecrawl` | no | yes |
| `jina-reader` | no | yes |
| `exa` | yes | yes |
| `gemini` | yes, via `searchViaChat` | no |

The source registry explicitly marks `tavily` and `exa` with both `webSearch` and `webFetch` service kinds. `firecrawl` is fetch-only, and `jina-reader` is fetch-only. Gemini is source-supported for search through `searchViaChat`; it is not fetch-supported. Gemini search still requires mocked and opt-in live contract tests before production enablement.

## Verified runtime evidence

A 9router `/v1/search` request using the `tavily` selector returned HTTP 200 and a valid JSON payload with `provider: tavily` and `results` entries. The credential was supplied only for the live test and was not copied into files or output.

The Hermes code was inspected directly. No Hermes configuration or application code was modified during exploration.

The live `/v1/web/fetch` authenticated test remains a required implementation gate. It must be run with a secret loaded from the local credential store/environment, never copied from chat into commands or files.

## Proposed architecture

```text
Hermes web_search tool
        │
        ▼
WebSearchProvider registry
        │  web.search_backend: 9router
        ▼
NineRouterWebProvider
        │
        ├── POST {base_url}/v1/search
        │       {model: search_model, query, max_results, ...}
        │
        └── POST {base_url}/v1/web/fetch
                {model: fetch_model, url, format, max_characters}
        │
        ▼
Normalize to existing Hermes contracts
```

### Why one new provider

Use one provider named `9router` (or `9router-web`, with the final name chosen before implementation) because:

- authentication and base URL are shared;
- search and fetch are separate capabilities but use the same gateway;
- backend selection (`tavily`, `firecrawl`, `jina-reader`, `exa`, `gemini`) belongs in request payloads;
- Hermes already supports one provider advertising both capabilities;
- this avoids duplicating five Hermes providers that would all implement the same transport.

### Proposed config shape

Behavioral configuration belongs in `config.yaml`; the credential belongs in `.env` or the Hermes credential layer.

Recommended shape:

```yaml
web:
  search_backend: 9router
  extract_backend: 9router

  9router:
    base_url: https://ai-router.ptdev.vip
    search_model: tavily
    fetch_model: firecrawl
    timeout_seconds: 30
    max_results: 5
    fetch_format: markdown
```

Recommended secret:

```text
NINE_ROUTER_API_KEY=...
```

The final credential name should follow Hermes conventions and be added to the config/setup metadata so `hermes tools` can report/setup it. Do not use an `HERMES_*` variable for non-secret behavior. Do not store the key in YAML.

Alternative if existing custom-provider configuration is the project convention:

```yaml
web:
  search_backend: 9router
  extract_backend: 9router
  9router:
    base_url: https://ai-router.ptdev.vip
    credential_env: NINE_ROUTER_API_KEY
    search_model: tavily
    fetch_model: firecrawl
```

The provider should read the credential through `get_provider_env()` or an equivalent config-aware resolver so environment and Hermes `.env` both work.

## Provider behavior specification

### Availability

`is_available()` must be a cheap local check only:

```text
base_url is configured AND NINE_ROUTER_API_KEY is present
```

It must not make a network request. This is important because registry availability checks run during tool registration and UI/status refreshes.

### Search

Input from Hermes:

```text
query: str
limit: int
```

Request mapping:

```json
{
  "model": "<search_model>",
  "query": "<query>",
  "search_type": "web",
  "max_results": <integer-limit>
}
```

The provider should support only the fields currently exposed by Hermes unless the tool schema is intentionally expanded later. Clamp `limit` to 1–100 at the tool layer and to the 9router/provider limit in the adapter.

Response mapping:

```text
9router results[].title       → Hermes data.web[].title
9router results[].url         → Hermes data.web[].url
9router results[].snippet     → Hermes data.web[].description
result index                  → Hermes data.web[].position
```

Preserve useful optional fields in metadata only if the existing tool contract permits it; do not break model-facing output shape.

### Extract

Input from Hermes:

```text
urls: List[str]
char_limit: int | None
```

9router's fetch endpoint accepts one URL per request, so the adapter should issue one request per URL with a small bounded semaphore (default 4; configurable only if required). Preserve input order deterministically, propagate interruption/cancellation, and ensure one failed URL does not abort or reorder the remaining URLs.

Current Hermes dispatch passes only `format` into `provider.extract()` and applies `char_limit` after provider return. To implement the positive `char_limit` → 9router `max_characters` mapping below, the implementation must first change the core dispatch to pass the effective/requested character limit into the provider while retaining the existing local post-processing bound. This is a required core change, not an assumption about the current provider ABC.

Request mapping per URL:

```json
{
  "model": "<fetch_model>",
  "url": "<url>",
  "format": "markdown",
  "max_characters": <effective-char-limit>
}
```

`max_characters` behavior:

- The dispatcher must compute one effective value using Hermes' existing default/clamp semantics (currently the configured default is 15,000 and explicit values are clamped to the supported range), then pass that effective integer into the provider.
- The adapter sends that effective integer as `max_characters` for every fetch, including when the caller omits `char_limit`; it must not convert an omitted/default value to `0` in the initial design.
- Hermes still enforces the same local final bound defensively after the provider returns.

Response mapping:

```text
payload.url                  → document.url
payload.title                → document.title
payload.content.text        → document.content/raw_content
payload.metadata             → document.metadata
payload.error                → document.error
```

The currently inspected 9router source returns the bare normalized provider object above. Optional `{data: payload}` compatibility may be added only as a documented, fixture-tested shim; it must not hide an untested contract change.

### Error handling

The adapter must:

- redact authorization values from exception messages/logs;
- preserve HTTP status in debug logs, not secret-bearing response bodies;
- return a provider-level failure for search;
- return per-URL failures for extract;
- distinguish 401/403 (credential/configuration) from 429 (upstream rate limit) and 5xx/timeouts (retryable upstream failure);
- use a bounded timeout and no unbounded retry loop;
- never silently fall back to a different backend when `web.search_backend: 9router` or `web.extract_backend: 9router` is explicit. This requires a Hermes dispatch change: explicit registered providers must be selected even when `is_available()` is false, so the provider returns a precise missing-credential/configuration error.

### SSRF boundary

Hermes already applies URL safety/policy checks in web extraction providers. 9router also applies an SSRF/public-URL guard. The adapter must retain Hermes' local guard before making outbound requests; upstream validation is defense in depth, not a replacement.

The adapter must normalize and validate `base_url` before attaching credentials: HTTPS by default; reject URL credentials, query strings, fragments, unsupported schemes, and unsafe/private hosts unless a narrowly scoped development override is explicit. Tests must prove invalid endpoints never receive an Authorization header and that logs/errors redact credentials.

## Options considered

### Option A — New Hermes `9router` web plugin (recommended)

**Pros**

- Uses the existing provider ABC and registry.
- Keeps core tool schemas unchanged.
- Supports both `/v1/search` and `/v1/web/fetch`.
- One transport/auth implementation supports multiple upstream web backends.
- Can be installed/disabled independently.

**Cons**

- Requires new config/setup metadata and adapter tests.
- Must define a stable credential name and endpoint validation behavior.

### Option B — Extend Firecrawl provider with a 9router mode

**Rejected.** It conflates Firecrawl's native API with 9router's provider-routing API, makes `use_gateway` semantics ambiguous, and prevents clean use of Tavily/Jina/Exa through the same route.

### Option C — Add a generic custom HTTP web provider config

**Possible future abstraction, not first implementation.** It could support arbitrary method/body templates, but introduces a large untyped configuration surface, secret/header risks, and harder validation. A focused 9router adapter is safer and smaller for the stated mission.

### Option D — Use an MCP server

**Not required for this mission.** Hermes already has `web_search` and `web_extract` as core tools and 9router has a stable HTTP contract. MCP would add another process/transport and duplicate tool semantics.

## Required Hermes implementation scope

1. Add a standalone web provider module under `plugins/web/9router/`.
2. Implement transport with the project's existing HTTP dependency/style and explicit timeout.
3. Implement config-aware base URL and credential lookup.
4. Implement `search()` and `extract()` normalization.
5. Register provider through the plugin entry point.
6. Add `web.9router` defaults to config defaults without changing existing Firecrawl behavior.
7. Add secret/setup metadata so `hermes tools` can identify missing configuration.
8. Add tests for:
   - availability without network calls;
   - config precedence and base URL normalization;
   - exact search request payload;
   - search result normalization;
   - fetch request payload and response normalization;
   - effective `char_limit` propagation from the core dispatcher into the provider and local post-processing bound, covering omitted/default, positive, invalid, and clamped values;
   - multiple URLs and per-URL failures;
   - timeout/status/error redaction;
   - explicit backend selection and no silent fallback;
   - SSRF policy remains enforced;
   - real imports against temporary `HERMES_HOME`.
9. Add authenticated E2E tests as opt-in, reading the key from environment and never printing it. The report must record only endpoint class (public/local, not secrets), route, selector, timestamp, HTTP status, response-schema assertions, redacted output, and deployed 9router revision/image identifier. Both search and fetch live gates are required before production enablement.
10. Update user documentation and setup/status output.

## Required 9router review/implementation scope

The 9router repo already exposes the routes and provider dispatch. Before declaring integration complete:

1. Confirm deployed route and source route parity for `/v1/search` and `/v1/web/fetch`.
2. Confirm the provider registry advertises every requested backend for each capability.
3. Confirm the exact response shapes for:
   - Tavily search;
   - Firecrawl fetch;
   - Jina Reader fetch;
   - Exa search/fetch;
   - Gemini search, if supported.
4. Confirm authentication policy for external clients and bearer-token scope.
5. Confirm SSRF policy and response-size limits on `/v1/web/fetch`.
6. Confirm timeout/rate-limit/error status behavior.
7. Add or run route contract tests using mocked upstream providers.
8. Add an E2E smoke test with a secret injected by CI/runtime, not committed.
9. Verify logs mask bearer keys and do not log fetched private URLs or page contents unnecessarily.
10. Deploy only after route tests and Hermes adapter tests pass.

## Cross-repository verification matrix

| Test | Hermes | 9router | Required |
|---|---:|---:|---:|
| Provider import/registration | yes | n/a | yes |
| Missing credential | yes | yes | yes |
| Search payload contract | yes mock | yes route/core | yes |
| Search live Tavily | opt-in | yes | yes |
| Fetch payload contract | yes mock | yes route/core | yes |
| Fetch live Firecrawl | opt-in | yes | yes |
| Fetch live Jina | opt-in | yes | if advertised |
| Fetch live Exa | opt-in | yes | if advertised |
| Gemini search via chat | opt-in | yes | after mocked + live contract test |
| 401/403/429/5xx mapping | yes | yes | yes |
| Timeout behavior | yes | yes | yes |
| SSRF/private URL rejection | yes | yes | yes |
| Secret redaction | yes | yes | yes |
| Tool output backward compatibility | yes | n/a | yes |

## Deployment design

### Order

1. Implement and test 9router route/provider contract first if any gaps are found.
2. Deploy 9router routes and verify externally with a secret loaded from the runtime secret store.
3. Implement/install Hermes plugin.
4. Configure Hermes with `search_backend: 9router` and `extract_backend: 9router` only after the live smoke test passes.
5. Restart the relevant Hermes process so plugin/config discovery is fresh.
6. Run `web_search` and `web_extract` through the actual Hermes tool path.
7. Inspect logs for provider name, status, latency, and absence of leaked credentials.

### Rollback

Rollback is configuration-only if the provider is additive, but it must restore the actual pre-rollout per-install values captured in a secret-safe deployment record; do not assume repository defaults or Firecrawl:

```yaml
web:
  search_backend: <previous-value>
  extract_backend: <previous-value>
```

If the previous installation used Firecrawl gateway, restore its recorded `use_gateway` value. If not, restore the recorded direct provider. Do not delete existing provider code during initial rollout.

## Open questions that must be resolved before implementation is approved

1. Final stable provider name: `9router` or `9router-web`.
2. Final secret name: `NINE_ROUTER_API_KEY` versus an existing project-standard router key.
3. Whether the Hermes deployment uses `https://ai-router.ptdev.vip` or the local `http://192.168.100.110:20128` endpoint as its canonical base URL.
4. Whether the deployed 9router revision matches source for Gemini chat-based search and confirms fetch remains unsupported.
5. Exact 9router response shape for `/v1/web/fetch` in the deployed version; current source indicates a bare normalized provider object.
6. Whether one URL per fetch request is a hard route contract or batching can be added safely.
7. Whether Hermes should expose optional 9router fields (`search_type`, `country`, `language`, domain filters) in the existing core tool schema. Recommendation: defer; keep the first adapter compatible with current tool schema.
8. Whether cost/metrics should be surfaced in Hermes tool output. Recommendation: preserve current tool output and log metrics only.
9. Whether 9router needs a dedicated health/validation command in `hermes tools`. Recommendation: add setup validation only if it can run without exposing secrets.

## Final recommendation

Proceed with **Option A: a standalone Hermes `9router` web provider plugin**. It is the smallest correct change that leverages the already-existing Hermes provider interface and 9router's native `/v1/search` and `/v1/web/fetch` capabilities.

Do not modify the existing Firecrawl provider to impersonate 9router. Do not add an LLM model configuration for this path: `tavily`, `firecrawl`, `jina-reader`, and `exa` are 9router web-provider selectors; `gemini` is a 9router chat-search selector, not a Hermes auxiliary chat model.

Implementation should begin only after the open questions above are resolved through source/runtime verification, especially the deployed fetch response shape and Gemini fetch capability.

## Evidence index

### Hermes

- `agent/web_search_provider.py:89` — provider ABC.
- `agent/web_search_registry.py:133` — explicit capability provider resolution.
- `tools/web_tools.py:273` — search backend selection.
- `tools/web_tools.py:287` — extract backend selection.
- `plugins/web/tavily/provider.py:35` — existing direct HTTP provider pattern and normalization.
- `plugins/web/firecrawl/provider.py:212` — direct vs managed-gateway behavior.
- `hermes_cli/config.py:1271` — web config defaults.

### 9router

- `src/app/api/v1/search/route.js:17` — search route.
- `src/app/api/v1/web/fetch/route.js:17` — fetch route.
- `src/sse/handlers/search.js:24` — search auth/provider dispatch.
- `src/sse/handlers/fetch.js:25` — fetch auth/SSRF/provider dispatch.
- `open-sse/handlers/search/index.js:147` — search core normalization/envelope.
- `open-sse/handlers/fetch/index.js:88` — fetch provider dispatch and normalized data.
- `src/shared/constants/providers.js:78` — media endpoint declarations.

## Exploration conclusion

The current implementation is sufficiently understood to hand to an implementation agent and reviewer. The correct seam, request/response mappings, security boundaries, test gates, deployment order, and rollback path are identified. Remaining items are bounded verification questions, not architectural unknowns.
