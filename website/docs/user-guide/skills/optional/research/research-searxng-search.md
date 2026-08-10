---
title: "Searxng Search — Free keyless meta-search aggregating 70+ engines"
sidebar_label: "Searxng Search"
description: "Free keyless meta-search aggregating 70+ engines"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Searxng Search

Free keyless meta-search aggregating 70+ engines.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/research/searxng-search` |
| Path | `optional-skills/research/searxng-search` |
| Version | `1.0.1` |
| Author | hermes-agent |
| License | MIT |
| Platforms | linux, macos |
| Tags | `search`, `searxng`, `meta-search`, `self-hosted`, `free`, `fallback` |
| Related skills | [`duckduckgo-search`](/docs/user-guide/skills/optional/research/research-duckduckgo-search), [`domain-intel`](/docs/user-guide/skills/optional/research/research-domain-intel) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# SearXNG Search

Free meta-search using [SearXNG](https://searxng.org/) — a privacy-respecting, self-hosted search aggregator that queries 70+ search engines simultaneously.

**No API key required**, but it does require an instance whose **JSON API is enabled**. In practice this means self-hosting: most public instances serve `format=html` only and reject `format=json` (HTTP 403), or sit behind a bot-check/CAPTCHA wall. Automatically appears as a fallback when the main web search toolset (`FIRECRAWL_API_KEY`) is not configured.

## Configuration

SearXNG requires a `SEARXNG_URL` environment variable pointing to your SearXNG instance:

```bash
# Self-hosted SearXNG (recommended — see "Self-Hosting" below)
SEARXNG_URL=http://127.0.0.1:8888
```

:::warning
The JSON API is opt-in. SearXNG's shipped `settings.yml` sets
`search.formats: [html]`. Until `json` is added to that list, every
`format=json` request returns HTTP 403 and Hermes reports the backend as
unavailable. This is a server-side setting — it cannot be fixed from the client.
:::

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

Requires Python 3.10+. `uv` is a convenient way to get one without touching the
system Python.

```bash
git clone --depth 1 https://github.com/searxng/searxng.git ~/services/searxng
cd ~/services/searxng

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-server.txt
```

Create `~/services/searxng/settings.yml`:

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

Run it with the bundled `granian` WSGI server, then set
`SEARXNG_URL=http://127.0.0.1:8888`:

```bash
cd ~/services/searxng
SEARXNG_SETTINGS_PATH=$PWD/settings.yml \
  .venv/bin/python -m granian --interface wsgi \
  --host 127.0.0.1 --port 8888 searx.webapp:app
```

:::warning
There is no `pip install searxng`. SearXNG is not distributed as a runnable
PyPI package and provides no `searxng-run` entrypoint — the `searxng` name on
PyPI is an unrelated third-party stub. Install from source or use the
container image.
:::

Public instance lists live at [searx.space](https://searx.space/) — but verify
`format=json` support before relying on one, since most instances serve HTML
only or sit behind a bot-check wall.

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
| `SEARXNG_URL` not set | No instance configured | Use a public SearXNG instance or set up your own |
| Connection refused | Instance not running or wrong URL | Check the URL is correct and the instance is running |
| Empty results | Instance blocks the query | Try a different instance or self-host |
| Slow responses | Public instance under load | Self-host or use a less-loaded public instance |
| `json` format not supported | Old SearXNG version | Try `format=rss` or upgrade SearXNG |

## Pitfalls

- **Always set `SEARXNG_URL`**: Without it, the skill cannot function.
- **URL-encode queries**: Spaces and special characters must be URL-encoded in curl, or use `urllib.parse.quote()` in Python.
- **Use `format=json`**: The default format may not be machine-readable. Always request JSON explicitly.
- **Set a timeout**: Always use `--max-time` or `timeout=` to avoid hanging on unreachable instances.
- **Self-hosting is best**: Public instances may go down, rate-limit, or block. A self-hosted instance is reliable.

## Instance Discovery

If `SEARXNG_URL` is not set and the user asks about SearXNG, help them either:
1. Find a public SearXNG instance (search for "public searxng instance")
2. Set up their own with Docker or pip

Public instances are listed at: https://searxng.org/
