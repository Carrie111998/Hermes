---
name: shopify
description: Shopify Admin & Storefront GraphQL APIs via curl. Products, orders, customers, inventory, metafields.
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [SHOPIFY_ACCESS_TOKEN, SHOPIFY_STORE_DOMAIN]
  commands: [curl, jq]
required_environment_variables:
  - name: SHOPIFY_ACCESS_TOKEN
    prompt: Shopify Admin API access token (starts with shpat_)
    help: "Shopify admin → Settings → Apps and sales channels → Develop apps → Create an app → API credentials. Token shown ONCE on install."
  - name: SHOPIFY_STORE_DOMAIN
    prompt: Your shop subdomain without protocol (e.g. my-store.myshopify.com)
    help: "The permanent myshopify.com domain, not your custom domain."
  - name: SHOPIFY_API_VERSION
    prompt: Shopify API version (default 2026-01)
    help: "Stable quarterly version. Override if you need an older one."
metadata:
  hermes:
    tags: [Shopify, E-commerce, Commerce, API, GraphQL]
    related_skills: [airtable, xurl]
    homepage: https://shopify.dev/docs/api/admin-graphql
---

# Shopify — Admin & Storefront GraphQL APIs

Work with Shopify stores directly through `curl`: list products, manage inventory, pull orders, update customers, read metafields. No SDK, no app framework — just the GraphQL endpoint and a custom-app access token.

The REST Admin API is legacy since 2024-04 and only receives security fixes. **Use GraphQL Admin** for all admin work. Use **Storefront GraphQL** for read-only customer-facing queries (products, collections, cart).

## When to use

Use this skill when the user wants to read or change data in a Shopify store: list/search/create products, adjust prices or SKUs, pull orders, look up or create customers, read/adjust inventory per location, read/write metafields, run bulk exports, register webhooks, or query the public Storefront API. It needs `SHOPIFY_ACCESS_TOKEN` + `SHOPIFY_STORE_DOMAIN` and the `curl`/`jq` commands.

## Routing table — read the reference you need

| Intent | Do this |
|---|---|
| Create the custom app, get a token, save env vars, pick Admin API scopes, Dev Dashboard change for 2026 | read `references/setup-and-auth.md` |
| Shop info, API versions, search/paginate products, get a product with variants + metafields, create products, add variants, update price/SKU | read `references/products.md` |
| List recent orders, order query filters, fetch one order with shipping address + transactions, search/create customers | read `references/orders-and-customers.md` |
| Inventory per location, adjust stock by delta, set absolute stock, read/write metafields and metaobjects | read `references/inventory-and-metafields.md` |
| Storefront GraphQL (public/private tokens), bulk operations + JSONL, webhook subscriptions + HMAC verification | read `references/storefront-bulk-webhooks.md` |

## API Basics

- **Endpoint:** `https://$SHOPIFY_STORE_DOMAIN/admin/api/$SHOPIFY_API_VERSION/graphql.json`
- **Auth header:** `X-Shopify-Access-Token: $SHOPIFY_ACCESS_TOKEN` (NOT `Authorization: Bearer`)
- **Method:** always `POST`, always `Content-Type: application/json`, body is `{"query": "...", "variables": {...}}`
- **HTTP 200 does not mean success.** GraphQL returns errors in a top-level `errors` array and per-field `userErrors`. Always check both.
- **IDs are GID strings:** `gid://shopify/Product/10079467700516`, `gid://shopify/Variant/...`, `gid://shopify/Order/...`. Pass these verbatim — don't strip the prefix.
- **Rate limit:** calculated via query cost (leaky bucket). Each response has `extensions.cost` with `requestedQueryCost`, `actualQueryCost`, `throttleStatus.{currentlyAvailable, maximumAvailable, restoreRate}`. Back off when `currentlyAvailable` drops below your next query's cost. Standard shops = 100 points bucket, 50/s restore; Plus = 1000/100.

Base curl pattern (reusable):

```bash
shop_gql() {
  local query="$1"
  local variables="${2:-{}}"
  curl -sS -X POST \
    "https://${SHOPIFY_STORE_DOMAIN}/admin/api/${SHOPIFY_API_VERSION:-2026-01}/graphql.json" \
    -H "Content-Type: application/json" \
    -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
    --data "$(jq -nc --arg q "$query" --argjson v "$variables" '{query: $q, variables: $v}')"
}
```

Pipe through `jq` for readable output. `-sS` keeps errors visible but hides the progress bar.

## Shortest end-to-end skeleton

Define `shop_gql` (above), then confirm auth and the shop identity before anything else:

```bash
shop_gql '{ shop { name myshopifyDomain primaryDomain { url } currencyCode plan { displayName } } }' | jq
```

If that returns a shop name, the token, domain, and API version are all good. Then pick the reference for the task from the routing table.

## Pitfalls

- **REST endpoints still exist but are frozen.** Don't write new integrations against `/admin/api/.../products.json`. Use GraphQL.
- **Token format check.** Admin tokens start with `shpat_`. Storefront public tokens with `shpua_`. If you have one and the wrong header, every request returns 401 without a useful error body.
- **403 with a valid token = missing scope.** Shopify returns `{"errors":[{"message":"Access denied for ..."}]}`. Re-configure Admin API scopes on the app, then reinstall to regenerate the token.
- **`userErrors` is empty != success.** Also check `data.<mutation>.<resource>` is non-null. Some failures populate neither — inspect the whole response.
- **GID vs numeric ID.** Legacy REST gave numeric IDs; GraphQL wants full GID strings. To convert: `gid://shopify/Product/<numeric>`.
- **Rate limit surprise.** A single `products(first: 250)` with deep nesting can cost 1000+ points and throttle immediately on a standard-plan shop. Start narrow, read `extensions.cost`, adjust.
- **Pagination order.** `products(first: N, reverse: true)` sorts by `id DESC`, not `created_at`. Use `sortKey: CREATED_AT, reverse: true` for "newest first."
- **`read_all_orders` for historical data.** Without it, `orders(...)` silently caps at the 60-day window. You won't get an error, just fewer results than expected. For Shopify Plus merchants with many orders, request this scope via the app's protected-data settings.
- **Currencies are strings.** Amounts come back as `"49.00"` not `49.0`. Don't `jq tonumber` blindly if you care about zero-padding.
- **Multi-currency Money fields** have `shopMoney` (store's currency) AND `presentmentMoney` (customer's). Pick one consistently.

## Safety

Mutations in Shopify are real — they create products, charge refunds, cancel orders, ship fulfillments. Before running `productDelete`, `orderCancel`, `refundCreate`, or any bulk mutation: state clearly what the change is, on which shop, and confirm with the user. There is no staging clone of production data unless the user has a separate dev store.
