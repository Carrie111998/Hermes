# Marketplace Evidence Contract

This contract separates retrieval from reconciliation. Hermes uses existing browser/search tools to retrieve public evidence, then records one normalized observation per source view. `scripts/marketplace_intel.py` validates and reconciles those rows deterministically.

## Product profile

```json
{
  "name": "Apple MacBook Pro M3 Max 128GB",
  "match_any": [
    ["MacBook Pro", "M3 Max", "128 GB"]
  ],
  "exclude_any": ["M4 Max", "64 GB", "96 GB"]
}
```

- `name`: human-readable target.
- `match_any`: OR across groups; every phrase inside one group must appear in the normalized listing title/text.
- `exclude_any`: a matching exclusion rejects the row even if an inclusion group matched.

Use independent profiles when variants have materially different markets. A PS5 original disc console, PS5 Slim, PS5 Digital Edition, and PS5 Pro should not be silently pooled. Likewise, do not pool Max-Q/full-power GPUs, laptop memory tiers, storage tiers when value-sensitive, broken/parts units, or materially different generations.

Include accessory exclusions when the product name is reused in accessory titles. For consoles this commonly includes standalone disc drives, controllers, docks, faceplates, empty boxes, and parts/repair listings.

Normalization is case-insensitive, punctuation-insensitive, and treats capacity forms such as `128GB` and `128 GB` as equivalent. It does not perform fuzzy semantic matching.

## Observation schema

Each JSON or JSONL row represents what one source visibly established at one observation time:

```json
{
  "source": "ebay",
  "listing_id": "example-123",
  "url": "https://www.ebay.com/itm/example-123",
  "title": "Sony PS5 original disc console",
  "text": "Optional retrieved listing text",
  "seller_id": "public-marketplace-handle",
  "observed_at": "2026-08-15T12:00:00Z",
  "posted_at": "2026-08-12T09:30:00-04:00",
  "retrieval_method": "browser",
  "evidence_scope": "listing_card",
  "status": "offered",
  "status_basis": "platform_marker",
  "status_evidence": "Optional exact visible status marker",
  "quantity": 1,
  "condition": "used",
  "ask_price": "375.00",
  "realized_price": null,
  "currency": "USD",
  "campaign_key": "seller-handle:ps5-original-disc-1",
  "inventory_key": "serial-safe-physical-item-a"
}
```

### Required fields

- `source`: marketplace or community name.
- `listing_id`: source-native identifier, or a stable locally assigned identifier when none exists.
- `url`: canonical public HTTP(S) URL.
- `title`: retrieved listing title.
- `observed_at`: timezone-aware ISO-8601 retrieval time.
- `retrieval_method`: one of `live_page`, `browser`, `api`, `archive`, `search_index`, `search_snippet`, `other`.
- `evidence_scope`: one of `full_page`, `listing_card`, `snippet`, `title_only`.
- `status`: one of `offered`, `pending`, `sold`, `completed`, `removed`, `unknown`.
- `status_basis`: one of `platform_marker`, `seller_statement`, `buyer_statement`,
  `moderator_marker`, `search_snippet`, `archive_snapshot`, or `unspecified`.
- `quantity`: positive integer; defaults to 1.

### Price fields

- `ask_price`: seller's visible request, never a realized price.
- `realized_price`: visible completed transaction price. Omit when hidden or unknown.
- `currency`: three-letter currency code whenever a price is present.

Price statistics are grouped by currency. Never compute a median, range, or average across currencies without a separately sourced exchange-rate conversion and conversion timestamp.

Do not calculate unit prices in the observation. Preserve the package price and quantity. Explain per-unit arithmetic separately when presenting the result.

### Campaign keys and reposts

`campaign_key` explicitly joins corrections, bumps, and price-reduced reposts for the same seller and inventory. The reconciler namespaces it by source, selects the latest observation, and counts that latest quantity once.

The reconciler rejects an explicit campaign key that spans different non-empty seller identifiers. Campaign keys should include enough seller/inventory context to remain unique.

If the relationship is not clear, omit `campaign_key`; preserving two possible campaigns is safer than silently collapsing distinct inventory. Do not use title similarity alone to merge different sellers.

`inventory_key` optionally joins source campaigns that visibly advertise the same physical inventory. This is the cross-source deduplication layer. Use a privacy-safe local identifier derived from corroborating evidence; never put a full serial number in the row. If equivalence is uncertain, omit it and state that cross-posted inventory may inflate the physical-unit count. The reconciler rejects inconsistent quantities inside one explicit inventory group. Any sold/live conflict across linked source campaigns becomes unknown and preserves all signals for review; the reconciler never chooses the more optimistic state.

## Status evidence hierarchy

### Publicly confirmed sold

The latest campaign observation has:

- status `sold` or `completed`;
- `status_basis` equal to `platform_marker`;
- non-empty `status_evidence` describing the visible marker;
- `full_page` or `listing_card` evidence; and
- a retrieval method stronger than a search index/snippet.

A confirmed sale may still lack a visible realized price. Count the sale but do not invent the price.

### Seller-reported sold

A native full-page/card observation with `status_basis` equal to
`seller_statement` and explicit evidence that the seller reported completion.
Keep this separate from platform-confirmed sales and attribute it.

### Weak sold signal

A `sold` or `completed` status seen only in a snippet/search index, or lacking explicit status evidence. Report separately. Do not include its price in confirmed realized-price statistics.

### Pending

Explicit pending status. Keep separate from completed sales.

### Offered

A native listing still displaying an ask without a completed marker. Count as
offered even if old. Search snippets and title-only results are weak offered
signals and their prices belong in weak-ask statistics, not active asks.

### Unknown

Removed, deleted, unavailable, or otherwise indeterminate. Removal is not evidence of sale.

## Count invariants

Always present these layers separately:

1. **Input observations** — every retrieved row, including near-match exclusions.
2. **Matching observations** — rows that pass exact profile rules.
3. **Distinct campaigns** — reposts/corrections reconciled by explicit campaign key.
4. **Distinct inventories** — cross-source campaigns joined only by explicit inventory key.
5. **Physical units** — quantity counted once per distinct inventory.
6. **Status units** — confirmed sold, seller-reported sold, weak sold signal,
   pending, offered, weak offered signal, and unknown.

For every result:

```text
physical_units = confirmed_sold + seller_reported_sold + weak_sold + pending + offered + weak_offered + unknown
```

Absence of publicly confirmed sale evidence does not prove that no private transaction occurred.

## Provenance rules

- Preserve the canonical URL and retrieval method for every row.
- A live page supports only what was visible at observation time.
- An archive supports historical state at its snapshot time, not current availability.
- A search snippet supports only its literal text, not the inaccessible full page.
- Broad search-engine result counts are discovery hints, not listing counts.
- Never paste authentication cookies, private messages, street addresses, payment data, receipts, or full hardware serial numbers into an observation.

## CLI

```bash
python3 scripts/marketplace_intel.py reconcile \
  --profile product-profile.json \
  --observations observations.jsonl \
  --format json \
  --out report.json

python3 scripts/marketplace_intel.py reconcile \
  --profile product-profile.json \
  --observations observations.jsonl \
  --format markdown
```

Validation fails closed for malformed URLs, timezone-free timestamps, invalid quantities, invalid currencies, and non-positive prices.
