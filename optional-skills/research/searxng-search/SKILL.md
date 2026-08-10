---
name: searxng-search
description: Free keyless meta-search aggregating 70+ engines.
version: 1.1.0
author: hermes-agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [search, searxng, meta-search, self-hosted, free, fallback]
    related_skills: [duckduckgo-search, domain-intel]
    fallback_for_toolsets: [web]
---

# SearXNG Search

Free meta-search using [SearXNG](https://searxng.org/) — a privacy-respecting, self-hosted search aggregator that queries 70+ search engines simultaneously.

**No API key required**, but it does require an instance whose **JSON API is enabled**. In practice this means self-hosting: most public instances serve `format=html` only and reject `format=json` (HTTP 403), or sit behind a bot-check/CAPTCHA wall. Automatically appears as a fallback when the main web search toolset (`FIRECRAWL_API_KEY`) is not configured.

## Configuration

SearXNG requires a `SEARXNG_URL` environment variable pointing to your SearXNG instance:

```bash
# Self-hosted SearXNG (recommended — see "Self-Hosting" below)
SEARXNG_URL=http://127.0.0.1:8888
```

> **The JSON API is opt-in.** SearXNG's shipped `settings.yml` sets
> `search.formats: [html]`. Until `json` is added to that list, every
> `format=json` request returns HTTP 403 and Hermes reports the backend as
> unavailable. Enabling it is a server-side change — it cannot be fixed from
> the client.

If no instance is configured, this skill is unavailable and the agent falls back to other search options.

## Detection Flow

Check what is actually available before choosing an approach:

```bash
# Check if SEARXNG_URL is set and the instance is reachable
curl -s --max-time 5 "${SEARXNG_URL}/search?q=test&format=json" | head -c 200
```

Decision tree:
1. If `SEARXNG_URL` is set and the instance responds, use SearXNG
2. If `SEARXNG_URL` is unset or unreachable, fall back to other available search tools
3. If the user wants SearXNG specifically, help them set up an instance or find a public one

## Method 1: CLI via curl (Preferred)

Use `curl` via `terminal` to call the SearXNG JSON API. This avoids assuming any particular Python package is installed.

```bash
# Text search (JSON output)
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=python+async+programming&format=json&engines=google,bing&limit=10"

# With Safesearch off
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=example&format=json&safesearch=0"

# Specific categories (general, news, science, etc.)
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=AI+news&format=json&categories=news"
```

### Common CLI Flags

| Flag | Description | Example |
|------|-------------|---------|
| `q` | Query string (URL-encoded) | `q=python+async` |
| `format` | Output format: `json`, `csv`, `rss` | `format=json` |
| `engines` | Comma-separated engine names | `engines=google,bing,ddg` |
| `limit` | Max results per engine (default 10) | `limit=5` |
| `categories` | Filter by category | `categories=news,science` |
| `safesearch` | 0=none, 1=moderate, 2=strict | `safesearch=0` |
| `time_range` | Filter: `day`, `week`, `month`, `year` | `time_range=week` |

### Parsing JSON Results

```bash
# Extract titles and URLs from JSON
curl -s --max-time 10 "${SEARXNG_URL}/search?q=fastapi&format=json&limit=5" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data.get('results', []):
    print(r.get('title',''))
    print(r.get('url',''))
    print(r.get('content','')[:200])
    print()
"
```

Returns per result: `title`, `url`, `content` (snippet), `engine`, `parsed_url`, `img_src`, `thumbnail`, `author`, `published_date`

## Method 2: Python API via `requests`

Use the SearXNG REST API directly from Python with the `requests` library:

```python
import os, requests, urllib.parse

base_url = os.environ.get("SEARXNG_URL", "")
if not base_url:
    raise RuntimeError("SEARXNG_URL is not set")

query = "fastapi deployment guide"
params = {
    "q": query,
    "format": "json",
    "limit": 5,
    "engines": "google,bing",
}

resp = requests.get(f"{base_url}/search", params=params, timeout=10)
resp.raise_for_status()
data = resp.json()

for r in data.get("results", []):
    print(r["title"])
    print(r["url"])
    print(r.get("content", "")[:200])
    print()
```

## Self-Hosting SearXNG

Self-hosting is the reliable path, because you control whether the JSON API is enabled.

### Option A: Docker

```bash
docker run -d --name searxng -p 8888:8080 \
  -v "$(pwd)/searxng:/etc/searxng" \
  searxng/searxng:latest
```

Then add `json` to `search.formats` in `./searxng/settings.yml` and restart the
container (`docker restart searxng`). The default config is HTML-only.

### Option B: From source (no container runtime)

Use this when Docker/Podman/Colima are unavailable. Requires Python 3.10+;
`uv` is a convenient way to get one without touching the system Python.

```bash
git clone --depth 1 https://github.com/searxng/searxng.git ~/services/searxng
cd ~/services/searxng

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-server.txt
```

Create a settings file that inherits the defaults and overrides only what is
needed (`~/services/searxng/settings.yml`):

```yaml
use_default_settings: true

search:
  formats:
    - html
    - json      # REQUIRED — Hermes calls /search?format=json

server:
  port: 8888
  bind_address: "127.0.0.1"
  secret_key: "REPLACE_ME"   # openssl rand -hex 32
  limiter: false             # must be false, or the JSON API gets bot-challenged
  public_instance: false
```

Run it with the bundled `granian` WSGI server:

```bash
cd ~/services/searxng
SEARXNG_SETTINGS_PATH=$PWD/settings.yml \
  .venv/bin/python -m granian --interface wsgi \
  --host 127.0.0.1 --port 8888 searx.webapp:app
```

Then set `SEARXNG_URL=http://127.0.0.1:8888`.

> **There is no `pip install searxng`.** SearXNG is not distributed as a
> runnable PyPI package and provides no `searxng-run` entrypoint. The
> `searxng` name on PyPI is an unrelated third-party stub (an MCP server at
> version `0.0.0.dev0`). Install from source or use the container image.

### Keeping it running (macOS launchd)

Run it as a user agent so it survives logout/reboot and restarts on crash.
`~/Library/LaunchAgents/com.local.searxng.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.local.searxng</string>
    <key>ProgramArguments</key>
    <array><string>/Users/YOU/services/searxng/run-searxng.sh</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.searxng.plist
launchctl print gui/$(id -u)/com.local.searxng | grep -E "state|pid"
```

Use `launchctl bootout gui/$(id -u)/com.local.searxng` to stop it.

Public instance lists live at https://searx.space/ — but check
`format=json` support before relying on one.

## Workflow: Search then Extract

SearXNG returns titles, URLs, and snippets — not full page content. To get full page content, search first and then extract the most relevant URL with `web_extract`, browser tools, or `curl`.

```bash
# Search for relevant pages
curl -s "${SEARXNG_URL}/search?q=fastapi+deployment&format=json&limit=3"
# Output: list of results with titles and URLs

# Then extract the best URL with web_extract
```

## Limitations

- **Instance availability**: If the SearXNG instance is down or unreachable, search fails. Always check `SEARXNG_URL` is set and the instance is reachable.
- **No content extraction**: SearXNG returns snippets, not full page content. Use `web_extract`, browser tools, or `curl` for full articles.
- **Rate limiting**: Some public instances limit requests. Self-hosting avoids this.
- **Engine coverage**: Available engines depend on the SearXNG instance configuration. Some engines may be disabled.
- **Results freshness**: Meta-search aggregates external engines — result freshness depends on those engines.

## Troubleshooting

| Problem | Likely Cause | What To Do |
|---------|--------------|------------|
| `SEARXNG_URL` not set | No instance configured | Set up your own instance (see Self-Hosting) |
| HTTP 403 on `format=json` | Instance is HTML-only (`search.formats: [html]`) | Add `json` to `search.formats` and restart |
| HTML captcha / "Verifying your browser" instead of JSON | Public instance behind a bot wall, or `limiter: true` | Self-host; set `limiter: false` and `public_instance: false` |
| HTTP 429 | Public instance rate-limiting you | Self-host |
| Connection refused | Instance not running or wrong URL | Check the URL and that the process is listening |
| Empty results | Instance blocks the query | Try a different instance or self-host |
| Slow responses | Public instance under load | Self-host or use a less-loaded public instance |
| `json` format not supported | Old SearXNG version | Try `format=rss` or upgrade SearXNG |
| Some engines missing from results | Upstream engine rate-limited/CAPTCHA'd | Normal for metasearch; check `unresponsive_engines` in the JSON |

## Pitfalls

- **Always set `SEARXNG_URL`**: Without it, the skill cannot function.
- **URL-encode queries**: Spaces and special characters must be URL-encoded in curl, or use `urllib.parse.quote()` in Python.
- **Use `format=json`**: The default format may not be machine-readable. Always request JSON explicitly.
- **Set a timeout**: Always use `--max-time` or `timeout=` to avoid hanging on unreachable instances.
- **Self-hosting is best**: Public instances may go down, rate-limit, or block. A self-hosted instance is reliable.

## Instance Discovery

If `SEARXNG_URL` is not set and the user asks about SearXNG, guide them to
**self-host** (see Self-Hosting above) — that is the only setup that reliably
exposes the JSON API this skill depends on.

Before trusting any public instance, verify it actually serves JSON:

```bash
curl -s -m 10 "https://INSTANCE/search?q=test&format=json" | head -c 200
# JSON starting with {"query": ... → usable
# HTML (<!doctype html>) or HTTP 403/429 → not usable
```

Public instance lists: https://searx.space/
