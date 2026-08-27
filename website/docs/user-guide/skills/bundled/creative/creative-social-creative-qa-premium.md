---
title: "Social Creative Qa Premium"
sidebar_label: "Social Creative Qa Premium"
description: "Use when reviewing Instagram/social-media creatives, feed posts, stories, carousel cards, ad graphics, flyers, thumbnails, or campaign assets before delivery"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Social Creative Qa Premium

Use when reviewing Instagram/social-media creatives, feed posts, stories, carousel cards, ad graphics, flyers, thumbnails, or campaign assets before delivery. Provides a strict premium visual QA gate for mobile legibility, hierarchy, text accuracy, anti-basic design quality, safe areas, brand consistency, and export verification.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/creative/social-creative-qa-premium` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `visual-qa`, `instagram`, `social-media`, `design-review`, `accessibility`, `creative` |
| Related skills | [`instagram-art-direction-premium`](/docs/user-guide/skills/bundled/creative/creative-instagram-art-direction-premium), [`claude-design`](/docs/user-guide/skills/bundled/creative/creative-claude-design), [`popular-web-designs`](/docs/user-guide/skills/bundled/creative/creative-popular-web-designs), [`baoyu-infographic`](/docs/user-guide/skills/bundled/creative/creative-baoyu-infographic) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Premium Social Creative QA

## Overview

This skill is the quality gate for static social creatives. Use it after producing Instagram posts, stories, carousel cards, flyers, event graphics, social ads, thumbnails, and local-business campaign assets. It prevents the common failure mode where an image exists, has the right dimensions, and contains the correct words, but still looks amateur, basic, cluttered, or weak on a phone.

A final creative is not verified because a file was generated. It is verified only after the exported asset is inspected at real size, checked for mobile-safe legibility, compared against the brief, and corrected when it fails.

## When to Use

Use this skill when:

- You are about to deliver final Instagram/social creatives.
- The user asks whether a design looks professional, premium, attractive, or ready to post.
- A flyer or post was upgraded and needs approval-level QA.
- Multiple assets need a contact sheet plus individual inspection.
- A design contains critical text: price, date, phone, address, event name, disclaimer, coupon, or CTA.
- The user complains that earlier visuals looked basic, generic, Canva-like, or AI-made.

Do not use it for pure text drafts, backend code, non-visual files, or brand strategy with no rendered artifact. For art direction before production, use `instagram-art-direction-premium` first.

## Verification Levels

Use explicit evidence language:

- **Verified:** exported file exists, dimensions were checked, and visual QA was performed on the actual rendered asset.
- **Done, not visually tested:** file exists but has not been opened/inspected as an image.
- **Inferred:** guidance is based on reading source files, code, prompts, or assumptions, not on the final render.

Never call a creative ready if critical text, logo, pricing, date, or CTA has not been visually checked in the final file.

## Required Inputs

Before QA, gather:

- Final files or preview images.
- Target format and dimensions.
- Source facts: offer, date, time, price, phone, address, CTA, brand name, spelling.
- Brand/reference context when available.
- Intended platform: Instagram feed, story, carousel, ad, thumbnail, WhatsApp, print, or multi-platform.
- Any known risk areas: small text, dense pricing, logo overlay, image model text, cropped footer, busy photo, story safe area.

If the original source flyer or brief is available, compare against it. QA must protect facts as much as aesthetics.

## QA Workflow

1. **List final artifacts.** Confirm the exact files being reviewed.
2. **Check technical properties.** Verify dimensions, file size, format, and presence.
3. **Create a contact sheet** when there are multiple creatives. Contact sheets reveal campaign consistency, repeated mistakes, density imbalance, and weak cards faster than isolated inspection.
4. **Inspect risky assets at real size.** Always open the cards with critical text: pricing, phone, date, address, legal copy, story CTA, or dense body copy.
5. **Score the design.** Use the premium scorecard below.
6. **Fix blockers.** Do not hand off issues as “small adjustments” if they block posting.
7. **Recheck after fixes.** A fix is not verified until the corrected final asset is inspected.
8. **Deliver with evidence.** State what was checked and what remains unverified.

## Technical Checks

For every final file:

- Expected dimensions match target platform.
- File opens without error.
- No accidental transparency if JPEG was expected.
- No placeholder, lorem ipsum, fake UI text, or watermark.
- Filename/order is clear enough for posting.
- Export is not blurry or upscaled from a tiny source.
- If multiple assets share a campaign, they use consistent palette, typography, motif, and logo treatment.

Typical dimensions:

- Instagram feed vertical: 1080×1350.
- Instagram story: 1080×1920.
- Instagram square: 1080×1080.
- Reel cover: 1080×1920 with critical crop-safe center.
- X/Twitter landscape: 1600×900 or 1200×675.
- LinkedIn feed: 1200×1500, 1080×1350, or platform-specific target.

## Mobile Legibility Checks

Inspect the creative as if seen on a phone for one second.

Must pass:

- Headline is readable immediately.
- Date, price, contact, and CTA are readable without zoom.
- Body copy is not below practical mobile size.
- Text is not placed over busy image detail without a real overlay.
- Contrast is strong enough in light and dark areas.
- Story text avoids top/bottom UI interference.
- No line breaks create awkward words, broken phone numbers, broken prices, or ugly widows/orphans.
- CTA is visually anchored, not floating as an afterthought.

Approximate minimums:

- Feed body text: avoid below 34–40 px.
- Feed CTA/date/price: 42–64 px or larger.
- Story body text: 48 px or larger.
- Story CTA: 56–80 px or larger.

## Fact Accuracy Checks

Critical text must match source facts exactly. Check:

- Brand and event names.
- Dates and weekdays.
- Times and time ranges.
- Currency, decimals, package prices, and included items.
- Phone/WhatsApp digits and country/area code.
- Address, city, state, venue, room, or location label.
- URLs, handles, coupon codes, hashtags, and QR codes.
- Health/legal/financial disclaimers when relevant.
- Portuguese/English accents and punctuation if applicable.

If the year is missing and weekday matters, verify with a calendar tool. If a phone number is partially hidden, do not infer the missing digits.

## Premium Scorecard

Score each item from 0 to 5.

- **First-second impact:** Does it stop attention before the viewer scrolls?
- **Hierarchy:** Is the first, second, third, and CTA read obvious?
- **Mobile legibility:** Can a normal viewer understand it on a phone without zoom?
- **Aesthetic quality:** Does it feel designed, not templated?
- **Commercial clarity:** Is the offer/event/action unmistakable?
- **Brand coherence:** Does it fit the business, audience, and story?
- **Composition:** Are spacing, alignment, crop, and balance intentional?
- **Image treatment:** Are photo/illustration/background choices art-directed and consistent?
- **Typography:** Are type choices, scale, weight, and line breaks controlled?
- **CTA strength:** Is the next action visually clear and easy?

Blocking rule: any item below 4 blocks final delivery for a premium job. Fix the asset or clearly label it as draft.

## Anti-Basic Blockers

Block delivery if the creative has:

- Canva-template look with no ownable detail.
- Generic stock background with text slapped on top.
- All text centered without composition logic.
- Random gradient, random icons, or decorative stickers.
- Too many fonts, colors, shadows, outlines, chips, or boxes.
- Weak headline that does not create curiosity, desire, urgency, or clarity.
- Poor image crop or image that does not support the message.
- Text competing with the logo or footer.
- Price/date/phone buried in small type.
- CTA hidden or unclear.
- Logo distorted, low-res, recreated by AI, or miscolored unintentionally.
- Fake text, misspellings, or hallucinated letters from an image model.
- Unclear relationship among multiple cards in a campaign.

If a creative fails because it is basic, do not only adjust margins. Revisit art direction and visual concept.

## Safe-Area Rules

### Feed 1080×1350

- Keep critical text at least 80 px from edges.
- Prefer 96–140 px margins for premium editorial work.
- Avoid placing phone/address/CTA in the last 70–90 px unless a solid footer protects it.
- Leave breathing room around the logo.
- Do not let badges touch corners unless intentionally designed with bleed.

### Story 1080×1920

- Keep critical text away from top app UI and bottom reply controls.
- Useful safe band for critical content: y=240 to y=1640.
- If CTA is in the lower third, keep it comfortably above the bottom UI.
- Avoid dense paragraphs in stories.

### Carousel

- Check every card as an isolated post and as part of the sequence.
- Make sure card numbers, arrows, progress marks, or repeated motifs are consistent.
- Last card should not look like an afterthought.

## Contact Sheet Review

A contact sheet should show:

- Each asset in order.
- Filename or card number labels.
- Enough size to judge hierarchy and density.
- Consistent background color around previews.

When reviewing a contact sheet, look for:

- One weak card dragging down the set.
- Repeated layout causing boredom.
- Inconsistent fonts or colors.
- CTA present on the wrong assets or missing on the final one.
- Overloaded pricing/logistics cards.
- Story version that looks like a stretched feed card.

A contact sheet does not replace real-size inspection for dense text.

## Fix Patterns

Use these remedies depending on failure:

- **Text too small:** reduce words, split into another card, enlarge hierarchy, remove decoration.
- **Busy image:** add gradient/scrim/solid panel, crop differently, blur background subtly, or choose cleaner image.
- **Basic template feel:** add a concept-driven motif, stronger crop, editorial alignment, texture, or custom label system.
- **Weak CTA:** create a high-contrast button/band, move to a predictable action zone, simplify wording.
- **Price/date hidden:** promote to badge, ticket, stamp, or large numeric block.
- **Inconsistent campaign:** define one palette, one motif, one title scale, one CTA pattern.
- **Story cluttered:** remove secondary copy, enlarge main action, move text to safe zone.
- **Awkward line breaks:** manually set line breaks; do not rely on automatic wrapping for headlines.

## QA Report Template

Use a compact report:

```text
Verdict: APPROVE / BLOCK / APPROVE WITH NOTES
Artifacts reviewed: <files/count>
Verified: dimensions, opened exports, contact sheet, real-size checks on <files>
Blocking issues: <if any>
Scores: impact <n>/5, hierarchy <n>/5, legibility <n>/5, aesthetic <n>/5, clarity <n>/5, brand <n>/5
Fixes made: <if any>
Remaining unverified: <if any>
```

For user-facing responses, be concise. Do not dump the entire scorecard unless the user asks for review detail.

## Common Pitfalls

1. **Checking only the contact sheet.** It catches campaign-level issues but can hide small text errors.
2. **Assuming generated text is correct.** Image models frequently corrupt digits, accents, and logos.
3. **Calling a draft final.** If a score is below 4 or critical text is not checked, label it draft.
4. **Treating “inside canvas” as safe.** Text can be technically inside the image and still too close to Instagram UI or visually cramped.
5. **Overlooking the story version.** Stories need different density and safe zones, not just resized feed layouts.
6. **Letting aesthetics override facts.** Wrong phone/date/price is a hard failure even if the design looks beautiful.
7. **Not rechecking after a patch.** Fixes can create new crop, contrast, or spacing problems.

## Verification Checklist

- [ ] Final artifacts listed by exact path or URL.
- [ ] Dimensions checked against target platform.
- [ ] Contact sheet created for multi-asset sets.
- [ ] Real-size inspection performed on text-heavy/risky assets.
- [ ] Critical facts checked against source.
- [ ] Mobile legibility checked.
- [ ] Safe areas checked for feed/story.
- [ ] Anti-basic blockers checked.
- [ ] Scorecard is 4/5 or higher on every premium criterion.
- [ ] Fixes were re-exported and rechecked.
- [ ] Delivery separates verified facts from assumptions and remaining notes.
