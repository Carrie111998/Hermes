"""JSON schemas for the crypto_tax Hermes plugin."""

CALCULATE_GAINS_SCHEMA = {
    "name": "calculate_crypto_gains",
    "description": (
        "Calculates capital gains/losses with FIFO/LIFO lot matching, "
        "plus ordinary income (staking, mining, airdrops). "
        "Returns a per-event disposal ledger with short/long-term classification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "description": "List of transactions (BUY, SELL, STAKING, AIRDROP, MINING).",
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp": {
                            "type": "string",
                            "description": "ISO-8601 timestamp with timezone (e.g. 2024-03-15T10:30:00Z)",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["BUY", "SELL", "STAKING", "AIRDROP", "MINING"],
                        },
                        "asset": {
                            "type": "string",
                            "description": "Ticker symbol (e.g. BTC, ETH, SOL)",
                        },
                        "amount": {"type": "number"},
                        "price_usd": {
                            "type": "number",
                            "description": "Price per unit in USD at event time",
                        },
                        "fee_usd": {"type": "number", "default": 0.0},
                    },
                    "required": ["timestamp", "type", "asset", "amount", "price_usd"],
                },
            },
            "method": {
                "type": "string",
                "enum": ["FIFO", "LIFO"],
                "default": "FIFO",
            },
            "jurisdiction": {
                "type": "string",
                "enum": ["US", "UK", "GENERIC"],
                "default": "GENERIC",
                "description": "Country rules for tax-free thresholds and rates.",
            },
            "annual_income_usd": {
                "type": "number",
                "description": "Estimated annual income for tax bracket calculation.",
            },
        },
        "required": ["transactions"],
    },
}

FETCH_WALLET_TXS_SCHEMA = {
    "name": "fetch_wallet_transactions",
    "description": (
        "Fetch on-chain transaction history for an EVM wallet address "
        "(Ethereum, Polygon, Arbitrum, Base, Optimism) via Blockscout. "
        "Returns native ETH transfers with optional ERC-20 token support. "
        "Historical prices are sourced from CoinGecko when available; "
        "falls back to Blockscout's current exchange_rate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Public wallet address (0x...)",
            },
            "chain": {
                "type": "string",
                "enum": ["ethereum", "polygon", "arbitrum", "base", "optimism"],
                "default": "ethereum",
                "description": "Target blockchain network.",
            },
            "etherscan_api_key": {
                "type": "string",
                "description": "Optional Etherscan API key (reserved; Blockscout is keyless).",
            },
            "max_pages": {
                "type": "integer",
                "default": 20,
                "description": "Maximum pages to fetch (50 transactions per page).",
            },
            "min_value_eth": {
                "type": "number",
                "default": 0.0,
                "description": "Skip native ETH transfers below this value (dust filter).",
            },
            "include_erc20": {
                "type": "boolean",
                "default": False,
                "description": "Also fetch ERC-20 token transfers for common tokens.",
            },
        },
        "required": ["address"],
    },
}

FETCH_CEX_TXS_SCHEMA = {
    "name": "fetch_cex_transactions",
    "description": (
        "Fetch spot trade history from a centralized exchange via CCXT. "
        "Supports Binance, Coinbase, Kraken, and any exchange with a CCXT driver."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "exchange_id": {
                "type": "string",
                "description": "CCXT exchange id (e.g. 'binance', 'coinbase', 'kraken').",
            },
            "api_key": {
                "type": "string",
                "description": "API key (falls back to EXCHANGE_API_KEY env var).",
            },
            "secret": {
                "type": "string",
                "description": "API secret (falls back to EXCHANGE_SECRET env var).",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Trading pairs to query (e.g. ['BTC/USDT', 'ETH/USDT']). "
                    "Defaults to BTC/USDT and ETH/USDT only."
                ),
            },
        },
        "required": ["exchange_id"],
    },
}
