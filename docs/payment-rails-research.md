# Payment Rails Research for Agentic Business OS

## Executive Summary

For an autonomous business OS that needs to receive funding and make payments,
six payment rail categories are viable in 2026. The choice depends on:

1. **Geographic availability** (user's location)
2. **Direction** (receiving vs. sending payments)
3. **Agentic compatibility** (API/webhook support for autonomous operation)
4. **Settlement speed** (instant vs. T+2)
5. **Custodial vs. non-custodial** (holds funds or passes through)

---

## 1. Traditional Processors (Global Coverage)

### Stripe
- **Availability**: 46+ countries with local acquiring, 195+ countries for cross-border
- **Agentic Support**:
  - Webhook-based event delivery (`checkout.session.completed`, `payment_intent.*`)
  - REST API for payment creation, refund, dispute handling
  - Usage billing and subscription support
  - 2026: Added stablecoin support (USDC on Ethereum, Solana)
  - 2026: "Tempo" streaming payments, "Metronome" agent billing (announced Sessions 2026)
- **Direction**: Receive + Send (via Stripe Connect)
- **Settlement**: T+2 standard, instant for stablecoin
- **Custody**: Custodial (holds merchant balance)
- **Webhook Auth**: HMAC-SHA256 signature verification
- **Entry Points**: `charterforge.inbound_payment_rails`, `charterforge.outbound_payment_rails`
- **Plugin**: `packages/charterforge-stripe-rail`

### PayPal / Braintree
- **Availability**: 200+ countries, 400M+ consumer accounts
- **Agentic Support**:
  - PayPal REST API with OAuth2 authentication
  - Webhooks for payment events
  - Braintree SDK for card/vault processing
- **Direction**: Receive + Send (Payouts API)
- **Settlement**: T+1 to bank, instant PayPal balance
- **Custody**: Custodial
- **Strengths**: Strong consumer trust, PayPal as payment method
- **Weaknesses**: Higher fees, FX markup

### Adyen
- **Availability**: Global enterprise processor, strong Europe/Asia
- **Agentic Support**:
  - REST API with webhook notifications
  - Single integration for cards, wallets, local methods
- **Direction**: Receive + Send (payouts)
- **Settlement**: T+1 to T+3
- **Custody**: Custodial, acquiring license in many jurisdictions
- **Best For**: High-volume enterprise, multi-local acquiring

---

## 2. Cross-Border / Transfer Services

### Wise (formerly TransferWise)
- **Availability**: Global, 160+ countries
- **Agentic Support**:
  - Wise Platform API for transfers, balance, direct debits
  - Webhook notifications
  - Real-time FX at mid-market rate
- **Direction**: Primarily Send (payouts, payroll), some Receive
- **Settlement**: Same-day to instant
- **Custody**: Transit (funds move through Wise balance)
- **Strengths**: Best FX rates, fast settlement
- **Use Cases**: Payroll, contractor payments, cross-border B2B

### Payoneer
- **Availability**: 190+ countries, strong for freelancers/B2B
- **Agentic Support**:
  - Mass Payouts API for batch payments
  - Business API for receiving account balances
  - Webhook events
- **Direction**: Receive + Send
- **Settlement**: T+2 to bank
- **Custody**: Custodial (receiving accounts)
- **Strengths**: Multi-currency receiving accounts (USD, EUR, GBP, JPY, etc.)
- **Use Cases**: Marketplace payouts, freelancer payments, B2B invoicing

---

## 3. Stablecoin / Crypto-Native

### Nevermined (AI-Native)
- **Availability**: Global, crypto rails
- **Agentic Support**:
  - Native agent-to-agent payments ("PayPal for AI Commerce")
  - Universal Agent ID for persistent identity
  - Tamper-proof third-party metering
  - Instant settlement via crypto or fiat rails
  - Usage-based, outcome-based pricing models
  - Supports Google A2A protocol, Model Context Protocol
- **Direction**: Receive + Send
- **Settlement**: Instant (seconds)
- **Custody**: Non-custodial (user-controlled wallets)
- **Strengths**: Purpose-built for autonomous agents, instant settlement, trust infrastructure
- **Funding**: $4M raised, backing from notable AI investors
- **Use Cases**: AI agent commerce, agent marketplaces, autonomous services

### Circle (USDC Issuer)
- **Availability**: Global (USDC holders)
- **Agentic Support**:
  - CCTP (Cross-Chain Transfer Protocol) for native cross-chain USDC
  - Gateway API for unified multi-chain balance
  - Programmable wallets API
  - No API key required for CCTP/Gateway (permissionless)
- **Direction**: Receive + Send
- **Settlement**: Sub-500ms for Gateway, instant on-chain
- **Custody**: Non-custodial
- **Strengths**: Direct USDC issuer, multi-chain support, no bridge risk
- **Weaknesses**: USDC/EURC only, no fiat ramp included

### Crossmint
- **Availability**: Global (50+ chains, 150+ countries for fiat ramps)
- **Agentic Support**:
  - Full-stack stablecoin orchestration
  - Programmable wallets for agents
  - Onramp/offramp (fiat ↔ USDC/USDT)
  - Built-in KYC/AML/travel rule compliance
  - Agentic finance solution
- **Direction**: Receive + Send
- **Settlement**: Instant settlement, near-instant onramp
- **Custody**: Custodial or non-custodial
- **Strengths**: All-in-one stack, compliance built-in, 50+ chains
- **Use Cases**: Fintech apps, agentic platforms, remittances

### Stripe Stablecoin (via Bridge)
- **Availability**: Where Stripe operates, Ethereum/Solana
- **Agentic Support**:
  - Add `crypto` payment method to existing Stripe integration
  - USDC/USDB support
  - Financial Accounts for stablecoin balance
  - USDC subscriptions and payouts
- **Direction**: Receive + Send
- **Settlement**: Instant on-chain, T+2 fiat conversion
- **Custody**: Custodial (Bridge subsidiary)
- **Strengths**: Incremental add to existing Stripe stack
- **Weaknesses**: Limited to Ethereum/Solana, USDC/USDB only

---

## 4. Regional Specialists

### Checkout.com (EMEA)
- **Availability**: Strong Europe, Middle East
- **Agentic Support**: REST API, webhook events
- **Strengths**: Local payment methods, competitive for mid-market
- **Use Case**: European expansion

### Airwallex (APAC/Europe)
- **Availability**: Asia-Pacific, Europe, UK
- **Agentic Support**: API, webhook, multi-currency accounts
- **Strengths**: Multi-currency IBANs, FX at mid-market
- **Use Case**: APAC/Europe cross-border

---

## 5. Agentic-Native Alternatives

### Paid.ai
- **Focus**: AI cost tracking, margin monitoring
- **Agentic Support**: Tracks AI model costs, prevents unprofitable transactions
- **Direction**: Billing intelligence (not payment processing)
- **Use Case**: Complement to Stripe/Nevermined for margin protection

---

## Payment Rail Selection Matrix

| Rail | Geographic | Receive | Send | API/Webhooks | Settlement | Custody | Agentic Fit |
|------|------------|---------|------|--------------|------------|---------|-------------|
| **Stripe** | 46+ countries | ✓ | ✓ (Connect) | ✓ Webhooks | T+2 | Custodial | ★★★★☆ (add stablecoin for agents) |
| **PayPal/Braintree** | 200+ countries | ✓ | ✓ | ✓ Webhooks | T+1 | Custodial | ★★★☆☆ |
| **Wise** | 160+ countries | Limited | ✓ | ✓ Webhooks | Same-day | Transit | ★★★☆☆ |
| **Payoneer** | 190+ countries | ✓ | ✓ | ✓ Webhooks | T+2 | Custodial | ★★★☆☆ |
| **Nevermined** | Global (crypto) | ✓ | ✓ | ✓ Agent-native | Instant | Non-custodial | ★★★★★ |
| **Circle** | Global (USDC) | ✓ | ✓ | ✓ Permissionless | Instant | Non-custodial | ★★★★☆ |
| **Crossmint** | Global (50 chains) | ✓ | ✓ | ✓ Full-stack | Instant | Both | ★★★★★ |
| **Stripe Stablecoin** | Global (limited chains) | ✓ | ✓ | ✓ Via Stripe | Instant | Custodial | ★★★★☆ |

---

## Recommendations for Charterforge

### Tier 1: Core Support (Recommended)

1. **Stripe** — Traditional processor for fiat payments, stablecoin add-on
   - Entry point: `charterforge-stripe-rail`
   - Webhook events: checkout.session.completed, payment_intent.*
   - Credential: `STRIPE_WEBHOOK_SIGNING_SECRET`, `STRIPE_API_KEY`

2. **Nevermined** — Agent-native payments for autonomous commerce
   - Entry point: `charterforge-nevermined-rail` (recommended)
   - Native agent identity, instant settlement, tamper-proof metering
   - Credential: Nevermined API key

3. **Circle/Crossmint** — Stablecoin rails for crypto-native businesses
   - Entry point: `charterforge-circle-rail` or `charterforge-crossmint-rail`
   - Permissionless (Circle) or full-stack (Crossmint)
   - No custody (user-controlled wallets)

### Tier 2: Geographic Expansion

4. **Wise** — Cross-border payouts, payroll, contractor payments
   - Entry point: `charterforge-wise-rail`
   - Use for: Sending payments to 160+ countries at mid-market FX

5. **Payoneer** — B2B/freelancer markets, multi-currency receiving accounts
   - Entry point: `charterforge-payoneer-rail`
   - Use for: Marketplace payouts, international invoicing

6. **PayPal/Braintree** — Consumer trust, broad coverage
   - Entry point: `charterforge-paypal-rail`
   - Use for: Consumer-facing payments, PayPal as payment method

### Tier 3: Regional Specialists

7. **Checkout.com**, **Adyen**, **Airwallex** — Regional expansion

---

## Implementation Path

### Phase 1: Core Rails (Current)
- [x] Stripe rail (`packages/charterforge-stripe-rail`)
- [ ] Nevermined rail (recommended for agentic commerce)
- [ ] Circle rail (stablecoin direct)

### Phase 2: Cross-Border
- [ ] Wise rail
- [ ] Payoneer rail

### Phase 3: Consumer/Fiat
- [ ] PayPal rail
- [ ] Additional regional processors (Adyen, Checkout.com)

---

## Charter Configuration

Users can enable multiple payment rails by installing plugin packages:

```bash
# Core rails
pip install charterforge-stripe-rail

# Agentic-native
pip install charterforge-nevermined-rail

# Stablecoin
pip install charterforge-circle-rail

# Cross-border
pip install charterforge-wise-rail
pip install charterforge-payoneer-rail
```

After installation, rails are auto-discovered via entry points:

```bash
charterforge business payment-rails --check
```

The charter file does not need to specify rails — they're discovered at runtime
based on installed packages and environment credentials.

---

## Credential Requirements

| Rail | Environment Variables | Notes |
|------|----------------------|-------|
| Stripe | `STRIPE_WEBHOOK_SIGNING_SECRET`, `STRIPE_API_KEY` | Webhook secret required for inbound |
| Nevermined | `NEVERMINED_API_KEY` | Agent identity auto-created |
| Circle | None (permissionless) or `CIRCLE_API_KEY` for mint/redeem | CCTP/Gateway permissionless |
| Crossmint | `CROSSMINT_API_KEY` | Required for compliance layer |
| Wise | `WISE_API_KEY`, `WISE_PROFILE_ID` | Profile = personal or business |
| Payoneer | `PAYONEER_API_KEY`, `PAYONEER_PARTNER_ID` | Mass Payouts API |
| PayPal | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | OAuth2 credentials |

---

## References

- Nevermined: https://nevermined.ai/ (Agentic Payments & Settlement)
- Circle Developer: https://developers.circle.com/
- Crossmint: https://www.crossmint.com/
- Stripe Stablecoin: https://stripe.com/docs/crypto
- Wise Platform: https://docs.wise.com/
- Payoneer Developer: https://www.payoneer.com/developers/
- PayPal Developer: https://developer.paypal.com/
