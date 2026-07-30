# TEFA / ESA launch audit

Reviewed against the merged `main` state from pull request #1. This audit treats the repository as the source of truth and does not assume that prototype behavior is production-ready.

## Recommendation

**Hold public launch. Prepare an ESA-only release. Keep General Store outside the public buyer path.**

The separation between ESA-funded ordering and direct retail is correct. However, the current ESA experience still includes launch placeholders, illustrative pricing, stand-in product-detail routing, and an operator-light `mailto:` quote flow. General Store remains a useful prototype but does not have real payments, tax calculation, finalized product data, order creation, or fulfillment operations.

## 1. Keep

- The strict separation between ESA/TEFA invoice flows and direct retail.
- ESA's invoice-first, approval-aware ordering model with no fake card checkout.
- Funding copy that leaves eligibility, approval, and reimbursement decisions with program administrators.
- The shared styling/build structure, accessible themes, print support, and reduced-motion behavior.
- General Store as a development-only preview for staged rollout.

## 2. Improve

- Replace all public placeholders: contact details, hours, response times, shipping turnaround, legal business identity, returns, accepted programs, and operating commitments.
- Verify supplier cost, public price, margin, packaging, inventory/made-to-order rule, and fulfillment expectations for every ESA SKU.
- Give each ESA product a complete, accurate product-detail page instead of routing all cards to one stand-in page.
- Replace the `mailto:`-only quote path with a reliable customer confirmation plus internal owner, record, task, and follow-up.
- Add final mobile/desktop regression coverage after real content is inserted.

## 3. Trim

- Remove duplicate or low-value paths that do not help the first ESA release.
- Remove buyer-facing retail actions that look transactable before the retail backend exists.
- Reduce speculative category/product language until titles, descriptions, prices, and inventory are confirmed.
- Keep the first release focused on the smallest truthful ESA catalog that can be fulfilled well.

## 4. Hide / relabel

- Relabel every ESA cross-link from **General store** to **General Store Preview**.
- Relabel General Store cart, checkout, add-to-cart, and confirmation actions as previews.
- Remove "real checkout," "pay," and "place order" language from the prototype UI.
- Do not link General Store from the customer-facing ESA launch path until retail products, policies, tax, payment, and fulfillment are complete.

## 5. Remaining blockers

### Business facts

Phone, hours, legal business name, response time, shipping turnaround, returns, direct-purchase terms, and support ownership must be verified.

### Product truth

Illustrative pricing, repeated stand-in product pages, unfinished book metadata, working-title coloring books, and placeholder images cannot appear as finished customer facts.

### ESA operations

Quote requests need a dependable confirmation and internal record. The team must be able to receive, own, fulfill, and follow up on each request without improvising.

### Retail operations

General Store needs a PCI-compliant processor, real tax configuration, accurate shipping rates, finalized product metadata, order creation, customer email, and fulfillment tracking.

## Launch gates

The ESA-only release can move forward when all of the following are true:

- No public ESA page contains `[___]`, `[__]`, `TBD`, unfinished business claims, or prototype-only stand-ins.
- Every public ESA product has verified content, price, fit, shipping expectation, and return/support guidance.
- Every quote request sends a customer confirmation and creates an owned internal task/record.
- Navigation makes ESA the obvious primary path and keeps General Store out of the buyer journey.
- Build and smoke checks pass on the final generated pages at mobile and desktop widths.

## Ruthless review conclusion

- **Improved:** launch language, path priority, and the documented source of truth.
- **Trimmed:** false-live retail signals and equal prominence for unfinished commerce.
- **Intentionally left out:** fake payments, generic tax/legal claims, invented book/product details, and speculative public promises.
- **Ship decision:** do not ship the current tree as public commerce. Complete the gates above, then launch ESA only.