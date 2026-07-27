# Charterforge Nevermined Rail

AI agent-to-agent payment rail using the Nevermined protocol.

## Features

- **Inbound Rails**: Validate payments for services Charterforge provides
- **Outbound Rails**: Pay for external agent services autonomously
- **USDC Settlement**: Permissionless stablecoin payments
- **Credit Metering**: Usage-based access control

## Installation

```bash
pip install charterforge-nevermined-rail
```

## Configuration

Set environment variables:

```bash
# Nevermined API key (sandbox or live)
export NVM_API_KEY="sandbox:your-api-key"

# Agent ID (from registration)
export NVM_AGENT_ID="did:nv:..."

# Payment plan ID
export NVM_PLAN_ID="0x..."

# For outbound: subscriber wallet address
export NVM_SUBSCRIBER_ADDRESS="0x..."
```

## Inbound Usage (Receiving Payments)

```python
from charterforge_nevermined_rail import InboundNeverminedRail

rail = InboundNeverminedRail()

# Validate incoming request has valid payment
validation = await rail.validate_payment(access_token)
if validation["is_valid"]:
    # Serve request
    print(f"Credits remaining: {validation['credits_remaining']}")
else:
    raise HTTPException(402, "Payment required")
```

## Outbound Usage (Paying for Services)

```python
from charterforge_nevermined_rail import OutboundNeverminedRail

rail = OutboundNeverminedRail()

# Order a plan and get access token
order = await rail.order_plan(
    plan_id="0x...",
    amount=10_000_000  # 10 USDC
)
access_token = order["access_token"]

# Call paid agent API
response = await rail.call_agent(
    endpoint="https://api.example.com/query",
    access_token=access_token,
    payload={"prompt": "Hello"}
)
```

## Entry Points

This package registers:

- `charterforge.inbound_payment_rails.nevermined` → `InboundNeverminedRail`
- `charterforge.outbound_payment_rails.nevermined` → `OutboundNeverminedRail`

## References

- [Nevermined Docs](https://nevermined.ai/docs)
- [Python SDK](https://pypi.org/project/payments-py/)
- [Charterforge Payment Rails Guide](/docs/payment-rails-research.md)
