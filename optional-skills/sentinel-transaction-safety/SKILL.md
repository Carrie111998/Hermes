---
name: sentinel-transaction-safety
description: Get a pre-execution safety verdict via SENTINEL.
license: MIT
compatibility: Requires internet access to reach the SENTINEL API. No local dependencies. Compatible with any agent that can make HTTP POST requests. Requires an x402-capable wallet to pay per call — there is no free trial.
metadata:
  author: teodorofodocrispin-cmyk
  version: "1.0.0"
  endpoint: https://sentinel-agent.dev/v1/guard
  health: https://sentinel-agent.dev/health
  pricing: https://sentinel-agent.dev/pricing
  languages: English
  homepage: https://github.com/teodorofodocrispin-cmyk/sentinel-public
---

# SENTINEL — Agent Transaction Safety Oracle

An **opt-in hosted API** that returns a pre-execution safety verdict for an
on-chain transaction: `SAFE` / `UNSAFE` / `UNKNOWN`, with a signed (ed25519)
receipt. You send the chain, sender, and transaction payload; SENTINEL
evaluates contract security (GoPlus), simulates the call (Alchemy `eth_call`),
checks for honeypots (honeypot.is), and checks LP concentration, then returns
a verdict plus a graduated 0–100 risk score. The request transits SENTINEL's
infrastructure (FastAPI + Supabase + Render) — this is a hosted risk check,
not a local computation.

## Transparency notice

- The transaction payload (chain, sender, tx data) is transmitted to
  `sentinel-agent.dev` for evaluation. If your policy forbids sending
  pre-signature transaction data to third parties, do not use this skill.
- The verdict is produced by rule-based checks plus an LLM council
  (Claude Haiku + GPT-4o-mini) server-side; results are returned as JSON with
  a signed receipt.
- **No free trial.** Every call to `/v1/guard` requires x402 payment
  ($0.005 USDC on Base) — there is no no-wallet preview endpoint today.

## When to use

- An agent is about to sign an on-chain transaction and wants a pre-flight
  safety check (rug pull, honeypot, malicious contract).
- You need a signed, independently verifiable SAFE/UNSAFE/UNKNOWN verdict
  before spending funds.

## When NOT to use

- Strict zero-transmission environments (air-gapped, on-premise).
- Flows where the unsigned transaction must never leave the local machine.
- Any agent without an x402-capable wallet (there is no free tier to fall
  back to).

## How it works

1. You POST `{chain, from, tx}` to `sentinel-agent.dev/v1/guard`.
2. SENTINEL returns a JSON verdict (`SAFE` / `UNSAFE` / `UNKNOWN`), a 0–100
   risk score, the contributing risk signals, and an ed25519-signed receipt.

## Paid usage (required, user-controlled)

Payment is **out of band and user-controlled** — the skill never instructs
the agent to sign transactions or spend funds on the agent's behalf beyond
the payment itself. `POST /v1/guard` without payment returns HTTP 402 with
`accepts` (network `base`, price $0.005 USDC, `payTo` published in the
response). A human operator (or the agent's own x402-capable payment client,
e.g. `x402-fetch`) signs an EIP-3009 `TransferWithAuthorization` for the
exact amount and retries with the `X-PAYMENT` header. The skill itself does
not embed or construct that transfer.

## API request

**Endpoint:** `POST https://sentinel-agent.dev/v1/guard`
**Headers:** `Content-Type: application/json`, `X-PAYMENT: <x402 payload>`

```json
{
  "chain": "base",
  "from": "0xYourAgentWallet",
  "tx": { "to": "0xTargetContract", "data": "0x...", "value": "0x0" }
}
```

## Response (success 200)

```json
{
  "verdict": "SAFE",
  "riskScore": 6,
  "contract": { "risks": [] },
  "signature": "ed25519:...",
  "signer": "sentinel-agent.dev"
}
```

## Response (no payment, 402)

```json
{
  "x402Version": 2,
  "accepts": [
    { "network": "base", "amount": "5000", "payTo": "0xCf1d...337E7" }
  ]
}
```

## Known limitations

- No free trial or no-wallet preview endpoint exists today.
- Verdicts reflect the checks currently implemented (contract security,
  simulation, honeypot detection, LP concentration); they are not a
  guarantee against novel attack patterns.
- Paid endpoints require x402 settlement via a facilitator (CDP/PayAI).

## Resources

- GitHub: https://github.com/teodorofodocrispin-cmyk/sentinel-public
- Health: https://sentinel-agent.dev/health
- Pricing: https://sentinel-agent.dev/pricing
- Docs: https://sentinel-agent.dev/llms.txt
