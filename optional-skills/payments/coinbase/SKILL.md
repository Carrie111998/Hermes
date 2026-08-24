---
name: coinbase
description: Manage Coinbase accounts, orders, and payments.
version: 0.1.0
author: Ethan Oroshiba (ethanoroshiba), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Payments, Coinbase, Crypto, Trading, x402]
    related_skills: [mcp-oauth-remote-gateway, mpp-agent]
---

# Coinbase Skill

Use the hosted Coinbase MCP server for brokerage operations. Prefer its typed `coinbase_*` tools over terminal commands. The server handles OAuth and requests on the user's behalf; it does not fund an account or replace confirmation for fund-affecting actions.

## When to Use

- Check balances, products, fees, orders, or portfolios.
- Place, preview, modify, or cancel a Coinbase brokerage order.
- Convert assets, transfer between portfolios, or pay an x402 resource.
- Don't use for account funding; direct the user to Coinbase web or mobile.

## Prerequisites

Add the remote OAuth MCP server to `config.yaml`:

```yaml
mcp_servers:
  coinbase:
    url: "https://agents.coinbase.com/mcp"
    auth: oauth
    timeout: 180
    connect_timeout: 60
```

Reload MCP servers, complete the browser OAuth flow, and confirm Coinbase tools are available. If OAuth cannot complete on a headless gateway, use the `mcp-oauth-remote-gateway` skill.

## How to Run

Call the exposed `coinbase_*` MCP tools directly. Use the tools' schemas and responses as authoritative; do not reconstruct brokerage HTTP requests or fall back to shell commands.

## Quick Reference

- Market data: `coinbase_products_list`, `coinbase_products_get`, `coinbase_products_ticker`, `coinbase_products_book`, and `coinbase_products_candles`.
- Account data: `coinbase_balance`, `coinbase_fees`, `coinbase_portfolios_list`, and `coinbase_portfolios_get`.
- Orders: `coinbase_orders_preview`, `coinbase_orders_create`, `coinbase_orders_edit`, and `coinbase_orders_cancel`.
- Convert: `coinbase_convert_quote` → `coinbase_convert_execute` → `coinbase_convert_get`.
- For x402 resources, discover with `coinbase_x402_resources` before calling `coinbase_x402_fetch` or `coinbase_x402_pay`.

## Procedure

1. Select the correct portfolio and inspect balances or market data. Completion: the user confirms the intended asset and funding source.
2. Before any order, conversion, transfer, x402 fetch, or x402 pay, state and get confirmation of the complete action: asset/product, side, amount, price or limit, fees when available, source portfolio, and maximum spend.
3. Preview large, limit, stop, or futures orders. Completion: user approves the preview terms, including liquidation risk for futures when returned.
4. Submit the confirmed tool call. Use a stable `client_order_id` or idempotency key for retries when the tool accepts one. Completion: report its response; do not automatically fetch the order afterward.
5. For a conversion, quote first, show the rate and fees, then confirm before execution.
6. For x402, select a catalog resource, use only its advertised input schema, and confirm the maximum spend. On retries, reuse the same idempotency key if the tool supports one.

## Pitfalls

- OAuth scopes and runtime gates limit visible tools and portfolios. Ask the user to reconnect with the required Coinbase consent instead of retrying an authorization failure.
- Use the quote currency the user specifies. If omitted, inspect balances; if both USD and USDC are available, prefer USDC. Do not silently change products.
- Native limit and stop orders are durable; do not emulate them with a polling loop.
- x402 accepts only catalog resources; set `max_amount` as a spend ceiling. Never request or expose credentials, raw API keys, or payment details.

## Verification

Confirm that `coinbase_balance` returns the expected portfolio balances. A successful typed response confirms the MCP connection, OAuth authorization, and brokerage read path.
