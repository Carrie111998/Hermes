# Vendor storefront landing page

A self-contained storefront page for families spending TEFA and other state education
funds with MMM Investment. `index.html` is the deliverable: one file, no build step to
view it, no network requests at all. Open it in a browser, upload it to any static host,
or paste its contents into a vendor profile that accepts HTML.

The page is a store, not a school: five departments of kits (practical/trade, situation
handling & self-command, design & motion, AI & emerging tech, homeschool essentials),
bundles, a funding-setup service for families without an ESA account yet, a carbon
reinvestment ledger, and an ordering flow built around invoice-then-approval rather than
a checkout cart.

## Storefront mockups (`store/`)

`store/shop.html`, `store/product.html`, and `store/order.html` are three linked
prototype pages — a category grid, a fully-built product detail page, and an order/quote
review page with a working (localStorage-only) cart — built to show what running this as
an actual browsable store looks like, rather than one long scrolling page. Open
`store/shop.html`, click a product, add it to your order, and click through to
`store/order.html` to see the whole loop, including the "Request itemized quote" button,
which builds a pre-filled `mailto:` with the cart contents. There's no checkout or payment
step anywhere, on purpose — education funds pay against an approved invoice, not a card.

Every card in the shop links to the one built product page (the AI Literacy Bench Kit) as
a stand-in for what every SKU's page would look like in production. Each mockup carries a
"Prototype — not the live site" banner and prices flagged as illustrative; see
`PRICING.md` for how those prices were actually calculated. These pages are not yet linked
from `index.html` — wire that up once you've picked a direction.

## Editing

Edit `src/page.html` for the main page, or anything under `store/src/` for the storefront
mockups, then rebuild everything at once:

```
python3 build.py
```

That inlines the four subset webfonts from `fonts/` into `index.html`, and inlines the
same fonts plus `store/shared_style.css` (the mockups' shared design tokens) into each
`store/*.html`. Editing the built files directly works too, but the next build overwrites
them.

## Before it goes live

The page ships with every business-specific value left blank. Placeholders render
in clay-red monospace with a dotted underline, so they are easy to find on screen —
search `src/page.html` for `class="fill"` to get all of them.

| # | Placeholder | Where |
|---|---|---|
| 1 | Phone number | hero, contact, footer |
| 2 | Hours of operation | contact |
| 3 | Reply time | contact, ordering step 02 |
| 4 | Programs / states accepted beyond Texas TEFA | funding section |
| 5 | Shipping turnaround | hero counter, ordering step 04 |
| 6 | Product prices | every product card |
| 7 | Bundle prices | bundles |
| 8 | Software licence / 3D print credit specifics | Dept. 03 and 04 products |
| 9 | Funding-setup fee and typical turnaround | funding section |
| 10 | Carbon percentages | carbon ledger |
| 11 | Carbon partner organisation | carbon ledger |
| 12 | Reporting cadence | carbon ledger, FAQ |
| 13 | Direct-purchase terms (non-program buyers) | FAQ |
| 14 | Returns policy | FAQ |
| 15 | Legal business name | FAQ, footer |

Product and bundle prices are no longer arbitrary placeholders in the store mockups —
they're computed from a cost × markup formula documented in `PRICING.md`, using
illustrative example costs. Replace the cost column there with what you actually pay
suppliers, and every price recalculates from the same formula. The published `index.html`
still leaves its own prices as `[___]` placeholders since it doesn't (yet) share the
`store/` product data.

Three claims on this page are assertions about the business rather than copy, and
each is worth a second look before publishing:

- **Program approvals.** TEFA is Texas-specific. Every other state runs its own ESA,
  voucher, or school-choice program with its own vendor registration and its own
  rules about eligible expenses. The page is written to say the business *ships*
  nationwide while listing *only the programs it is actually registered with* — do
  not add a state to that list without registration in hand. "Approved nationwide"
  is not a claim any single vendor approval supports, and the FAQ says so explicitly.
- **The carbon percentages.** The ledger presents a fixed share of proceeds
  committed to carbon reduction, and the page explicitly promises published figures
  families can check. Fill in numbers the business will actually honour; an unmet
  environmental claim is the kind that draws regulatory attention.
- **Funding-setup help.** The copy is deliberately careful to say the business helps
  families *file* an application, never that it can approve one or guarantee an
  award — that decision belongs to the state agency. Keep that distinction intact
  if you edit this section; it is what keeps the service from reading as a
  guarantee it can't back.

## How it is built

- **Fonts.** Bricolage Grotesque (display), Newsreader (body), DM Mono (labels,
  SKUs, and ledger figures), all under the SIL Open Font License. Subset to Latin
  plus the punctuation in use and instanced to one optical size — 78 KB across four
  files, inlined as data URIs so the page never calls out to a font CDN.
- **Themes.** Light and dark are both designed, driven by custom properties.
  `prefers-color-scheme` carries the OS preference and `data-theme` on the root
  element overrides it in either direction.
- **Accessibility.** Text and control colours are checked against WCAG AA on both
  grounds. The accent splits into `--accent` for anything carrying text and
  `--accent-lit` for decoration only, because the brighter tone does not hold
  contrast as small type on the light paper.
- **Print.** Families print vendor pages for their funding file, so there is a print
  stylesheet: navigation and buttons drop out, the dark bands invert to white, and
  FAQ answers expand.
- **Motion.** A staged hero reveal and scroll-triggered fades, both disabled under
  `prefers-reduced-motion`.
