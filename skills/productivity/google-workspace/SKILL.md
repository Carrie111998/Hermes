---
name: google-workspace
description: "Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python."
version: 1.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials (downloaded from Google Cloud Console)
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya]
---

# Google Workspace

Gmail, Calendar, Drive, Contacts, Sheets, and Docs — through Hermes-managed OAuth and a thin CLI wrapper. When `gws` is installed, the skill uses it as the execution backend for broader Google Workspace coverage; otherwise it falls back to the bundled Python client implementation.

## When to use this skill

Use it when the user explicitly asks to read/send Gmail, list or create Calendar
events, search/upload/share/download Drive files, read or edit Sheets or Docs,
or list Contacts on their own Google account.

**Email only?** Don't use this skill. The `himalaya` skill works with a Gmail App
Password and takes 2 minutes — no Google Cloud project, no OAuth client. Only
come here when the user needs Calendar/Drive/Sheets/Docs/Contacts too.

## Routing table — read the reference before you act

| To do this | Read |
|---|---|
| Run first-time OAuth setup, pick `--services` scopes, exchange the auth code, revoke access | `references/oauth-setup.md` |
| Look up any command + its JSON output shape (Gmail, Calendar, Drive, Contacts, Sheets, Docs) | `references/cli-commands.md` |
| Build a complex Gmail query (`is:unread`, `from:`, `newer_than:`, ...) | `references/gmail-search-syntax.md` |
| Diagnose `NOT_AUTHENTICATED`, `REFRESH_FAILED`, 403s, missing scopes | `references/troubleshooting.md` |

## Scripts

- `scripts/setup.py` — OAuth2 setup (run once to authorize)
- `scripts/google_api.py` — compatibility wrapper CLI. It prefers `gws` for operations when available, while preserving Hermes' existing JSON output contract.

## Red lines

**Credentials & OAuth**
- The OAuth token lives at `~/.hermes/google_token.json` and auto-refreshes.
  Never print, cat, summarize, or transmit that file, `google_client_secret.json`,
  or `~/.hermes/google_oauth_pending.json`. Never echo a client secret or refresh
  token into the conversation.
- Never ask the user to paste a client secret into chat — have them download the
  JSON from Google Cloud Console and give you the **file path** instead.
- The authorization **code** the user pastes back is short-lived and single-use.
  Pass it straight to `--auth-code`; do not repeat it back in your reply.
- Request the narrowest `--services` set that covers the task. Don't default to
  `all` when the user only needs email+calendar.
- **Check auth before first use** — run `setup.py --check`. If it fails, guide the
  user through `references/oauth-setup.md`.

**Confirm before acting — every one of these needs explicit approval first**
- **Sending email** (`gmail send`, `gmail reply`) — show the full recipient list,
  subject, and body, and wait for approval. Never send to an address you inferred.
- **Creating or deleting calendar events** — show summary, start/end, and the
  attendee list (creating an event with attendees emails those people).
- **Deleting Drive files** — show the file name and ID. Prefer the default trash
  (reversible) over `--permanent`; only use `--permanent` if the user explicitly
  asks for it after being told it is unrecoverable.
- **Sharing Drive files** — show file, target, and role. `--type anyone` makes the
  file public to anyone with the link; call that out explicitly before doing it.
- **Modifying Docs/Sheets or Gmail labels** — show the target and the change.

**Other**
- **Calendar times must include timezone** — always use ISO 8601 with offset
  (e.g. `2026-03-01T10:00:00-06:00`) or UTC (`Z`).
- **Respect rate limits** — avoid rapid-fire sequential API calls. Batch reads
  when possible.

## Minimal end-to-end skeleton

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"

# 1. Auth gate. Anything other than AUTHENTICATED → references/oauth-setup.md
$GSETUP --check

# 2. Read first (cheap, non-destructive) — JSON to stdout.
$GAPI gmail search "is:unread" --max 10
$GAPI gmail get MESSAGE_ID

# 3. Show the user exactly what you intend to send, get approval, THEN write.
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
```

Same shape for the other services: `calendar list` → confirm → `calendar create`;
`drive search`/`drive get` → confirm → `drive upload` / `share` / `delete`.
Full command and output-field catalog: `references/cli-commands.md`.
