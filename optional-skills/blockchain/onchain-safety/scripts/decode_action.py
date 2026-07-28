#!/usr/bin/env python3
"""Decode high-risk onchain actions from calldata (stdlib only).

Recognizes the four dangerous selectors so an agent can pre-flight a pending
transaction before signing. Prints JSON: {action, spender, amount, unlimited,
risk}. No network calls, no signing, no broadcast.

Usage:
    python3 decode_action.py --chain ethereum --to 0x... --data 0x...
"""
import argparse
import json
import sys

# selector -> (action name, arg layout we care about)
SELECTORS = {
    "0x095ea7b3": ("approve", "address,uint256"),
    "0xa22cb465": ("setApprovalForAll", "address,bool"),
    "0xd505accf": ("permit", "token,owner,spender,value,deadline"),
    "0x38ed1739": ("swapExactTokensForTokens", "amountIn,amountOutMin,path,to,deadline"),
}

MAX_UINT = (1 << 256) - 1


def _int_from_hex(h: str) -> int:
    return int(h, 16)


def decode(data: str) -> dict:
    data = data.lower().removeprefix("0x")
    if len(data) < 8:
        return {"action": "unknown", "risk": "unknown", "note": "calldata too short"}
    selector = "0x" + data[:8]
    info = SELECTORS.get(selector)
    if not info:
        return {"action": "unknown", "selector": selector, "risk": "unknown"}
    action, _ = info
    body = data[8:]
    out = {"action": action, "selector": selector, "risk": "ok"}

    # approve(address,uint256): spender = bytes 8..40, amount = last 32 bytes
    if action == "approve":
        spender = "0x" + body[24:64]
        amount = _int_from_hex(body[-64:]) if len(body) >= 64 else 0
        unlimited = amount >= MAX_UINT - 1
        out.update(spender=spender, amount=str(amount), unlimited=unlimited)
        out["risk"] = "NO-GO" if unlimited else "CAUTION"
        out["reason"] = (
            "unlimited approval (max uint256)" if unlimited
            else "bounded approval — revoke after use"
        )
    elif action == "setApprovalForAll":
        spender = "0x" + body[24:64]
        approved = body[-1] == "1"
        out.update(spender=spender, approved=approved)
        out["risk"] = "NO-GO" if approved else "ok"
        out["reason"] = "grants operator full NFT control" if approved else "revoke"
    elif action == "permit":
        # permit(token,owner,spender,value,deadline,v,r,s) — deadline at arg 5
        out["risk"] = "CAUTION"
        out["reason"] = "offline approval — verify spender + deadline"
    elif action == "swapExactTokensForTokens":
        out["risk"] = "CAUTION"
        out["reason"] = "verify slippage + path liquidity before signing"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="ethereum")
    ap.add_argument("--to", required=True, help="target contract address")
    ap.add_argument("--data", required=True, help="calldata hex")
    args = ap.parse_args()
    result = decode(args.data)
    result["chain"] = args.chain
    result["to"] = args.to
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
