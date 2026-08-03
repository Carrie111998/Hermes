"""Shared request fingerprint for ChatGPT's private Codex backends."""

from __future__ import annotations

import base64
import json
from typing import Dict


def codex_cloudflare_headers(access_token: str) -> Dict[str, str]:
    """Return the first-party-shaped headers expected by ChatGPT's edge.

    The Cloudflare layer in front of ChatGPT's private Codex routes accepts a
    small set of first-party originators.  Match the upstream codex-rs client
    and include the account ID carried by the OAuth JWT when available.
    Malformed tokens intentionally omit the account header so the backend can
    return the authoritative authentication error.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers

    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-ID"] = account_id
    except Exception:
        pass

    return headers
