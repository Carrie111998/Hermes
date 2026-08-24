---
name: coinbase-cli
description: Use Coinbase Advanced Trade through the agent-first CLI for market data, portfolios, conversions, and spot or CFM futures orders.
version: 0.1.0
author: Coinbase
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Payments, Coinbase, Crypto, Trading, x402]
    related_skills: [mpp-agent]
---

# Coinbase CLI Skill

Use `coinbase` for Coinbase Advanced Trade market data, balances, portfolios, conversions, and orders. Commands return JSON on stdout; errors return JSON on stderr.

## Safety

- Orders, conversions, transfers, and x402 payments can affect real funds. Before submitting one, state and get confirmation of the complete action: asset/product, side, amount, price or limit, fees when available, source portfolio, and maximum spend.
- Use `--dry-run` or `orders preview` before large, limit, stop, or futures orders.
- Prefer server-side `limit` and `stop_limit` orders to session-lived watchers.
- Do not read, print, or store API-key files or secrets. The CLI stores credentials in the OS keychain.
- For x402 retries, supply the same `idempotency_key`; retries without it can create another USDC hold.

## Setup

```sh
npm install -g @coinbase/coinbase-cli
coinbase env live --key-file path/to/cdp_api_key.json
coinbase balance
```

Create an ECDSA CDP API key with Coinbase App & Advanced Trade access. Enable `Trade` for orders/conversions and `Transfer` for portfolio transfers. Each key is scoped to one portfolio; register separate environments for separate portfolios.

```sh
coinbase env live-trading --key-file path/to/trading-key.json
coinbase balance -e live-trading
```

## Read market data and balances

These commands do not trade:

```sh
coinbase balance
coinbase products ticker BTC-USDC
coinbase products candles BTC-USD granularity==ONE_HOUR
coinbase products book BTC-USDC
coinbase products list --paginate
coinbase portfolios list
coinbase orders list status==OPEN
```

## Trade and manage spot orders

First confirm the product, side, and size. For market buys use `quote_size`; for market sells use `base_size`.

```sh
coinbase orders create product_id=BTC-USDC side=BUY type=market quote_size=100
coinbase orders preview product_id=BTC-USD side=BUY type=limit base_size=0.01 limit_price=50000
coinbase orders create product_id=BTC-USD side=SELL type=stop_limit base_size=0.01 limit_price=48000 stop_price=49000 stop_direction=down
coinbase orders cancel order_ids:='["<order-id>"]'
```

Use the quote currency the user specifies. If omitted, inspect balances; if both USD and USDC are available, prefer USDC. Do not silently substitute a different product.

## Convert and transfer

Quote a conversion, show the rate and fees, then get confirmation before executing:

```sh
coinbase convert quote from=USD to=USDC amount=100
coinbase convert execute <quote-id> from=USD to=USDC
coinbase transfer amount=1000 currency=USD from=<source-portfolio-id> to=<destination-portfolio-id>
```

## Futures and conditional actions

For CFM dated futures, discover a current `-CDE` product, preview the order, inspect `predicted_liquidation_price`, then confirm. Futures market orders use `base_size` only; prefer IOC limits for reliable fills.

```sh
coinbase products list product_type=FUTURE
coinbase orders preview product_id=BIT-28AUG26-CDE side=BUY type=limit base_size=1 limit_price=65000
coinbase orders create product_id=BIT-28AUG26-CDE side=BUY type=limit base_size=1 limit_price=65000 time_in_force=IOC
```

Use `--until` only for triggers native orders cannot express. Always set a timeout and chain actions with `&&` so a timeout or failure cannot trade:

```sh
coinbase products ticker BTC-USDC ETH-USDC --until "price BTC-USDC >= 65000" --until-timeout 3600 \
  && coinbase orders create product_id=ETH-USDC side=BUY type=market quote_size=50
```

## x402 resources

Discover and fetch Coinbase-curated x402 resources. `fetch` or `pay` can place a USDC hold, so confirm the resource and maximum spend first.

```sh
coinbase x402 resources q==wallet
coinbase x402 fetch resource=https://example.com/resource max_amount=1000000 input:='{"address":"0x..."}' idempotency_key="<stable-retry-key>"
```

## MCP option

For single structured operations, run the CLI as an MCP server:

```sh
coinbase mcp
```

Use the CLI for pipelines, pagination, and watchers. Discover the current command schema before unfamiliar calls:

```sh
coinbase --help
coinbase orders create --template
```
