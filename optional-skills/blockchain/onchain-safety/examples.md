# Onchain Safety — Examples

## 1. Pre-flight an unlimited ERC-20 approval (NO-GO)

```bash
python3 ~/.hermes/skills/blockchain/onchain-safety/scripts/decode_action.py \
  --chain ethereum \
  --to 0xSpenderContractAddress \
  --data 0x095ea7b3<spender_padded><max_uint256>
```

Expected verdict: `risk: NO-GO`, `unlimited: true`. Agent must NOT sign;
surface "unlimited approval detected" to the user or abort the wallet call.

## 2. Bounded approval (CAUTION)

```bash
python3 ~/.hermes/skills/blockchain/onchain-safety/scripts/decode_action.py \
  --chain ethereum \
  --to 0xTokenAddress \
  --data 0x095ea7b3<spender_padded><amount_hex>
```

Expected: `risk: CAUTION`, `unlimited: false`. Proceed but recommend revoke
after use.

## 3. setApprovalForAll on NFT (NO-GO)

```bash
python3 ~/.hermes/skills/blockchain/onchain-safety/scripts/decode_action.py \
  --chain ethereum \
  --to 0xNFTContract \
  --data 0xa22cb465<operator_padded>0000000000000000000000000000000000000000000000000000000000000001
```

Expected: `risk: NO-GO`, `approved: true`. Operator gains full transfer rights.

## 4. Agentic-wallet integration (pseudo)

```text
1. Wallet skill builds tx -> calls onchain-safety pre-flight
2. IF risk == NO-GO -> hard abort, return reason to user
3. IF risk == CAUTION -> proceed with bounded amount, log warning
4. IF risk == ok -> sign
```

No `EXAMPLES_API` needed — this skill is read-only and offline.
