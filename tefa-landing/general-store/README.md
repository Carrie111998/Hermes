# General Store (prototype)

A separate retail line — kept apart from `../store/` on purpose. The ESA/TEFA storefront
only ever produces an itemized invoice for a state program to approve; it never collects
payment. Retail goods (a family book, activity books) are a different money flow entirely —
paid by card, taxed at retail — and mixing the two risks a program questioning why a TEFA
invoice includes a coloring book that isn't obviously curriculum. Hence two storefronts,
cross-linked but never merged.

`shop.html` → `product.html` → `checkout.html` is the same browse → detail → cart pattern as
the ESA store, with its own cart (`cart.js`, `localStorage` key `mmm_gs_cart_v1`) so the two
carts never mix.

## What's real here and what isn't

- **The product catalog is real in structure, fake in specifics.** The Vulturian is listed
  with its title only — price, page count, ISBN, cover, and description are all `[___]`
  placeholders, because this is a real book by real people and nothing about its content has
  been invented. The three coloring book titles (*Future Founders*, *Tools & Trades*, *Big
  Feelings, Big Wins*) are suggested working titles tying into the skills-store themes, marked
  with the same dotted-underline "edit me" treatment — confirm or replace them before this
  goes anywhere public.
- **Checkout is a full layout, not a full system.** Contact info, shipping address, shipping
  method, an order summary with a tax line — all real, working UI. Payment is deliberately
  mocked: there is no card-number field anywhere in this codebase. A static page has no
  business collecting raw card numbers (that's a PCI-DSS problem, not a design problem), so
  the payment section is a clearly-labelled placeholder. **Before this goes live, connect an
  actual payment processor** — Stripe, Square, Shopify Payments, or similar — and let it host
  the actual card entry (usually via its own hosted fields or redirect, precisely so you never
  touch raw card data yourself).
- **Sales tax is a placeholder rate** (`TAX_RATE` in `cart.js`, currently 8.25% as a generic
  example), not your real obligation. What you actually owe depends on your nexus, the buyer's
  state, and how your resale permit is set up — connect a real tax calculation step (Stripe
  Tax, TaxJar, Avalara) or your own filing process before publishing a number to customers.
- **"Place order" does not place an order.** It validates the form, clears the cart, and shows
  a confirmation panel that says exactly that — no charge, no email, no created order. That's
  the seam where a real backend needs to go.

## Before it goes live

- Real details for The Vulturian: title spelling, author credit, price, format, ISBN/ASIN if
  it has one, cover image, and back-cover copy.
- Real coloring book titles, covers, page counts, and print costs (swap into `PRICING.md`'s
  formula the same way the ESA store's costs work).
- A real payment processor wired into the checkout section.
- A real tax rate/engine, matched to how your resale permit is actually configured.
- A real shipping-rate source if `$4.95` / `$12.95` isn't accurate for your actual carrier rates.
