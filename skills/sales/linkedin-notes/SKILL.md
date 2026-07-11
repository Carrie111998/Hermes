---
name: linkedin-notes
description: Find the right buyer's LinkedIn profile, store the URL, and generate a personalized connection note for the user to send manually. No LinkedIn automation.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, linkedin, outreach, connection-notes, b2b]
    category: sales
    config:
      - key: sales.linkedin_note_max_chars
        description: Hard character cap for connection notes (LinkedIn truncates around 300)
        default: "280"
        prompt: Max characters for a LinkedIn connection note?
---

# LinkedIn Notes Skill

Support LinkedIn outreach the MVP-compliant way: **discover the profile,
generate the note, let the human send it.** Automated connection requests,
browser automation against LinkedIn, and bulk actions are out of scope —
LinkedIn's User Agreement prohibits automated connecting/messaging, and this
product does not ship it (PRODUCT.md §5).

## Flow (per lead contact)

1. **Find the profile** — web-search for the person:
   `"linkedin.com/in/" "{name}" "{company}"`. Accept ONLY canonical profile
   URLs of the form `linkedin.com/in/{vanity}/`. Never store search-result or
   company-people URLs (`/search/results/people/`, `/company/{slug}/people/`).
   Prefer contacts matching the Company Brain buyer roles (import manager,
   purchasing manager, general manager). Not found → record that and move on.
2. **Store** — save the URL on the contact (`POST /api/v1/linkedin/find-profile`
   result → lead's LinkedIn action record).
3. **Generate the note** — from the company template pack
   (`templates/linkedin-note-templates.md`):
   - Language chosen by the contact's country (template pack country→language
     map).
   - ≤ `sales.linkedin_note_max_chars` characters, hard cap.
   - Personalized to the person and company from research — a note that could
     be sent to anyone is a failed note.
   - Same exclusion filters as email outreach apply (no-outreach markets, excluded
     industries): don't generate notes for contacts the company shouldn't
     approach.
4. **Hand off** — surface profile URL + note to the user. The user opens
   LinkedIn, sends the request manually, and marks the status
   (`mark-opened` / `mark-connection-sent` / `mark-connected` / `mark-replied`).

## Why manual sending

Automated connection requests risk account restriction and violate LinkedIn's
terms — no official LinkedIn API has ever permitted sending them (verified
2026). Everything up to the click (targeting, profile discovery, note quality,
language selection) is where the agent adds value; the click stays human.

Profile discovery may optionally be upgraded from web search to a licensed
enrichment API (e.g. People Data Labs, Apollo, Clay) — better match rates,
same compliance posture, since the product still never touches LinkedIn.
Session-automation wrappers (Unipile-class) are excluded by policy.

## Quality rules for notes

- One clear reason to connect, tied to the recipient's business.
- No links, no pricing, no attachments in the note.
- Never a double dash (`--`); use an em dash or restructure.
- Single language throughout — the recipient's, not the operator's.
