---
name: google-workspace
description: "Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python."
version: 1.2.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
  - path: google_client_secret.json
metadata:
  hermes:
    tags: [Google Workspace, Gmail, Calendar, Drive, Docs, Sheets]
    homepage: https://developers.google.com/workspace
---

# Google Workspace

Use the scripts in `scripts/` to bootstrap OAuth and call Google Workspace APIs
through a short-lived access token. The token and client secret live under
`${HERMES_HOME:-~/.hermes}` and are declared in `required_credential_files` so
remote sandboxes can mount only the files this skill needs.

## Setup

- `references/gmail-search-syntax.md` — Gmail search operators (is:unread, from:, newer_than:, etc.)
- `references/daily-brief.md` — daily/morning brief procedure: schedule + conflicts + meeting prep + urgent mail from Gmail and Calendar. Load it when the user asks for a morning brief, meeting preparation, or "what's on my calendar and what email needs attention."

The resulting `${HERMES_HOME:-~/.hermes}/google_token.json` is an authorized
user credential and may be refreshed by `scripts/gws_bridge.py`.
