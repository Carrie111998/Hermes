# Hermes Crypto Tax Plugin

[![Version](https://img.shields.io/badge/version-1.2.0-blue)](plugin.yaml)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-8A2BE2)](https://github.com/NousResearch/hermes-agent)

A Hermes Agent plugin for crypto tax calculations: fetch on-chain wallet transactions and CEX trade history, then compute capital gains with FIFO/LIFO lot matching.

## Features

- **On-chain wallet scanning** — Fetch native ETH transfers + ERC-20 token transfers for any EVM address via Blockscout (no API key required)
- **CEX trade history** — Pull spot trades from Binance, Coinbase, Kraken, and 100+ exchanges via CCXT
- **FIFO/LIFO lot matching** — Proper cost-basis tracking with short/long-term classification
- **Historical prices** — CoinGecko daily price lookup for accurate cost basis at transaction time
- **Income classification** — Separate staking, mining, and airdrop income from capital gains
- **Multi-chain** — Ethereum, Polygon, Arbitrum, Base, Optimism

## Installation

```bash
# Copy to your Hermes plugins directory
cp -r hermes-crypto-tax-plugin ~/.hermes/plugins/crypto_tax

# Enable the plugin
hermes plugins enable crypto_tax

# Install Python dependencies
pip install --user ccxt requests
```

## Tools

### `fetch_wallet_transactions`

Fetch on-chain transaction history for an EVM wallet.

| Param | Type | Default | Description |
|---|---|---|---|
| `address` | string | *(required)* | Wallet address (0x...) |
| `chain` | string | `"ethereum"` | Network: ethereum, polygon, arbitrum, base, optimism |
| `max_pages` | int | `20` | Pages to fetch (50 txs/page) |
| `min_value_eth` | float | `0.0` | Skip transfers below this ETH value (dust filter) |
| `include_erc20` | bool | `false` | Also fetch ERC-20 token transfers |

### `fetch_cex_transactions`

Fetch spot trade history from a centralized exchange.

| Param | Type | Default | Description |
|---|---|---|---|
| `exchange_id` | string | *(required)* | CCXT exchange id (`"binance"`, `"coinbase"`, etc.) |
| `api_key` | string | `EXCHANGE_API_KEY` env | API key |
| `secret` | string | `EXCHANGE_SECRET` env | API secret |
| `symbols` | list | `["BTC/USDT","ETH/USDT"]` | Trading pairs to query |

### `calculate_crypto_gains`

Compute capital gains/losses with FIFO/LIFO lot matching.

| Param | Type | Default | Description |
|---|---|---|---|
| `transactions` | list | *(required)* | Transaction list from fetch tools |
| `method` | string | `"FIFO"` | Lot matching: `"FIFO"` or `"LIFO"` |
| `jurisdiction` | string | `"GENERIC"` | Tax jurisdiction: `"US"`, `"UK"`, `"GENERIC"` |
| `annual_income_usd` | float | `0.0` | For bracket estimation |

Returns `{ capital_gains_summary, ordinary_income_summary, sales_detail }`.

## Usage

```python
from tools import fetch_wallet_transactions, calculate_crypto_gains

# Fetch wallet history
result = fetch_wallet_transactions(
    address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    chain="ethereum",
    max_pages=30,
    min_value_eth=0.0001,
    include_erc20=True,
)

# Calculate gains
tax = calculate_crypto_gains(
    result["transactions"],
    method="FIFO",
    jurisdiction="US",
    annual_income_usd=80000,
)

print(tax["capital_gains_summary"])
# {
#   "total_realized_gain_loss_usd": 12500.00,
#   "short_term_gain_loss_usd": 8000.00,
#   "long_term_gain_loss_usd": 4500.00
# }

for disposal in tax["sales_detail"]:
    print(f"{disposal['sell_date'][:10]} {disposal['asset']} "
          f"gain=${disposal['gain_loss']:.2f} "
          f"{'LTCG' if disposal['long_term'] else 'STCG'}")
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `EXCHANGE_API_KEY` | For CEX | CCXT exchange API key |
| `EXCHANGE_SECRET` | For CEX | CCXT exchange API secret |
| `ETHERSCAN_API_KEY` | Optional | Higher rate limits (Blockscout is keyless by default) |

## Caveats

- **Wallet transactions are simplistic** — all outgoing ETH is classified as "SELL" (taxable disposition). Wallet-to-wallet transfers, bridge transactions, and contract interactions are NOT real sales but are classified as such.
- **No DEX swap detection** — only native ETH transfers and ERC-20 token transfers. No internal transaction tracing.
- **Unmatched disposals** — when a sell has no matching buy lots, cost basis is $0 and the full proceeds are treated as short-term gain.
- **Fees not deducted** — transaction fees are recorded but do not reduce sale proceeds.
- **ERC-20 prices are approximate** — token prices use ETH's daily price as a proxy. For accurate token pricing, provide price data externally.

## License

MIT — see [Hermes Agent](https://github.com/NousResearch/hermes-agent) for details.
