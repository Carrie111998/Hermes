# Shopify — Setup, Custom App, and Scopes

1. In Shopify admin: **Settings → Apps and sales channels → Develop apps → Create an app**.
2. Click **Configure Admin API scopes**, select what you need (examples below), save.
3. **Install app** → the Admin API access token appears ONCE. Copy it immediately — Shopify will never show it again. Tokens start with `shpat_`.
4. Save to `${HERMES_HOME:-~/.hermes}/.env`:
   ```
   SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxx
   SHOPIFY_STORE_DOMAIN=my-store.myshopify.com
   SHOPIFY_API_VERSION=2026-01
   ```

> **Heads up:** As of January 1, 2026, new "legacy custom apps" created in the Shopify admin are gone. New setups should use the **Dev Dashboard** (`shopify.dev/docs/apps/build/dev-dashboard`). Existing admin-created apps keep working. If the user's shop has no existing custom app and it's after 2026-01-01, direct them to Dev Dashboard instead of the admin flow.

Common scopes by task:
- Products / collections: `read_products`, `write_products`
- Inventory: `read_inventory`, `write_inventory`, `read_locations`
- Orders: `read_orders`, `write_orders` (30 most recent without `read_all_orders`)
- Customers: `read_customers`, `write_customers`
- Draft orders: `read_draft_orders`, `write_draft_orders`
- Fulfillments: `read_fulfillments`, `write_fulfillments`
- Metafields / metaobjects: covered by the matching resource scopes

