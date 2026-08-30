---
name: opportunity-radar
description: "Cross-source scan that suggests timely, evidenced actions."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Proactivity, Suggestions, Email, Social, Automation]
    related_skills: [email-inbox-triage, google-workspace, weekly-review-planning, himalaya]
---

# Opportunity Radar

Watch the user's own activity streams — public posts, inbox, calendar, recent Hermes sessions — and surface the moments where two of them connect: a need voiced in one place while the opening to solve it sits unanswered in another ("you posted about needing app signing help; there's an unreplied email from a Microsoft contact in your inbox — reply and ask for an intro"). Inspired by Energy's (getenergy.com) proactive suggestions, adapted to Hermes's cron + connector architecture as a strictly suggest-only radar.

Setup runs once in the foreground; the recurring scan runs as a `cronjob` tick (the `opportunity-radar` automation blueprint scaffolds this). The radar proposes; the user disposes. It never sends, replies, posts, books, or buys — surfacing a suggestion does not imply permission to act on it.

## When to Use

- "Watch my posts and inbox and tell me when there's something I should act on."
- "Connect the dots across my email / X / calendar and suggest next moves."
- "Nudge me when someone in my inbox can solve something I said I needed."
- A cron tick fires for an existing radar (steps 5-7).

Don't use for: routine inbox triage (use `email-inbox-triage`), weekly planning recaps (use `weekly-review-planning`), or company/news tracking (use `competitor-news-monitor`).

## Procedure — Setup (foreground, once)

### 1. Define the radar contract

Pin down with the user: which sources to watch (their public posts via `x_search` with their handle, inbox via connector skills, calendar, recent session history via `session_search`), what kinds of opportunities matter to them (intros, unanswered asks, deadlines meeting capabilities, follow-ups going cold), the suggestion bar (only cross-source or time-sensitive items — never single-source restatements), the scan cadence, and the delivery destination. Done when the contract names every source and states the suggestion bar in one sentence.

### 2. Verify each source with one live read

For each source, do one bounded foreground read now: posts via `x_search` scoped to the user's handle and a date window, email/calendar via the connector skills (`himalaya`, `google-workspace`), session history via `session_search`. Auth walls, missing handles, or empty results surface here, not on the first scheduled run. Drop or renegotiate sources that fail. Done when every contracted source returned real data or was explicitly removed.

### 3. Run a baseline scan and write the state file

Do one full scan now: collect recent signals per source (needs, asks, offers, expiring windows), then look for cross-source links. Write the radar contract plus state to `~/.hermes/opportunity-radar/<slug>.json`: sources with per-source cutoff timestamps, a seen-signal ledger, and a suggestion ledger (each entry: the suggestion, its evidence refs, status `proposed`/`acted`/`dismissed`, and the date). The state file is the source of truth; delivered messages are projections of it. Present any baseline findings to the user. Done when the state file exists with a cutoff for every source and every baseline suggestion is in the ledger.

### 4. Schedule the scan

Only after step 3 succeeded, create the job:

```
cronjob(action="create",
        schedule=<cadence from the contract, e.g. "0 9 * * 1-5">,
        prompt="Load the opportunity-radar skill and run the scan tick for the radar at ~/.hermes/opportunity-radar/<slug>.json.",
        deliver=<user's destination>)
```

Pick a cadence gentle on source rate limits — daily is usually right. Done when the job exists and its prompt names the state-file path.

## Procedure — Tick (each scheduled run)

### 5. Collect new signals since the last cutoff

Load the state file and re-read each source from its stored cutoff: new posts, new or still-unanswered inbound mail, upcoming calendar items, notable recent sessions. A failed source read means unknown state: keep that source's old cutoff, note the failure, and never advance a cutoff past data you did not actually read. Done when every source is either collected-and-advanced or explicitly marked failed with the old cutoff intact.

### 6. Cross-link signals into candidate suggestions

Match needs against openings across sources: a stated problem × a person in the inbox who can help; an expiring window × free calendar time; an inbound offer × a goal from recent sessions. Score each candidate against the contract's suggestion bar, and dedup against the suggestion ledger — a dismissed suggestion stays dismissed unless material new evidence arrives (say so explicitly when reviving one). Done when every surviving candidate carries evidence refs from at least two sources or a concrete time trigger.

### 7. Deliver suggestions with evidence, else stay silent

Deliver at most 3 suggestions, each as: what to do, why now, and the evidence (quote or link the post, name the email thread, cite the date). Append them to the suggestion ledger as `proposed` and update cutoffs. Take no action on any suggestion — even ones that look safe — unless the user replies asking for it. If nothing cleared the bar, respond with `[SILENT]`. Done when delivery matches the ledger and no source action was executed.

## Pitfalls

- Acting on a suggestion instead of proposing it — the radar is read-only against the outside world.
- Single-source restatements ("you got an email from X") — a suggestion needs a cross-source link or a time trigger.
- Re-suggesting dismissed items every run — trust dies in a week.
- Advancing a source cutoff after a failed read, silently skipping the missed window.
- Flooding: more than ~3 suggestions per tick means the bar is set too low.

## Verification

- [ ] Every contracted source passed one foreground read before scheduling.
- [ ] The state file carries per-source cutoffs, a seen-signal ledger, and a suggestion ledger that replays radar history alone.
- [ ] Every delivered suggestion cites evidence from ≥2 sources or a concrete time trigger.
- [ ] Dismissed suggestions were not re-proposed without new evidence.
- [ ] No tick ever executed an external action; no-signal runs were `[SILENT]`.
