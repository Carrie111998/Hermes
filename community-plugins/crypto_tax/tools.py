import os
import requests
from datetime import datetime, timezone
from collections import defaultdict
import ccxt


# ── Historical price cache (CoinGecko) ──────────────────────────────────

def _fetch_historical_prices(dates: set) -> dict:
    """Fetch ETH daily prices from CoinGecko for a set of YYYY-MM-DD strings.
    Returns {date_str: price_usd}. Falls back gracefully on failure."""
    if not dates:
        return {}
    sorted_dates = sorted(dates)
    earliest, latest = sorted_dates[0], sorted_dates[-1]
    try:
        from_ts = int(datetime.strptime(earliest, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp())
        to_ts = int(datetime.strptime(latest, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp()) + 86400
        url = (
            "https://api.coingecko.com/api/v3/coins/ethereum/market_chart/range"
            f"?vs_currency=usd&from={from_ts}&to={to_ts}"
        )
        resp = requests.get(url, headers={"accept": "application/json"}, timeout=30)
        if resp.status_code != 200:
            return {}
        prices = resp.json().get("prices", [])
        result = {}
        for ms_ts, price in prices:
            d = datetime.fromtimestamp(ms_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            result[d] = price
        return result
    except Exception:
        return {}


# ── Wallet transaction fetching ─────────────────────────────────────────

BLOCKSCOUT_DOMAINS = {
    "ethereum": "eth.blockscout.com",
    "polygon": "polygon.blockscout.com",
    "arbitrum": "arbitrum.blockscout.com",
    "base": "base.blockscout.com",
    "optimism": "optimism.blockscout.com",
}

# ERC-20 tokens to track (by contract address, Ethereum mainnet)
TRACKED_TOKENS = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "LINK",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
    "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce": "SHIB",
}


def fetch_wallet_transactions(
    address: str,
    chain: str = "ethereum",
    etherscan_api_key: str = None,
    max_pages: int = 20,
    min_value_eth: float = 0.0,
    include_erc20: bool = False,
) -> dict:
    """Fetch native ETH and optional ERC-20 transactions for an EVM wallet.

    Uses Blockscout open APIs (no key required). Paginates up to max_pages
    (50 txs/page). Optionally fetches ERC-20 token transfers for common tokens.

    Args:
        address: Wallet address (0x...).
        chain: One of ethereum, polygon, arbitrum, base, optimism.
        etherscan_api_key: Optional Etherscan key (reserved, not used for Blockscout).
        max_pages: Max pages to fetch (50 txs/page). Default 20 = 1000 txs.
        min_value_eth: Skip native transfers below this ETH value.
        include_erc20: If True, also fetch ERC-20 token transfers.

    Returns:
        {"address", "chain", "total_fetched", "transactions": [...]}
        Each tx: {timestamp, type, asset, amount, price_usd, fee_usd}
    """
    domain = BLOCKSCOUT_DOMAINS.get(chain.lower(), "eth.blockscout.com")
    base_url = f"https://{domain}/api/v2/addresses/{address}/transactions"

    all_raw = []
    next_params = None

    for _ in range(max_pages):
        url = base_url
        if next_params:
            qs = "&".join(f"{k}={v}" for k, v in next_params.items())
            url += f"?{qs}"
        try:
            resp = requests.get(url, headers={"accept": "application/json"}, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("items", [])
            all_raw.extend(items)
            next_params = data.get("next_page_params")
            if not next_params or not items:
                break
        except Exception:
            break

    # Collect all unique dates for historical price lookup
    dates_needed = set()
    user_addr = address.lower()
    normalized = []

    for tx in all_raw:
        ts = tx.get("timestamp")
        if not ts:
            continue

        from_info = tx.get("from") or {}
        to_info = tx.get("to") or {}
        from_addr = (from_info.get("hash") or "").lower()
        to_addr = (to_info.get("hash") or "").lower()

        value_wei = float(tx.get("value", 0))
        value_eth = value_wei / 1e18
        if value_eth < min_value_eth:
            continue

        date_str = ts[:10]
        dates_needed.add(date_str)

        # Classification: outgoing ETH sent by user is a potential disposal
        if from_addr == user_addr:
            tx_type = "SELL"
        else:
            tx_type = "BUY"

        price_usd = float(tx.get("exchange_rate", 0))

        fee_data = tx.get("fee") or {}
        fee_wei = float(fee_data.get("value", 0))
        fee_eth = fee_wei / 1e18
        fee_usd = fee_eth * price_usd if price_usd else 0.0

        normalized.append({
            "timestamp": ts,
            "type": tx_type,
            "asset": "ETH",
            "amount": round(value_eth, 18),
            "price_usd": round(price_usd, 2),
            "fee_usd": round(fee_usd, 4),
        })

    # Override prices with CoinGecko historical data when available
    cg_prices = _fetch_historical_prices(dates_needed)
    if cg_prices:
        for tx in normalized:
            d = tx["timestamp"][:10]
            if d in cg_prices:
                tx["price_usd"] = round(cg_prices[d], 2)

    # ERC-20 token transfers (optional)
    if include_erc20 and chain.lower() == "ethereum":
        erc20_txs = _fetch_erc20_transfers(address, domain, cg_prices, min_value_eth)
        normalized.extend(erc20_txs)

    return {
        "address": address,
        "chain": chain,
        "total_fetched": len(normalized),
        "transactions": normalized,
    }


def _fetch_erc20_transfers(
    address: str, domain: str, cg_prices: dict, min_value: float
) -> list:
    """Fetch ERC-20 token transfers for tracked tokens via Blockscout."""
    results = []
    base_url = f"https://{domain}/api/v2/addresses/{address}/token-transfers"

    params = {"type": "ERC-20"}
    try:
        resp = requests.get(
            base_url, params=params,
            headers={"accept": "application/json"}, timeout=15,
        )
        if resp.status_code != 200:
            return results
        items = resp.json().get("items", [])
    except Exception:
        return results

    user_addr = address.lower()
    for item in items:
        token = item.get("token") or {}
        token_addr = (token.get("address") or "").lower()
        symbol = TRACKED_TOKENS.get(token_addr, token.get("symbol", "UNKNOWN"))
        decimals = int(token.get("decimals", 18))

        from_addr = (item.get("from") or {}).get("hash", "").lower()
        to_addr = (item.get("to") or {}).get("hash", "").lower()

        raw_amount = float(item.get("total", {}).get("value", 0))
        amount = raw_amount / (10 ** decimals)

        ts = item.get("timestamp")
        if not ts or amount <= 0:
            continue

        if from_addr == user_addr:
            tx_type = "SELL"
        elif to_addr == user_addr:
            tx_type = "BUY"
        else:
            continue

        # Use ETH price as proxy for token price (rough)
        date_str = ts[:10]
        price_usd = cg_prices.get(date_str, 0.0)

        results.append({
            "timestamp": ts,
            "type": tx_type,
            "asset": symbol,
            "amount": round(amount, 18),
            "price_usd": round(price_usd, 2),
            "fee_usd": 0.0,
        })

    return results


# ── CEX transaction fetching ────────────────────────────────────────────

def fetch_cex_transactions(
    exchange_id: str,
    api_key: str = None,
    secret: str = None,
    symbols: list = None,
) -> dict:
    """Fetch spot trade history from a CEX via CCXT.

    Args:
        exchange_id: CCXT exchange id ("binance", "coinbase", etc.).
        api_key: API key (falls back to EXCHANGE_API_KEY env var).
        secret: API secret (falls back to EXCHANGE_SECRET env var).
        symbols: Trading pairs to query. Defaults to ["BTC/USDT", "ETH/USDT"].

    Returns:
        {"exchange", "total_fetched", "transactions": [...]}
    """
    exchange_id = exchange_id.lower().strip()
    if not hasattr(ccxt, exchange_id):
        return {"error": f"Exchange '{exchange_id}' is not supported by CCXT."}

    key = api_key or os.getenv("EXCHANGE_API_KEY")
    sec = secret or os.getenv("EXCHANGE_SECRET")
    if not key or not sec:
        return {"error": f"Missing API credentials for {exchange_id}."}

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        "apiKey": key, "secret": sec, "enableRateLimit": True,
    })
    target_symbols = symbols or ["BTC/USDT", "ETH/USDT"]
    normalized_txs = []

    try:
        for symbol in target_symbols:
            if exchange.has.get("fetchMyTrades"):
                try:
                    trades = exchange.fetch_my_trades(symbol)
                except Exception:
                    continue
                for trade in trades:
                    ts = trade.get("timestamp")
                    if not ts:
                        continue
                    fee_cost = 0.0
                    fee_info = trade.get("fee") or {}
                    if fee_info.get("cost") is not None:
                        fee_cost = float(fee_info["cost"])
                    normalized_txs.append({
                        "timestamp": datetime.fromtimestamp(
                            ts / 1000, tz=timezone.utc
                        ).isoformat(),
                        "type": str(trade.get("side", "")).upper(),
                        "asset": symbol.split("/")[0],
                        "amount": float(trade.get("amount", 0)),
                        "price_usd": float(trade.get("price", 0)),
                        "fee_usd": fee_cost,
                    })
    except Exception as e:
        return {"error": f"Failed to fetch trades from {exchange_id}: {str(e)}"}

    return {
        "exchange": exchange_id,
        "total_fetched": len(normalized_txs),
        "transactions": normalized_txs,
    }


# ── Capital gains calculation ───────────────────────────────────────────

def calculate_crypto_gains(
    transactions: list,
    method: str = "FIFO",
    jurisdiction: str = "GENERIC",
    annual_income_usd: float = 0.0,
) -> dict:
    """Calculate capital gains/losses with FIFO/LIFO lot matching.

    Separates ordinary income events (STAKING, AIRDROP, MINING) from
    trade disposals (BUY/SELL). Unmatched disposals (no acquisition
    lots) are classified as short-term with zero cost basis.

    Args:
        transactions: List of dicts with {timestamp, type, asset, amount,
                      price_usd, fee_usd}.
        method: "FIFO" (default) or "LIFO".
        jurisdiction: "US", "UK", or "GENERIC".
        annual_income_usd: User's estimated annual income for bracket
                           estimation (used by downstream analysis).

    Returns:
        {jurisdiction, accounting_method, capital_gains_summary,
         ordinary_income_summary, sales_detail}
    """
    total_staking_income = 0.0
    total_airdrop_income = 0.0
    total_mining_income = 0.0
    trade_transactions = []

    # 1. Separate income events from trade events
    for tx in transactions:
        tx_type = str(tx.get("type", "BUY")).upper()
        usd_val = float(tx.get("amount", 0)) * float(tx.get("price_usd", 0))
        if tx_type == "STAKING":
            total_staking_income += usd_val
        elif tx_type == "AIRDROP":
            total_airdrop_income += usd_val
        elif tx_type == "MINING":
            total_mining_income += usd_val
        else:
            trade_transactions.append(tx)

    # 2. Process capital gains with lot matching
    sorted_txs = sorted(trade_transactions, key=lambda x: x["timestamp"])
    buy_lots = defaultdict(list)

    total_realized_gain = 0.0
    short_term_gain = 0.0
    long_term_gain = 0.0
    processed_sales = []

    for tx in sorted_txs:
        asset = tx["asset"].upper()
        tx_type = tx["type"].upper()
        amount = float(tx["amount"])
        price_usd = float(tx["price_usd"])
        fee_usd = float(tx.get("fee_usd", 0.0))
        tx_time = datetime.fromisoformat(tx["timestamp"].replace("Z", "+00:00"))

        if tx_type == "BUY":
            effective_price = price_usd + (fee_usd / amount if amount > 0 else 0)
            buy_lots[asset].append({
                "amount": amount,
                "price": effective_price,
                "timestamp": tx_time,
            })

        elif tx_type == "SELL":
            remaining_to_sell = amount
            sell_proceeds = (amount * price_usd) - fee_usd
            cost_basis_total = 0.0
            lots_used = []

            lots = buy_lots[asset]
            if method.upper() == "LIFO":
                lots.reverse()

            idx = 0
            while remaining_to_sell > 1e-12 and idx < len(lots):
                lot = lots[idx]
                take_amount = min(remaining_to_sell, lot["amount"])
                cost = take_amount * lot["price"]
                cost_basis_total += cost

                holding_days = (tx_time - lot["timestamp"]).days
                is_long_term = holding_days > 365

                lots_used.append({
                    "amount": take_amount,
                    "buy_price": lot["price"],
                    "buy_date": lot["timestamp"].isoformat(),
                    "holding_days": holding_days,
                    "term": "LONG" if is_long_term else "SHORT",
                })

                lot["amount"] -= take_amount
                remaining_to_sell -= take_amount

                if lot["amount"] < 1e-12:
                    lots.pop(idx)
                    if method.upper() == "LIFO":
                        continue
                else:
                    idx += 1

            if method.upper() == "LIFO":
                lots.reverse()

            gain_loss = sell_proceeds - cost_basis_total
            total_realized_gain += gain_loss

            # --- FIXED: ST/LT split accounts for matched AND unmatched portions ---
            sale_st_gain = 0.0
            sale_lt_gain = 0.0
            matched_amount = 0.0

            for lot_info in lots_used:
                lot_ratio = lot_info["amount"] / amount if amount > 0 else 0
                lot_gain = gain_loss * lot_ratio
                matched_amount += lot_info["amount"]
                if lot_info["term"] == "LONG":
                    sale_lt_gain += lot_gain
                else:
                    sale_st_gain += lot_gain

            # Unmatched portion: no acquisition lot → no holding proof → short-term
            if remaining_to_sell > 1e-12 and amount > 0:
                unmatched_ratio = remaining_to_sell / amount
                unmatched_gain = gain_loss * unmatched_ratio
                sale_st_gain += unmatched_gain

            short_term_gain += sale_st_gain
            long_term_gain += sale_lt_gain

            # Determine overall term: LT only if ALL matched lots are LT
            # and there is no unmatched portion
            all_long_term = (
                remaining_to_sell < 1e-12
                and len(lots_used) > 0
                and all(li["term"] == "LONG" for li in lots_used)
            )

            processed_sales.append({
                "asset": asset,
                "sell_date": tx["timestamp"],
                "sold_amount": amount,
                "sell_price_usd": price_usd,
                "proceeds": round(sell_proceeds, 2),
                "cost_basis": round(cost_basis_total, 2),
                "gain_loss": round(gain_loss, 2),
                "long_term": all_long_term,
            })

    total_income = total_staking_income + total_airdrop_income + total_mining_income

    return {
        "jurisdiction": jurisdiction,
        "accounting_method": method,
        "capital_gains_summary": {
            "total_realized_gain_loss_usd": round(total_realized_gain, 2),
            "short_term_gain_loss_usd": round(short_term_gain, 2),
            "long_term_gain_loss_usd": round(long_term_gain, 2),
        },
        "ordinary_income_summary": {
            "total_income_usd": round(total_income, 2),
            "staking_usd": round(total_staking_income, 2),
            "airdrops_usd": round(total_airdrop_income, 2),
            "mining_usd": round(total_mining_income, 2),
        },
        "sales_detail": processed_sales,
    }
