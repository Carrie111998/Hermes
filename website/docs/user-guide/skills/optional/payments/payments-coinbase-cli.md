---
title: "Coinbase Cli — Trade, manage portfolios, and pay x402 resources"
sidebar_label: "Coinbase Cli"
description: "Trade, manage portfolios, and pay x402 resources"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Coinbase Cli

Trade, manage portfolios, and pay x402 resources.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/payments/coinbase-cli` |
| Path | `optional-skills/payments/coinbase-cli` |
| Version | `0.1.0` |
| Author | Ethan Oroshiba (ethanoroshiba), Hermes Agent |
| License | MIT |
| Platforms | linux, macos |
| Tags | `Payments`, `Coinbase`, `Crypto`, `Trading`, `x402` |
| Related skills | [`mpp-agent`](/docs/user-guide/skills/optional/payments/payments-mpp-agent) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Coinbase CLI Skill

Use `coinbase` through `terminal` for Coinbase Advanced Trade market data, balances, portfolios, conversions, and orders. It returns JSON on stdout; errors return JSON on stderr. It does not fund an account or replace user confirmation for fund-affecting actions.

## When to Use

- Check prices, balances, orders, portfolios, or available products.
- Buy, sell, convert, transfer, or manage Coinbase Advanced Trade positions.
- Fetch a curated x402 resource or wait for a cross-product condition.
- Don't use for funding an account; direct the user to Coinbase web or mobile.

## Prerequisites

- Install Node.js 22+ and the CLI:

```sh
npm install -g @coinbase/coinbase-cli
```

- Create an ECDSA CDP API key with Coinbase App & Advanced Trade access. Enable `Trade` for orders/conversions and `Transfer` for transfers. Each key is scoped to one portfolio.
- Configure the key without printing or reading its contents:

```sh
coinbase env live --key-file path/to/cdp_api_key.json
coinbase balance
```

Register separate environments for separate portfolios:

```sh
coinbase env live-trading --key-file path/to/trading-key.json
coinbase balance -e live-trading
```

## How to Run

Invoke commands with `terminal`. Use `coinbase --help` before unfamiliar commands and `coinbase orders create --template` to inspect an order schema.

## Quick Reference

Read-only:

```sh
coinbase balance
coinbase products ticker BTC-USDC
coinbase products candles BTC-USD granularity==ONE_HOUR
coinbase products book BTC-USDC
coinbase products list --paginate
coinbase portfolios list
coinbase orders list status==OPEN
```

```sh
# Market buy uses quote_size; market sell uses base_size.
coinbase orders create product_id=BTC-USDC side=BUY type=market quote_size=100
coinbase orders preview product_id=BTC-USD side=BUY type=limit base_size=0.01 limit_price=50000
coinbase orders create product_id=BTC-USD side=SELL type=stop_limit base_size=0.01 limit_price=48000 stop_price=49000 stop_direction=down
coinbase orders cancel order_ids:='["<order-id>"]'
```

Use the quote currency the user specifies. If omitted, inspect balances; if both USD and USDC are available, prefer USDC. Do not silently substitute a different product.

Conversions and transfers:

```sh
coinbase convert quote from=USD to=USDC amount=100
coinbase convert execute <quote-id> from=USD to=USDC
coinbase transfer amount=1000 currency=USD from=<source-portfolio-id> to=<destination-portfolio-id>
```

## Procedure

1. Verify the active environment with `coinbase balance`. Completion: expected portfolio balances return.
2. For a fund-affecting action, state and get confirmation of the complete action: asset/product, side, amount, price or limit, fees when available, source portfolio, and maximum spend.
3. For large, limit, stop, or futures orders, run `orders preview` or `--dry-run`. Completion: user approves the resolved terms.
4. Submit the confirmed action. Completion: report the create response; do not automatically fetch the order.
5. For conversions, quote first, show rate/fees, then confirm before executing:

```sh
coinbase convert quote from=USD to=USDC amount=100
coinbase convert execute <quote-id> from=USD to=USDC
```

6. For CFM dated futures, discover a current `-CDE` product, preview it, inspect `predicted_liquidation_price`, then confirm. Futures market orders use `base_size` only; prefer IOC limits:

```sh
coinbase products list product_type=FUTURE
coinbase orders preview product_id=BIT-28AUG26-CDE side=BUY type=limit base_size=1 limit_price=65000
coinbase orders create product_id=BIT-28AUG26-CDE side=BUY type=limit base_size=1 limit_price=65000 time_in_force=IOC
```

7. Use `--until` only for triggers native orders cannot express. Always set a timeout and chain with `&&`; timeout or failure must not trade:

```sh
coinbase products ticker BTC-USDC ETH-USDC --until "price BTC-USDC >= 65000" --until-timeout 3600 \
  && coinbase orders create product_id=ETH-USDC side=BUY type=market quote_size=50
```

8. For x402, confirm the resource and maximum spend before fetching. On retry, supply the same `idempotency_key` to prevent an additional USDC hold:

```sh
coinbase x402 resources q==wallet
coinbase x402 fetch resource=https://example.com/resource max_amount=1000000 input:='{"address":"0x..."}' idempotency_key="<stable-retry-key>"
```

## Pitfalls

- Use the quote currency the user specifies. If omitted, inspect balances; if both USD and USDC are available, prefer USDC. Do not silently change products.
- Prefer server-side `limit` and `stop_limit` to a session-lived watcher. A stopped watcher loses its trigger.
- API-key files and credentials must never enter agent context. The CLI stores credentials in the OS keychain.
- An x402 fetch or pay can place a USDC hold.

## Verification

```sh
coinbase --version
coinbase balance
```

Exit code 0 confirms the CLI is installed and the configured environment can read balances.
