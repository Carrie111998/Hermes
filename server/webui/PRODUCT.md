# Product

Scope: `server/webui` only. This is the Rota customer and admin interface, a
vanilla ES-module SPA served by the FastAPI process in `server/app.py`. It does
not describe the agent runtime, the gateway, or the React dashboard under
`web/src`.

The root `PRODUCT.md` is a separate engineering specification (API routes,
data contracts, sprint order). It stays authoritative for those. This file
carries design context only.

## Register

product

## Users

**Primary: the export sales director** at a manufacturer, usually Turkish,
selling physical goods into foreign markets. Kitchen appliances is the demo
tenant (Silverine); the real customer base spans every export sector, so
nothing in the interface should assume an industry.

He is not a software buyer. He is responsible for revenue in markets he cannot
visit often, and his reputation travels with every message that leaves his
mailbox. He signs a five-figure annual contract with onboarding, which means he
expects implementation help and will put the product through a procurement
review.

**Daily operator: the salesperson** working the approval queue. Same company,
lower seniority, opens the product every morning and clears whatever is
waiting. This is who the interface is actually built for, hour to hour.

**Secondary: the Interfaze admin**, provisioning workspaces and users across
tenants. Admin surfaces are operational, not customer-facing, and may be denser
and more technical.

Context of use: a desktop in an office, an eight-hour day, often an unmanaged
monitor of unknown quality. Not a laptop in a cafe. Not mobile-first.

Job to be done: find qualified buyers abroad, understand why each one is worth
approaching, and open a conversation, without ever sending something that
embarrasses the company.

## Product Purpose

Rota finds buyer companies in foreign markets, researches them against real
sources, drafts outreach in the buyer's language, and then stops and waits for
a human to approve every message before it leaves the salesperson's own
mailbox.

The stopping is the product. Anyone can generate cold email; the value here is
that a cautious exporter can let a machine work his markets without losing
control of what goes out under his name.

Success looks like: the operator clears the approval queue every morning
without reading anything twice, and can answer "why this company?" for any
lead in the system by pointing at a source.

## Brand Personality

**An instrument, not an assistant.** Rota does not have a name, a face, a
personality, or an opinion. It does not say "I". It reports what it did, what
it found, and what it needs. The user is the one with judgment.

Three words: **precise, accountable, unhurried.**

Voice rules:
- The machine reports; it never persuades or congratulates.
- State what happened and what it cost. No exclamation, no encouragement.
- Name the outcome before the action: a button says whether it will save a
  draft or send.
- Missing data is stated as missing. Never rendered as zero, never invented.

Rota is a product of **Interfaze**, an Istanbul digital product studio. The
house brand endorses it (`rota_` above `BY INTERFAZE`) because at this contract
size the agency's implementation credibility is part of what is being bought.

## Anti-references

**The AI-SDR category** (Artisan, 11x, AiSDR). Fake employee avatars, invented
colleague names, "meet your AI teammate" personification, robot imagery. Our
buyer is nervous about a machine emailing his distributors; personifying it
makes that worse, not better.

**Consumer AI assistant styling.** Iridescent orbs, blue-to-purple gradients,
glowing accents on dark grounds, animated aurora. This is the visual signature
of Siri and Gemini and it reads as a toy in an enterprise procurement meeting.

**Generic B2B SaaS.** Gradient-mesh heroes, the hero-metric template (big
number, small label, supporting stats), identical icon-heading-text card grids,
smiling stock photography, safe navy-and-grey.

**Inherited from the Interfaze house system** (`Portfolyo/DESIGN.json`):
- No playful-rounded-agency: no large radii, no pastel gradients, no bubbly
  illustration, no emoji personality.
- No dark-neon-dev-tool: no glowing accent on black, no glassmorphism, no
  terminal cosplay.
- No shadows anywhere. A box-shadow in this system is a bug.

## Design Principles

**1. Evidence language, not AI language.** Say "verified claim", "source
evidence", "estimated range". Never imply generated text is authoritative on
its own. Every lead can be traced to where it came from.

**2. Unknown is a valid state.** Missing valuation, capacity, contact or
research renders as missing. Never as zero, never as a plausible guess. A blank
the user can trust beats a number they cannot.

**3. The machine reports, the human decides.** Every outbound action has an
explicit approval step, and the control says which outcome it will produce.
The one moment the interface asks for attention is the moment it needs a
person.

**4. Required first, advanced on demand.** The common path is visible; timeouts,
rate limits and per-source overrides live behind disclosure. Show effective
values and where they were inherited from.

**5. Density is respect.** This is an eight-hour tool for someone who lives in
spreadsheets. Sparse screens with generous padding read as unfinished, not
calm. Fill the screen with information, divide it with hairlines, and let
typography carry the hierarchy.

## Accessibility & Inclusion

**Target: WCAG 2.1 AA.** Not aspirational. A five-figure enterprise contract
comes with a procurement accessibility review, and this has already been a real
defect here (focus rings were set to `none`, and blue-on-ink text failed at
4.04:1 before being stepped up).

Specific commitments:
- Visible keyboard focus on every interactive element. Mouse focus may be
  quiet; `:focus-visible` never is.
- Text contrast measured, not eyeballed, especially white type over
  photography. Recorded in the CSS next to the rule.
- Full keyboard operation of the approval queue (`A`, `E`, `S`, arrows, `Esc`).
- `prefers-reduced-motion` respected everywhere. The caret's blink is the only
  looping animation and it stops on request; its label carries the state alone.
- Colour is never the sole carrier of meaning. Status has a label and a shape,
  not just a hue.
- Tabular figures on every number so digits align for a spreadsheet native.

Language: interface is English and Turkish. Turkish diacritics
(`İ ı ş Ş ğ Ğ ç Ç ö Ö ü Ü`) must render in every shipped face; this is verified
against the font cmaps, not assumed. Generated *email content* goes out in the
buyer's language (Arabic, Russian, German and others), so the draft preview
isolates bidirectional text, but the interface itself is never RTL.
