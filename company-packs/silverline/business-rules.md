# Silverline — Business Rules (Company Brain seed)

Durable operating rules extracted from the hand-built prototype. These are
company policy, consumed by the sales skills; the skills define mechanics,
this file defines Silverline's choices.

**Every value here is a demo default.** Each client sets their own rules in
the frontend (`/api/v1/company/sales-preferences` and onboarding) per
[`../company-config-schema.yaml`](../company-config-schema.yaml) — nothing in
this file is hardcoded into the product.

## Autonomy and approval

- The agent never makes uncovered decisions. Any decision point not covered by
  a workflow rule → stop and ask the operator. No exceptions — not for volume,
  not for efficiency.
- Daily operation runs without per-day approval, but the operator gets a daily
  plan notification each morning and a results report each evening. Only an
  explicit operator directive changes the plan.
- Supervisors get clean business reports (what was done, results, next steps)
  — never technical logs, error dumps, or workflow mechanics.

## Contact policy

- **Market preferences (client-selected in the UI):** see
  `market-preferences.yaml`. Never reach companies in a `no_outreach_markets`
  country; never scan/research in a `no_research_markets` country;
  `target_markets` are worked first. Any unlisted market is allowed.
- **Exclusion filter:** industrial/commercial kitchen equipment companies
  (HORECA, catering, professional kitchen lines) are never contacted —
  Silverline sells domestic built-in appliances.
- **One channel per customer per day.** Email or WhatsApp, never both.
- **Re-reach window: 30 days**, and only if the customer never replied on any
  channel. A reply on any channel permanently moves the customer to the human
  salesperson.
- **Send windows:** 09:00–12:00 and 13:00–15:00 recipient local time.
- **No auto-replies on any channel.** Inbound responses belong to salespeople.

## Channel status state machine (per customer)

| Status | Meaning | Next action |
|---|---|---|
| `email_pending` | never touched | queue for cold email |
| `email_only` | emailed, no WhatsApp yet | queue for WhatsApp follow-up (if phone) |
| `wa_only` | WhatsApp only, no email | queue for cold email |
| `email_wa` | both channels done | reach complete — skip |
| (empty) | data quality gap | skip until fixed |

- Cold email is NOT a prerequisite for WhatsApp reach.
- Country selection: skip only countries whose reach is complete; pick the
  highest-volume eligible country first.

## Message standards

- Every message individually researched and personalized — no copy-paste blast.
- Single language per message, the recipient's, start to finish. Internal
  notes never leak into customer messages (2026-07-08 incident: internal
  Turkish leaked into 3 customer emails — this rule is absolute).
- Fixed email subject: "Silverline Premium Built-In Kitchen Appliances",
  translated to the target language, nothing appended.
- Never use a double dash (`--`) in any customer-facing text; em dash or
  restructure.
- No emojis in emails; WhatsApp allows 1–2 where the template says so.

## Volume (client-configurable in frontend)

- `daily_email_limit`: demo default 50/day. Set per client in sales
  preferences. (The prototype docs also mention a 500+/day ambition; 50 was
  the enforced cap and is the deliverability-safe demo default.)
- `daily_whatsapp_limit`: demo default 50/day per market batch.
- LinkedIn: notes are generated for manual sending only (see linkedin-notes
  skill); the prototype's automated daily/weekly limits are obsolete.
