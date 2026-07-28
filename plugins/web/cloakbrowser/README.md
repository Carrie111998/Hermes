# CloakBrowser fallback

This plugin is the local browser fallback for web research.

## Routing

1. Hermes `web_search` uses the configured provider.
2. If that provider returns an error or an empty result, `tools.web_tools`
   retries through the local `cloakbrowser` provider.
3. If both fail, the tool returns a safe manual fallback hint for
   `browser-use` or ComputerUse. The final step is manual by design: it may
   reuse a signed-in browser session and must not enter credentials or bypass
   verification challenges.

`web_extract` continues to use the configured extraction provider; the
CloakBrowser provider is available as an explicit `web.extract_backend`.

## Cron / script entry point

For no-agent jobs and operational scripts:

```text
C:\Users\downl\.hermes\scripts\web_search_fallback.py "query" 5
```

Its order is DuckDuckGo Lite → CloakBrowser → CloakBrowser extraction of the
results page. It emits JSON only and never prints credentials.

## Scope

- `web_search` / `web_extract`: automatic fallback.
- OSINT/news jobs: use Hermes web tools, so they inherit the fallback; direct
  JMA retrieval also has a CloakBrowser extraction fallback.
- `lm-twitterer`: no automatic browsing or posting is added. X posting remains
  an explicit, separately authorized action. Research text for a post can use
  the fallback script before a human-approved publish.

## Environment

- `CLOAKBROWSER_HEADLESS=1` (default)
- `CLOAKBROWSER_HUMANIZE=1` only when a headed/interactive flow is explicitly
  requested
- `CLOAKBROWSER_PROXY` optional; do not put credentials in repository files
