---
name: notion
description: "Notion API + ntn CLI: pages, databases, markdown, Workers."
version: 2.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [NOTION_API_KEY]
metadata:
  hermes:
    tags: [Notion, Productivity, Notes, Database, API, CLI, Workers]
    homepage: https://developers.notion.com
---

# Notion

Talk to Notion two ways. The same integration token works for both — pick by what's available.

◆ **`ntn` CLI** — Notion's official CLI. Shorter syntax, one-line file uploads, required for Workers. macOS + Linux only as of May 2026. **Default when installed.**
◆ **HTTP + curl** — works everywhere including Windows. **Default fallback** when `ntn` isn't installed.

## When to use this skill

Use it when the user explicitly asks to read, search, create, or update Notion
pages, databases (data sources), blocks, or files — or to build a Notion-hosted
Worker (sync / agent tool / webhook). Typical triggers: "add this to my Notion
notes", "what's in my Notion project database", "create a Notion page for
today's meeting", "query my Notion tasks where Status = Active".

Do not use it for generic note-taking with no Notion mention.

## Routing table — read the reference before you act

| To do this | Read |
|---|---|
| Get a token, install `ntn`, pick a path, env vars, API version | `references/setup-and-auth.md` |
| Run any `ntn` command (search, read, create, query, file upload) | `references/ntn-cli.md` |
| Run raw HTTP/curl calls, full endpoint catalog, 3-step file upload | `references/http-api.md` |
| Build database property payloads; database_id vs data_source_id | `references/property-types.md` |
| Construct block JSON (paragraph, heading, callout, code, image, ...) | `references/block-types.md` |
| Write Notion-flavored Markdown (callouts, toggles, columns, mentions) | `references/notion-flavored-markdown.md` |
| Build/deploy a Notion Worker (sync, tool, webhook) | `references/workers.md` |

## Red lines

**Credentials**
- `NOTION_API_KEY` lives in `${HERMES_HOME:-~/.hermes}/.env`. Read it via the
  environment only. Never print it, echo it, paste it into chat, log it, or
  include it in a command whose output you will summarize.
- Never ask the user to paste their token into the conversation. Direct them to
  https://notion.so/my-integrations and to write it into the `.env` file themselves.
- Worker webhook URLs from `ntn workers webhooks list` are secrets too — anyone
  with the URL can POST events. Don't echo them back into shared context.
- Worker secrets go through `ntn workers env set`, never hardcoded in `src/index.ts`.

**Destructive actions — confirm first**
- **Never archive, delete, or overwrite a page, block, database, or file
  without showing the user the exact target (title + ID) and getting explicit
  approval.** This covers `archived: true` / `archived:=true`, `DELETE
  /v1/blocks/...`, and any `PATCH` that replaces existing page content rather
  than appending.
- Prefer append (`PATCH /v1/blocks/{id}/children`) over replace when the user's
  intent is "add to" rather than "rewrite".
- Do not share a page or database with additional people/integrations without asking.

**Access & limits**
- A 404 on a page that visibly exists almost always means the page isn't shared
  with the integration. Tell the user to run page `...` → `Connect to` → the
  integration. Do not work around it by guessing other IDs.
- Rate limit: **~3 requests/second average**. The CLI does not bypass this. Don't
  fan out parallel calls; on 429 back off and retry rather than looping.

## Minimal end-to-end skeleton

```bash
# 0. token present, path chosen
[ -n "$NOTION_API_KEY" ] || { echo "NOTION_API_KEY missing — see references/setup-and-auth.md"; exit 1; }
export NOTION_API_TOKEN=$NOTION_API_KEY NOTION_KEYRING=0
command -v ntn >/dev/null 2>&1 && CLI=ntn || CLI=curl

# 1. find the target page (ntn path)
ntn api v1/search query="Meeting Notes" | jq '.results[0].id'

# 2. read it as Markdown (cheapest form for a model to consume)
ntn api v1/pages/{page_id}/markdown

# 3. append — never replace — after confirming the target with the user
ntn api v1/pages/{page_id}/markdown -X PATCH markdown="## Update

Shipped the prototype."
```

curl equivalent of every step above, plus database queries and uploads, is in
`references/http-api.md`.
