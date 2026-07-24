---
name: scrapling
description: Scrapling web scraping — fetch/extract specific known pages, stealth browser automation, Cloudflare bypass, spider crawling (CLI + Python). Use to scrape target URLs, not for general web search.
version: 1.0.0
author: FEUAZUR
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Web Scraping, Browser, Cloudflare, Stealth, Crawling, Spider]
    related_skills: [duckduckgo-search, domain-intel]
    homepage: https://github.com/D4Vinci/Scrapling
prerequisites:
  commands: [scrapling, python]
---

# Scrapling

[Scrapling](https://github.com/D4Vinci/Scrapling) is a web scraping framework with anti-bot bypass, stealth browser automation, and a spider framework. It provides three fetching strategies (HTTP, dynamic JS, stealth/Cloudflare) and a full CLI.

**This skill is for educational and research purposes only.** Users must comply with local/international data scraping laws and respect website Terms of Service.

## When to Use

- Scraping static HTML pages (faster than browser tools)
- Scraping JS-rendered pages that need a real browser
- Bypassing Cloudflare Turnstile or bot detection
- Crawling multiple pages with a spider
- When the built-in `web_extract` tool does not return the data you need

## Routing

| Intent | Read |
|--------|------|
| Any `scrapling extract` subcommand, all CLI flags, output formats | `references/cli.md` |
| HTTP fetching, sessions, proxies, POST/PUT/DELETE | `references/python-api.md` |
| Dynamic/JS pages, `wait_selector`, `page_action`, resource blocking | `references/python-api.md` |
| Stealth mode, Cloudflare solving, stealth sessions | `references/python-api.md` |
| Element selection: CSS, XPath, find methods, similar elements, navigation | `references/python-api.md` |
| Spider framework, multi-session routing, pause/resume crawling | `references/python-api.md` |

## Installation

```bash
pip install "scrapling[all]"
scrapling install
```

Minimal install (HTTP only, no browser):
```bash
pip install scrapling
```

With browser automation only:
```bash
pip install "scrapling[fetchers]"
scrapling install
```

## Quick Reference

| Approach | Class | Use When |
|----------|-------|----------|
| HTTP | `Fetcher` / `FetcherSession` | Static pages, APIs, fast bulk requests |
| Dynamic | `DynamicFetcher` / `DynamicSession` | JS-rendered content, SPAs |
| Stealth | `StealthyFetcher` / `StealthySession` | Cloudflare, anti-bot protected sites |
| Spider | `Spider` | Multi-page crawling with link following |

## Most Common Invocations

Static page to Markdown (full flag catalog in `references/cli.md`):

```bash
scrapling extract get 'https://example.com' output.md
```

JS-rendered page:

```bash
scrapling extract fetch 'https://example.com' output.md \
  --css-selector '.dynamic-content' \
  --disable-resources \
  --network-idle
```

Cloudflare-protected page:

```bash
scrapling extract stealthy-fetch 'https://protected-site.com' output.html \
  --solve-cloudflare \
  --block-webrtc \
  --hide-canvas
```

Output format follows the file extension (`.html`, `.md`, `.txt`, `.json`/`.jsonl`).

## End-to-End Skeleton (Python)

```python
from scrapling.fetchers import Fetcher

page = Fetcher.get('https://quotes.toscrape.com/')
quotes = page.css('.quote .text::text').getall()
for q in quotes:
    print(q)
```

Escalate only as needed: `Fetcher` -> `DynamicFetcher` (JS) -> `StealthyFetcher` (anti-bot) -> `Spider` (multi-page). Each variant, its flags, and session forms are in `references/python-api.md`.

## Pitfalls

- **Browser install required**: run `scrapling install` after pip install -- without it, `DynamicFetcher` and `StealthyFetcher` will fail
- **Timeouts**: DynamicFetcher/StealthyFetcher timeout is in **milliseconds** (default 30000), Fetcher timeout is in **seconds**
- **Cloudflare bypass**: `solve_cloudflare=True` adds 5-15 seconds to fetch time -- only enable when needed
- **Resource usage**: StealthyFetcher runs a real browser -- limit concurrent usage
- **Legal**: always check robots.txt and website ToS before scraping. This library is for educational and research purposes
- **Python version**: requires Python 3.10+
