---
name: gws-cli
description: Google Workspace CLI (gws) — a unified command-line tool for all Google Workspace APIs. Provides high-level helper commands for Gmail triage, calendar agenda, Drive uploads, standup reports, and weekly digests. Complements the google-workspace Python skill with automation-focused workflows and dynamic API discovery.
version: 1.0.0
author: GillesETOUBLEAU
license: MIT
required_environment_variables:
  - name: GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE
    prompt: "Path to gws credentials JSON file"
    help: "Export from gws auth export --unmasked, or use service account JSON"
    required_for: "all gws operations"
metadata:
  hermes:
    tags: [Google, Workspace, Gmail, Calendar, Drive, Sheets, Docs, Chat, CLI, Automation]
    homepage: https://github.com/googleworkspace/cli
    related_skills: [google-workspace, himalaya]
    requires_toolsets: [terminal]
---

# Google Workspace CLI (gws)

A unified CLI for all Google Workspace APIs — built for automation and agent workflows.
Outputs structured JSON. Supports all Google Discovery APIs dynamically.

## When to Use gws-cli vs google-workspace

| Use Case | Tool |
|----------|------|
| Quick email/calendar/drive operations | `google-workspace` (Python skill) |
| Gmail triage, standup reports, weekly digests | `gws-cli` (this skill) |
| Paginated bulk operations (list all files) | `gws-cli` (--page-all streams NDJSON) |
| Chat messages to Google Spaces | `gws-cli` (Chat API not in Python skill) |
| Any Google API not explicitly wrapped | `gws-cli` (dynamic discovery) |

## Authentication

gws uses the same Google account as the `google-workspace` skill. Auth is bridged
automatically via the entrypoint script.

### Check auth status

```bash
gws auth status
```

### If not authenticated

The entrypoint bridges credentials from the existing google-workspace Python skill.
If that's not set up yet, follow the `google-workspace` skill setup first, then
the bridge script will convert the token for gws.

Alternatively, set up gws directly:

```bash
# Option 1: Use exported credentials file (headless/Railway)
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/path/to/credentials.json

# Option 2: Interactive login (local only)
gws auth login
```

## Helper Commands (High-Level Workflows)

These are hand-crafted shortcuts prefixed with `+`. They combine multiple API calls
into single, useful operations.

### Gmail

```bash
# Triage: summarize unread inbox
gws gmail +triage

# Send email
gws gmail +send --to user@example.com --subject "Hello" --body "Message text"

# Reply to a message
gws gmail +reply --message-id MESSAGE_ID --body "Thanks!"

# Watch for new emails (streams NDJSON)
gws gmail +watch
```

### Calendar

```bash
# Today's agenda
gws calendar +agenda --today

# This week's agenda
gws calendar +agenda --week

# Create an event
gws calendar +insert --summary "Team Sync" --start "2026-04-01T10:00:00" --end "2026-04-01T10:30:00"
```

### Drive

```bash
# Upload a file
gws drive +upload ./report.pdf

# Upload with metadata
gws drive files create --json '{"name": "report.pdf"}' --upload ./report.pdf
```

### Sheets

```bash
# Read cell values
gws sheets +read SPREADSHEET_ID 'Sheet1!A1:D10'

# Append rows
gws sheets +append SPREADSHEET_ID 'Sheet1!A:D' --values '[["new","row","data","here"]]'
```

### Docs

```bash
# Append text to a document
gws docs +write DOC_ID --text "New content to append"
```

### Chat (Google Spaces)

```bash
# Send a message to a Google Chat space
gws chat +send --space "spaces/SPACE_ID" --text "Deploy complete."
```

### Workflow Helpers

```bash
# Standup report: today's meetings + pending tasks
gws workflow +standup-report

# Meeting prep: context for the next upcoming meeting
gws workflow +meeting-prep

# Weekly digest: summary of the past week
gws workflow +weekly-digest
```

## Low-Level API Commands

For any Google API not covered by helpers, use the raw Discovery-based commands.
All return structured JSON.

### Introspect API schema

```bash
# See what parameters an endpoint accepts
gws schema gmail.users.messages.list
gws schema drive.files.list
gws schema calendar.events.list
```

### Gmail (raw)

```bash
# List messages with query
gws gmail users messages list --params '{"q": "is:unread", "maxResults": 10}'

# Get a specific message
gws gmail users messages get --params '{"id": "MESSAGE_ID", "userId": "me"}'

# Send a message
gws gmail users messages send --params '{"userId": "me"}' --json '{"raw": "BASE64_ENCODED"}'
```

### Drive (raw)

```bash
# List files with pagination
gws drive files list --params '{"pageSize": 10, "q": "name contains '\''report'\''"}'

# Stream ALL results (NDJSON)
gws drive files list --params '{"pageSize": 100}' --page-all | jq '.files[].name'
```

### Calendar (raw)

```bash
# List events
gws calendar events list --params '{"calendarId": "primary", "timeMin": "2026-04-01T00:00:00Z", "maxResults": 10, "singleEvents": true, "orderBy": "startTime"}'
```

### Sheets (raw)

```bash
# Create a spreadsheet
gws sheets spreadsheets create --json '{"properties": {"title": "Q1 Budget"}}'

# Get values
gws sheets spreadsheets values get --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1!A1:D10"}'
```

## Pagination

```bash
# Auto-paginate all results (streams NDJSON, one JSON object per page)
gws drive files list --params '{"pageSize": 100}' --page-all

# Limit pages
gws drive files list --params '{"pageSize": 50}' --page-all --page-limit 5

# Add delay between pages (rate limiting)
gws drive files list --params '{"pageSize": 100}' --page-all --page-delay 200
```

## Dry Run

Preview what would be sent without executing:

```bash
gws gmail +send --to user@example.com --subject "Test" --body "Hello" --dry-run
```

## Output Format

All commands return valid JSON. Paginated results with `--page-all` stream NDJSON
(one JSON object per line). Parse with `jq`:

```bash
# Extract file names from Drive listing
gws drive files list --params '{"pageSize": 100}' --page-all | jq -r '.files[].name'

# Count unread emails
gws gmail users messages list --params '{"q": "is:unread", "userId": "me"}' | jq '.resultSizeEstimate'
```

## Timezone

Time-aware helpers use your Google account timezone (cached 24h). Override:

```bash
gws calendar +agenda --today --timezone Europe/Paris
```

## Rules

1. **Never send email, create events, or modify data without user confirmation.** Use `--dry-run` to preview first.
2. **Use helper commands (+) when available** — they handle pagination, formatting, and edge cases.
3. **For bulk operations, use --page-all** to stream all results instead of manual pagination.
4. **Respect rate limits** — use `--page-delay` for large paginated requests.
5. **Prefer the google-workspace Python skill** for operations it already handles well (search, get, send, calendar CRUD). Use gws-cli for workflows, Chat API, bulk ops, and dynamic APIs.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `gws: command not found` | Install: `npm install -g @googleworkspace/cli` |
| Auth error | Check `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` is set and valid |
| `403 Forbidden` | API not enabled in Google Cloud project, or missing OAuth scope |
| Empty results | Check query syntax, try `gws schema SERVICE.METHOD` for params |
| Rate limited | Add `--page-delay 500` between paginated requests |

## References

- `references/gws-helpers.md` — Full list of all helper commands and their options
- Official docs: https://github.com/googleworkspace/cli
