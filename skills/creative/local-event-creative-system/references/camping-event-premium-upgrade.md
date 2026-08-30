# Camping/Event Premium Upgrade Pattern

Use this reference when a local camping, eco venue, restaurant night, retreat center, or community event starts from a rough flyer and the user wants Instagram creatives that feel more professional.

## Session Pattern

A basic flyer should not usually be polished into one prettier flyer. Convert it into a campaign system:

1. Hero feed card: promise, date, core experience, CTA.
2. Attraction card: one emotional hook such as pizza, karaoke, music, food, campfire, family day, or nature.
3. Offer/logistics card: packages, prices, inclusions, hours, rules.
4. Story/proof card: venue story, place identity, local trust, real photos when possible.
5. Story/reservation asset: minimal copy, large CTA, safe zone for Instagram UI.
6. Contact sheet: review the set as a campaign before delivery.

## Anti-Basic Direction

For camping and eco venues, avoid generic green flyer treatment. Build a visual system from:

- deep greens, earth beige, wood, canvas, camp-stamp, map-line, firelight amber;
- real venue photos whenever available;
- AI backgrounds only when they contain no text, logo, signs, numbers, or fake UI;
- deterministic overlay text using HTML/SVG/Pillow/Figma after image generation;
- one repeated motif across the set, not random icons on each card.

## QA Pattern That Worked

Use a hard QA loop:

- verify exported dimensions for feed/story;
- create a contact sheet to catch campaign consistency problems;
- inspect text-heavy cards individually at real size;
- patch footers, CTAs, phone numbers, prices, and story safe zones when they look even slightly risky;
- re-open the corrected asset before calling it verified.

## Evidence Labels

Separate evidence clearly:

- Verified: final files exist, dimensions checked, visual QA performed on the actual exports.
- Done, not visually tested: generated/exported but not inspected visually.
- Inferred: direction or copy based on source reading only.

Do not call a visual asset final if it only exists as a file. It must pass real-size visual QA.