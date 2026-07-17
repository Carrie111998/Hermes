# Personal CRM Pipeline (Dex-style keep-in-touch)

[Dex](https://getdex.com) is a personal CRM whose whole pitch is *"a personal
CRM that reminds you to keep in touch."* We reviewed its feature set and mapped
each capability onto what Hermes already provides, then built the missing piece:
a `crm` plugin that turns Hermes into a relationship pipeline.

## Dex features → Hermes

| Dex feature | What it does | Hermes mapping |
|---|---|---|
| **Keep-in-touch reminders** | Per-contact cadence; nudges you when it's time to reach out | **`crm` plugin** cadences + `hermes crm digest` scheduled via `hermes cron` |
| **Daily digest (text)** | "Here's who to reach out to today" | `hermes crm digest --silent-if-empty` piped to `hermes cron ... --deliver telegram\|sms` |
| **Contact management** | One place for all contacts | **`crm` plugin** durable contact store (`add`/`list`/`show`/`edit`/`rm`) |
| **Interaction timeline** | Every touchpoint logged on the contact | **`crm` plugin** `log` + `show` timeline |
| **Kanban keep-in-touch board** | Visual who's-due view | **`crm` plugin** `board` (Overdue / Due soon / On track / No cadence) |
| **Tags & smart sorting** | Group contacts by interest, cohort, etc. | **`crm` plugin** tags + `--tag` filters everywhere |
| **Birthdays / important dates** | Never miss a date | **`crm` plugin** `date` + `dates --within N` |
| **Map view** | Pins contacts by location | `location` field + existing `productivity/maps` skill |
| **Import (Gmail/LinkedIn/iCloud)** | Bulk-load contacts | `crm export` JSON + existing `google-workspace`/`notion`/`airtable` skills for ingest |
| **AI Assist (draft messages, sort)** | LLM helpers | Native — the agent drafts outreach and tags contacts directly |

The insight: Dex is mostly a **cadence engine + a contact database + a delivery
channel**. Hermes already had a best-in-class scheduler (`cron`) and delivery to
Telegram/Discord/Slack/SMS/email. What was missing was the contact/cadence
model. That's the `crm` plugin.

## The `crm` plugin

A standalone plugin (`plugins/crm/`) modeled on the same durable-store /
normalized-model / operator-CLI architecture as `teams_pipeline`:

- `models.py` — `Contact`, `Interaction`, `ImportantDate`; cadence parsing and
  stable slug ids.
- `store.py` — JSON-backed, lock-guarded, atomic-write store (`CrmStore`).
- `pipeline.py` — pure keep-in-touch math: `compute_status`, `compute_board`,
  `due_contacts`, `upcoming_dates`, `render_digest`. All take an explicit `now`
  so results are deterministic and testable.
- `cli.py` — the `hermes crm ...` operator surface.

### Enable it

```bash
hermes plugins enable crm
```

### Everyday flow

```bash
# Add people with a cadence and optional birthday
hermes crm add "Jane Doe" --email jane@x.com --company Acme \
  --cadence monthly --tag friend --birthday 07-20

# Log a touchpoint (resets the keep-in-touch clock)
hermes crm log jane-doe --kind call --summary "caught up on the new role"
hermes crm touch jane-doe                 # shorthand: "reached out just now"

# See who needs attention
hermes crm due                            # overdue + due-soon list
hermes crm board                          # kanban view
hermes crm dates --within 30              # upcoming birthdays
hermes crm show jane-doe                  # profile + full timeline
```

Cadence accepts presets (`weekly`, `monthly`, `quarterly`, `yearly`), a raw
number of days (`45`), or shorthand (`6w`, `3m`, `1y`). `none` clears it.

### The daily nudge (the whole point of Dex)

`crm digest` renders a ready-to-deliver summary and honors the `[SILENT]`
no-spam convention when nothing is due. The digest must be generated fresh
at every fire — don't use shell `$(...)` substitution, which would run once
at creation time and freeze that instant's text into the job. Two working
patterns:

**Agent-run** — the scheduled prompt tells the agent to run the CLI:

```bash
hermes cron create "0 9 * * *" \
  "Run \`hermes crm digest --silent-if-empty\` in the terminal and relay its
   output verbatim. If it prints [SILENT], reply with just [SILENT]." \
  --name "Keep in touch" \
  --deliver telegram
```

**Script mode** — zero LLM cost; stdout is delivered verbatim and `[SILENT]`
suppresses delivery (see the [cron automation guide](../website/docs/guides/automate-with-cron.md)):

```bash
printf '#!/bin/sh\nexec hermes crm digest --silent-if-empty\n' > ~/.hermes/scripts/crm-digest.sh
chmod +x ~/.hermes/scripts/crm-digest.sh
hermes cron create "0 9 * * *" --script crm-digest.sh --no-agent \
  --name "Keep in touch" --deliver telegram
```

You now get a morning text listing exactly who to reach out to and whose
birthday is coming up — Dex's core loop, running on your own infrastructure,
delivered to any platform, with no per-day limits.

## What's different from Dex

- **Runs on your infrastructure.** Contacts live in `~/.hermes/crm_store.json`,
  not a vendor cloud.
- **Delivered anywhere.** Telegram, Discord, Slack, SMS, email, or a local file
  — not just an in-app board.
- **Agent-native.** The same agent that reminds you can also draft the message,
  research the person, and log the interaction afterward — in one turn.
- **Open and scriptable.** `crm export` gives you the full JSON; the pipeline
  functions are importable for custom automations.

## Not yet built (future work)

- Automatic ingestion from Gmail/Calendar to auto-populate the timeline (Dex's
  passive auto-sync). The building blocks exist in the `google-workspace` skill.
- A `crm import` subcommand to bulk-load from vCard/CSV/exported JSON.
- Model tools (vs. CLI-only) so the agent can query the CRM without shelling out.
