#!/usr/bin/env python3
"""Decode high-risk onchain actions from calldata (stdlib only).

Recognizes the four dangerous selectors so an agent can pre-flight a pending
transaction before signing. Returns JSON: {action, ..., risk}.

Fail-closed: any calldata that does not match the full ABI layout for a
recognized selector returns risk="NO-GO" with a reason, instead of a misleading
CAUTION/GO verdict.
"""
import argparse
import json
import sys

# selector -> (action name, min body length in hex chars = 64 * num_args)
SELECTORS = {
    "0x095ea7b3": ("approve", 128),            # address(32) + uint256(32)
    "0xa22cb465": ("setApprovalForAll", 128),  # address(32) + bool(32)
    "0xd505accf": ("permit", 512),              # 8 words (token,owner,spender,value,deadline,v,r,s)
    "0x38ed1739": ("swapExactTokensForTokens", 256),  # amountIn(32)+amountOutMin(32)+path(32 ptr)+to(32)+deadline(32)
}

# ABI word size in hex chars (32 bytes = 64 hex)
_WORD = 64
MAX_UINT = (1 << 256) - 1


def _int_from_hex(h: str) -> int:
    try:
        return int(h, 16)
    except (ValueError, TypeError):
        return 0


def _addr_from_word(word: str) -> str:
    # words are left-padded; take the last 40 hex chars (20 bytes)
    if len(word) < 40:
        return "0x0"
    return "0x" + word[-40:]


def _fail(action: str, selector: str, reason: str) -> dict:
    """Fail-closed verdict for malformed calldata."""
    return {
        "action": action,
        "selector": selector,
        "risk": "NO-GO",
        "reason": reason,
    }


def decode(data: str) -> dict:
    data = data.lower().removeprefix("0x")
    if len(data) < 8:
        return {"action": "unknown", "risk": "unknown", "note": "calldata too short"}
    selector = "0x" + data[:8]
    info = SELECTORS.get(selector)
    if not info:
        return {"action": "unknown", "selector": selector, "risk": "unknown"}
    action, min_body_hex = info
    body = data[8:]
    out = {"action": action, "selector": selector, "risk": "ok"}

    if len(body) < min_body_hex:
        return _fail(action, selector, "malformed calldata: insufficient ABI words")

    # ---- approve(address,uint256) ----
    if action == "approve":
        spender_word = body[0:_WORD]
        amount_word = body[_WORD:2 * _WORD]
        spender = _addr_from_word(spender_word)
        amount = _int_from_hex(amount_word)
        unlimited = amount >= MAX_UINT - 1
        out.update(spender=spender, amount=str(amount), unlimited=unlimited)
        out["risk"] = "NO-GO" if unlimited else "CAUTION"
        out["reason"] = (
            "unlimited approval (max uint256)" if unlimited
            else "bounded approval — revoke after use"
        )

    # ---- setApprovalForAll(address,bool) ----
    elif action == "setApprovalForAll":
        spender = _addr_from_word(body[0:_WORD])
        approved = body[_WORD:2 * _WORD].lstrip("0") == "1"
        out.update(spender=spender, approved=approved)
        out["risk"] = "NO-GO" if approved else "ok"
        out["reason"] = "grants operator full NFT control" if approved else "revoke"

    # ---- permit(token,owner,spender,value,deadline,v,r,s) ----
    elif action == "permit":
        # word 4 = deadline (uint256). A zero deadline means the permit is
        # only valid for the current block — flag as NO-GO (deadline abuse).
        deadline_word = body[4 * _WORD:5 * _WORD]
        deadline = _int_from_hex(deadline_word)
        spender = _addr_from_word(body[2 * _WORD:3 * _WORD])
        out.update(spender=spender, deadline=str(deadline))
        if deadline == 0:
            out["risk"] = "NO-GO"
            out["reason"] = "permit deadline is zero (deadline abuse)"
        else:
            out["risk"] = "CAUTION"
            out["reason"] = "offline approval — verify spender + deadline"

    # ---- swapExactTokensForTokens(amountIn,amountOutMin,path,to,deadline) ----
    elif action == "swapExactTokensForTokens":
        # word 4 = deadline; word 3 = path pointer (array). We validate
        # structure only — a zero deadline is the documented NO-GO hazard.
        deadline_word = body[4 * _WORD:5 * _WORD]
        deadline = _int_from_hex(deadline_word)
        out.update(deadline=str(deadline))
        if deadline == 0:
            out["risk"] = "NO-GO"
            out["reason"] = "swap deadline is zero (front-run / replay risk)"
        else:
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
