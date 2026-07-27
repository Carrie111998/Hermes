# Charterforge Circle Rail

USDC payment rail using Circle's stablecoin and CCTP for cross-chain transfers.

## Features

- Native USDC transfers on multiple chains
- Cross-chain transfers via CCTP (no wrapped assets)
- Inbound monitoring of USDC balances
- Outbound USDC transfers

## Installation

```bash
pip install charterforge-circle-rail
```

## Supported Chains

| Chain | USDC Address |
|-------|--------------|
| Ethereum Mainnet | 0xA0b8... |
| Base Mainnet | 0x8335... |
| Polygon | 0x3c49... |
| Arbitrum One | 0xaf88... |
| Optimism | 0x0b2C... |
| Avalanche C-Chain | 0xB97E... |

## Inbound Usage (Monitoring USDC)

```python
from charterforge_circle_rail import InboundCircleRail

rail = InboundCircleRail(
    monitoring_address="0x...",
    chain_id=8453  # Base
)

balance = await rail.check_balance()
print(f"USDC balance: {balance / 1_000_000}")
```

## Outbound Usage (Sending USDC)

```python
from charterforge_circle_rail import OutboundCircleRail

rail = OutboundCircleRail(
    private_key="0x...",
    chain_id=8453  # Base
)

tx_hash = await rail.send_usdc(
    to_address="0x...",
    amount=10_000_000  # 10 USDC
)
```

## Environment Variables

```bash
# Private key for outbound transfers
export CIRCLE_PRIVATE_KEY="0x..."

# Address to monitor for inbound transfers
export CIRCLE_MONITORING_ADDRESS="0x..."

# Default chain ID
export CIRCLE_CHAIN_ID="8453"
```

## References

- [Circle CCTP Docs](https://developers.circle.com/cctp)
- [USDC Contract Addresses](https://www.circle.com/en/usdc)
