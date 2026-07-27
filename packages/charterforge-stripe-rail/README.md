# Charterforge Stripe rail

This is an optional standalone payment rail. Install it into the same
environment as Charterforge with `pip install -e packages/charterforge-stripe-rail`
and provide the secret-only `STRIPE_SECRET_KEY` environment variable.

The rail creates Stripe Checkout Sessions for inbound receivables and reads
them back for verification. Outbound payments require both an explicit Stripe
Connected Account and provider payment-method identifier; arbitrary vendor
payouts are rejected. Charterforge stores only opaque provider references and
read-back evidence. It never receives PANs, bank credentials, or private keys.

`route_webhook_event` verifies Stripe's signed timestamp/HMAC envelope, accepts
only payment event types, and routes an idempotent authenticated event into the
durable Charterforge objective inbox. The raw webhook body is not persisted.

Stripe account, tax, sanctions, webhook-signature, and jurisdictional
obligations remain the operator's responsibility and must be recorded in the
Charterforge compliance/provider-assessment records before execution.
