# Payment Rails Setup Guide

Charterforge supports multiple payment rails for receiving inbound payments and sending outbound payouts.

## Available Rails

| Rail | Use Case | Geographic Coverage |
|------|----------|---------------------|
| **Stripe** | Card payments, global coverage | 135+ countries |
| **Nevermined** | Agent-to-agent USDC payments | Permissionless (anywhere USDC supported) |
| **Circle** | Native USDC, cross-chain transfers | Base, Ethereum, Polygon, Arbitrum |

## Installation

### Stripe Rail

```bash
# Install with Stripe support
pip install charterforge[stripe]
# or
uv pip install charterforge charterforge-stripe-rail
```

**Environment Variables:**

```bash
STRIPE_SECRET_KEY=sk_live_...           # Required for payments
STRIPE_WEBHOOK_SECRET=whsec_...         # Required for webhooks
```

**Verify Installation:**

```bash
python -c "from charterforge_stripe_rail import StripeRail; print('✓ Stripe rail ready')"
```

### Nevermined Rail

```bash
# Install with Nevermined support
pip install charterforge[nevermined]
# or
uv pip install charterforge charterforge-nevermined-rail
```

**Environment Variables:**

```bash
NEVERMINED_API_KEY=...                  # Nevermined API key
NEVERMINED_APP_ID=...                   # Application ID
WEB3_PRIVATE_KEY=...                    # Ethereum private key (0x...)
```

**Verify Installation:**

```bash
python -c "from charterforge_nevermined_rail import NeverminedRail; print('✓ Nevermined rail ready')"
```

### Circle Rail

```bash
# Install with Circle support
pip install charterforge[circle]
# or
uv pip install charterforge charterforge-circle-rail
```

**Environment Variables:**

```bash
CIRCLE_API_KEY=...                      # Circle API key
CIRCLE_ENTITY_SECRET=...                # Entity secret for signing
```

**Verify Installation:**

```bash
python -c "from charterforge_circle_rail import CircleRail; print('✓ Circle rail ready')"
```

## Usage

### Receiving Payments

Each rail inherits from `PaymentRail` and provides:

- `get_webhook_handler()` — Validate incoming webhook signatures
- `process_webhook(payload)` — Parse webhook events
- `validate_payment(payment_intent)` — Check payment status

**Example (Stripe):**

```python
from charterforge_stripe_rail import StripeRail

rail = StripeRail()

# Handle webhook
result = rail.get_webhook_handler().handle(
    payload=request.body,
    signature=request.headers["Stripe-Signature"]
)

if result.valid:
    print(f"Payment {result.payment_id}: {result.amount} {result.currency}")
```

### Sending Payouts

Each rail provides:

- `get_balance()` — Check available balance
- `send_payout(recipient, amount, currency)` — Initiate payout
- `get_payout_status(payout_id)` — Check payout status

**Example (Circle USDC):**

```python
from charterforge_circle_rail import CircleRail

rail = CircleRail()

# Check balance
balance = rail.get_balance()
print(f"USDC Balance: {balance}")

# Send payout
payout = rail.send_payout(
    recipient="0x1234567890abcdef...",
    amount=10.00,
    currency="USD"
)
print(f"Payout ID: {payout.id}")
```

## Integration with Charterforge Business OS

Payment rails integrate with the autonomous billing engine:

```bash
# Configure default inbound rail
charterforge config set billing.inbound_rail stripe

# Configure default outbound rail  
charterforge config set billing.outbound_rail circle

# Verify configuration
charterforge config get billing
```

The billing engine automatically:
- Tracks usage events
- Generates invoices at month-end
- Collects payments via configured rail
- Sends payouts to vendors/workers

## Security

### Webhook Verification

Always verify webhook signatures to prevent fraud:

```python
# ✓ Correct: Verify signature
handler = rail.get_webhook_handler()
result = handler.handle(payload, signature)
if not result.valid:
    raise ValueError("Invalid webhook signature")

# ✗ Incorrect: Trust payload blindly
event = json.loads(payload)  # SECURITY RISK
```

### API Key Management

- Store API keys in environment variables (never in code)
- Use restricted keys for production (Stripe: `sk_live_...`)
- Rotate keys quarterly
- Monitor key usage in provider dashboards

### Testing

Use test keys during development:

```bash
# Stripe test mode
STRIPE_SECRET_KEY=sk_test_...

# Circle sandbox
CIRCLE_API_KEY=sandbox_...
```

Test webhook handling with CLI:

```bash
# Stripe CLI (install from stripe.com/docs/stripe-cli)
stripe listen --forward-to localhost:8000/webhook/stripe
stripe trigger payment_intent.succeeded
```

## Troubleshooting

### Module Not Found

```
ModuleNotFoundError: No module named 'charterforge_stripe_rail'
```

**Solution:** Install the rail package:

```bash
pip install charterforge-stripe-rail
```

### Invalid API Key

```
stripe.error.AuthenticationError: Invalid API Key provided
```

**Solution:** Verify environment variable:

```bash
echo $STRIPE_SECRET_KEY
# Should output: sk_live_... or sk_test_...
```

### Webhook Signature Invalid

```
ValueError: Invalid webhook signature
```

**Solution:** Ensure `STRIPE_WEBHOOK_SECRET` matches endpoint configuration in Stripe Dashboard.

## Next Steps

- [Billing Engine Guide](./billing-engine.md)
- [Multi-Tenant Payments](./multi-tenant-payments.md)
- [Payment Rail Plugin Development](./payment-rail-plugin.md)
