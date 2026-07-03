# gws CLI Helper Commands Reference

Helper commands are hand-crafted shortcuts prefixed with `+`. They simplify common
workflows by combining multiple API calls.

## Gmail Helpers

### +triage
Show unread inbox summary with sender, subject, and snippet.
```bash
gws gmail +triage
gws gmail +triage --max 20
```

### +send
Send an email.
```bash
gws gmail +send --to recipient@example.com --subject "Subject" --body "Body text"
gws gmail +send --to recipient@example.com --subject "HTML Email" --body "<h1>Hello</h1>" --html
gws gmail +send --to a@x.com --cc b@x.com --subject "FYI" --body "See attached"
```

### +reply
Reply to a message (auto-threads).
```bash
gws gmail +reply --message-id MSG_ID --body "Thanks for the update."
```

### +watch
Stream new emails as NDJSON (long-running).
```bash
gws gmail +watch
gws gmail +watch --label INBOX
```

## Calendar Helpers

### +agenda
Show upcoming events.
```bash
gws calendar +agenda              # Next 24 hours
gws calendar +agenda --today      # Today only
gws calendar +agenda --week       # This week
gws calendar +agenda --days 3     # Next 3 days
gws calendar +agenda --timezone Europe/Paris
```

### +insert
Create a calendar event.
```bash
gws calendar +insert \
  --summary "Team Standup" \
  --start "2026-04-01T10:00:00" \
  --end "2026-04-01T10:30:00" \
  --location "Room 3" \
  --attendees "alice@co.com,bob@co.com"
```

## Drive Helpers

### +upload
Upload a file to Google Drive.
```bash
gws drive +upload ./report.pdf
gws drive +upload ./photo.jpg --folder FOLDER_ID
gws drive +upload ./data.csv --name "Q1 Data.csv"
```

## Sheets Helpers

### +read
Read cell values from a spreadsheet.
```bash
gws sheets +read SPREADSHEET_ID 'Sheet1!A1:D10'
gws sheets +read SPREADSHEET_ID 'Sheet1!A:A'   # Entire column
```

### +append
Append rows to a spreadsheet.
```bash
gws sheets +append SPREADSHEET_ID 'Sheet1!A:D' \
  --values '[["Name","Score","Date","Notes"],["Alice","95","2026-04-01","Great"]]'
```

## Docs Helpers

### +write
Append text to a Google Doc.
```bash
gws docs +write DOC_ID --text "New paragraph to append."
```

## Chat Helpers

### +send
Send a message to a Google Chat space.
```bash
gws chat +send --space "spaces/SPACE_ID" --text "Deployment complete."
gws chat +send --space "spaces/SPACE_ID" --text "Alert: build failed!" --thread "spaces/SPACE_ID/threads/THREAD_ID"
```

## Workflow Helpers

### +standup-report
Generate a standup report: today's meetings + Google Tasks.
```bash
gws workflow +standup-report
gws workflow +standup-report --timezone Europe/Paris
```

### +meeting-prep
Prepare for the next upcoming meeting: attendees, agenda, related docs.
```bash
gws workflow +meeting-prep
```

### +weekly-digest
Generate a weekly summary: meetings attended, emails sent/received stats, Drive activity.
```bash
gws workflow +weekly-digest
gws workflow +weekly-digest --timezone Europe/Paris
```

## Common Flags (All Commands)

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview the API request without executing |
| `--page-all` | Auto-paginate all results (NDJSON output) |
| `--page-limit N` | Max pages to fetch (default: 10) |
| `--page-delay MS` | Delay between pages in ms (default: 100) |
| `--timezone TZ` | Override timezone (e.g., `Europe/Paris`) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_WORKSPACE_CLI_TOKEN` | Pre-obtained OAuth access token |
| `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` | Path to credentials JSON |
| `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` | Override config directory |
| `GOOGLE_WORKSPACE_CLI_LOG` | Log level (e.g., `gws=debug`) |
