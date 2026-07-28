---
name: onchain-safety
description: Pre-execution risk analysis for onchain actions — flags scam contracts, dangerous token approvals, MEV exposure, and unsafe swaps before signing. For agents and users reviewing crypto transactions, ERC-20 allowances, and DEX quotes.
version: 0.1.0
author: Baophan (Baophan00), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Onchain, Blockchain, Crypto, Safety, Security, DeFi, Wallet, Risk]
    related_skills: [hyperliquid, solana, evm]
---

# Onchain Safety Skill

Risk-analysis layer for onchain actions. Before an agent (or user) signs a
swap, approval, or contract call, this skill inspects the action and returns a
**GO / CAUTION / NO-GO** verdict with concrete reasons — mirroring how an RL
reward model would score a trajectory step without executing it.

Read-only analysis. No signing, no key handling, no transaction broadcast.

---

## When to Use

- User (or agent) is about to sign an ERC-20 `approve`, a DEX swap, or call a contract
- User pastes a transaction payload, calldata, or a contract/token address for review
- User asks "is this safe?", "should I approve this?", "is this contract a scam?"
- Agentic wallet (e.g. OKX Agentic Wallet) is about to execute an onchain step and wants a pre-flight check

---

## Pre-flight Check (agent-facing procedure)

Run this checklist on any pending onchain action. Each failed check downgrades
the verdict one level (GO → CAUTION → NO-GO).

| # | Check | NO-GO if | Tool / source |
|---|---|---|---|
| 1 | Address reputation | Contract not in a known-good allowlist AND no public audit/label | block explorer label API |
| 2 | Approval size | `approve` sets `amount = max(uint256)` (unlimited) | decode calldata `spender`+`amount` |
| 3 | Token legitimacy | Token has no liquidity, mint function owned by EOA, or honeypot flags | dex pair / holder scan |
| 4 | MEV exposure | Swap slippage > 3% or path crosses low-liquidity pool | quote vs spot |
| 5 | Phishing pattern | Calldata calls `setApprovalForAll` on NFT or `permit` with deadline 0 | decode calldata |
| 6 | Chain sanity | Target chain mismatches agent's active network | config compare |

### Verdict rules
- All pass → **GO**
- 1–2 minor (CAUTION-class) → **CAUTION** (proceed with limit: bounded amount, revocable)
- Any NO-GO check → **NO-GO** (do not sign; explain which check failed)

---

## Calldata decoding (stdlib only)

The helper script decodes the four high-risk selectors without external libs:

```bash
python3 ~/.hermes/skills/blockchain/onchain-safety/scripts/decode_action.py \
  --chain ethereum \
  --to 0x... \
  --data 0x...
```

Selectors recognized:
- `0x095ea7b3` — `approve(address,uint256)`
- `0xa22cb465` — `setApprovalForAll(address,bool)`
- `0xd505accf` — `permit(...)`
- `0x38ed1739` — `swapExactTokensForTokens(...)`

Output: JSON with `{action, spender, amount, unlimited, risk}`.

---

## Integrations

If an agentic-wallet skill is loaded (OKX Agentic Wallet, SmartVault), call
this skill's pre-flight check **before** invoking the wallet's sign path. The
wallet skill remains the executor; this skill is the brake.

**IF** `OKX_AGENTIC_WALLET` configured → route NO-GO verdicts back to the
wallet as a hard abort. **ELSE** → surface verdict to the user as a warning.

---

## Security note

This skill never holds keys and never broadcasts. It is a read-only advisor.
A GO verdict is not financial advice — it means "no structural red flags
detected", not "this trade will profit".
