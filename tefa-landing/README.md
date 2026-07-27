# TEFA vendor landing page

A self-contained landing page for families using TEFA funds with MMM Investment.
`index.html` is the deliverable: one file, no build step to view it, no network
requests at all. Open it in a browser, upload it to any static host, or paste its
contents into a vendor profile that accepts HTML.

## Editing

Edit `src/page.html`, then rebuild:

```
python3 build.py
```

That inlines the four subset webfonts from `fonts/` and writes `index.html`.
Editing `index.html` directly works too, but the next build overwrites it.

## Before it goes live

The page ships with every business-specific value left blank. Placeholders render
in clay-red monospace with a dotted underline, so they are easy to find on screen —
search `src/page.html` for `class="fill"` to get all of them.

| # | Placeholder | Where |
|---|---|---|
| 1 | Phone number | hero spec, contact, footer |
| 2 | Hours of operation | contact |
| 3 | Reply time | contact, enrollment step 02 |
| 4 | Service area / region | hero eyebrow, contact |
| 5 | Delivery format (online / in person) | hero spec, programs, FAQ |
| 6 | Weeks to first kit | hero spec, enrollment step 04 |
| 7 | Session counts per track | program cards |
| 8 | Tuition figures | tuition tiers |
| 9 | Carbon percentages | carbon ledger |
| 10 | Carbon partner organisation | carbon ledger |
| 11 | Reporting cadence | carbon ledger, FAQ |
| 12 | Refund / swap policy | FAQ |
| 13 | Legal business name | FAQ, footer |

Two claims on this page are assertions about the business rather than copy, and
both are worth a second look before publishing:

- **TEFA vendor status.** The hero carries an "Approved TEFA Vendor" stamp and the
  FAQ states the approval outright. Publish only once that approval is on file.
- **The carbon percentages.** The ledger presents a fixed share of proceeds
  committed to carbon reduction, and the page explicitly promises published
  figures families can check. Fill in numbers the business will actually honour;
  an unmet environmental claim is the kind that draws regulatory attention.

## How it is built

- **Fonts.** Bricolage Grotesque (display), Newsreader (body), DM Mono (labels
  and ledger figures), all under the SIL Open Font License. Subset to Latin plus
  the punctuation in use and instanced to one optical size — 78 KB across four
  files, inlined as data URIs so the page never calls out to a font CDN.
- **Themes.** Light and dark are both designed, driven by custom properties.
  `prefers-color-scheme` carries the OS preference and `data-theme` on the root
  element overrides it in either direction.
- **Accessibility.** Text and control colours are checked against WCAG AA on both
  grounds. The accent splits into `--accent` for anything carrying text and
  `--accent-lit` for decoration only, because the brighter tone does not hold
  contrast as small type on the light paper.
- **Print.** Families print vendor pages for their TEFA file, so there is a print
  stylesheet: navigation and buttons drop out, the dark bands invert to white, and
  FAQ answers expand.
- **Motion.** A staged hero reveal and scroll-triggered fades, both disabled under
  `prefers-reduced-motion`.
