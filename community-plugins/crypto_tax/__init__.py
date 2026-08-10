"""Crypto Tax Plugin for Hermes Agent."""

import json
from .schemas import CALCULATE_GAINS_SCHEMA, FETCH_CEX_TXS_SCHEMA, FETCH_WALLET_TXS_SCHEMA
from .tools import calculate_crypto_gains, fetch_cex_transactions, fetch_wallet_transactions


def register(ctx):
    """Register tools with Hermes Plugin Context."""

    def _calculate_gains_handler(params, **kwargs):
        if isinstance(params, str):
            params = json.loads(params)
        return json.dumps(calculate_crypto_gains(**params))

    def _fetch_cex_handler(params, **kwargs):
        if isinstance(params, str):
            params = json.loads(params)
        return json.dumps(fetch_cex_transactions(**params))

    def _fetch_wallet_handler(params, **kwargs):
        if isinstance(params, str):
            params = json.loads(params)
        return json.dumps(fetch_wallet_transactions(**params))

    ctx.register_tool(schema=CALCULATE_GAINS_SCHEMA, handler=_calculate_gains_handler)
    ctx.register_tool(schema=FETCH_CEX_TXS_SCHEMA, handler=_fetch_cex_handler)
    ctx.register_tool(schema=FETCH_WALLET_TXS_SCHEMA, handler=_fetch_wallet_handler)
