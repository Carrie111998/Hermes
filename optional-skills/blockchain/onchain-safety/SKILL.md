---
name: onchain-safety
description: Pre-flight risk analysis for onchain actions before signing.
version: 0.1.0
author: Baophan (Baophan00), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Onchain, Blockchain, Crypto, Safety, Security, DeFi]
    related_skills: [hyperliquid, solana, evm]
---

# Onchain Safety Skill

Read-only risk advisor that scores a pending onchain action **before** an agent
or agentic wallet signs it. It inspects calldata for the four high-risk ERC-20
/ ERC-721 selectors and returns a **GO / CAUTION / NO-GO** verdict with concrete
reasons — mirroring how an RL reward model would score a trajectory step
without executing it.

It does **not** sign, hold keys, or broadcast transactions. It is a brake, not
an executor.

---

## When to Use

- User (or agent) is about to sign an ERC-20 `approve`, `setApprovalForAll`,
  `permit`, or `swapExactTokensForTokens` call
- User pastes calldata or a transaction payload for review
- User asks "is this safe?", "should I approve this?", "is this contract a scam?"
- An agentic wallet (OKX Agentic Wallet, SmartVault) is about to execute an
  onchain step and wants a pre-flight check

---

## Prerequisites

Stdlib only (`argparse`, `json`). No API key, no external packages.

```bash
python3 ~/.hermes/skills/blockchain/onchain-safety/scripts/decode_action.py \
  --chain ethereum --to 0x... --data 0x...
```

---

## Quick Reference

| Selector | Action | NO-GO trigger |
|---|---|---|
| `0x095ea7b3` | `approve(address,uint256)` | amount = max(uint256) |
| `0xa22cb465` | `setApprovalForAll(address,bool)` | approved = true |
| `0xd505accf` | `permit(...)` | deadline = 0 |
| `0x38ed1739` | `swapExactTokensForTokens(...)` | deadline = 0 |

Malformed calldata for a recognized selector → **NO-GO** (fail-closed).

---

## Procedure

1. Agent receives a pending onchain action (calldata + target address)
2. Run `decode_action.py --to <addr> --data <hex>`
3. Inspect the returned `risk` field:
   - `NO-GO` — abort the sign call; surface `reason` to the user
   - `CAUTION` — proceed with a bounded amount; log the warning
   - `ok` — no structural red flags detected
4. If integrated with an agentic-wallet skill, route `NO-GO` verdicts back as a
   hard abort before the wallet's sign path is invoked

---

## Pitfalls

- **This skill does not validate token legitimacy or contract audits** — it
  only decodes the four selectors. For full contract risk, pair with a block-
  explorer label API.
- **Deadline checks are binary** (0 = NO-GO). A non-zero but low deadline is
  still CAUTION, not GO.
- **Calldata must be complete.** Truncated calldata for a recognized selector
  returns NO-GO (fail-closed) rather than guessing.

---

## Verification

```bash
# Unlimited approve -> NO-GO
python3 scripts/decode_action.py --to 0xAa --data \
  0x095ea7b300000000000000000000000000000000000000000000deadbeef00000000000000000000000000000000000000000000000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
# expect risk=NO-GO, unlimited=true

# Truncated calldata -> NO-GO (fail-closed)
python3 scripts/decode_action.py --to 0xAa --data 0x095ea7b3
# expect risk=NO-GO, reason="malformed calldata: insufficient ABI words"
```
