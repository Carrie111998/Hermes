#!/usr/bin/env python3
"""Repro: Copilot auth accepts arbitrary non-empty GITHUB_TOKEN values.

Bug class: #12650 / #13970 — validate_copilot_token() returned True for any
non-empty token, so a classic PAT (ghp_*), a random string, or a stale
token was accepted and the Copilot API call failed later with no clear
diagnosis.

On main: FAILS (arbitrary token accepted). With the fix: PASSES (only
supported prefixes accepted).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_cli.copilot_auth import validate_copilot_token  # noqa: E402

# The exact reporter scenario: arbitrary non-empty token
valid, msg = validate_copilot_token("not_a_github_token")
print(f"arbitrary token -> valid={valid} msg={msg!r}")
if valid:
    print("FAIL: arbitrary non-empty token accepted")
    sys.exit(1)

# Supported prefixes must still pass
for tok in ("gho_ab...1234", "github_pat_ab...1234", "ghu_ab...1234"):
    v, _ = validate_copilot_token(tok)
    if not v:
        print(f"FAIL: supported token rejected: {tok[:12]}...")
        sys.exit(1)

print("PASS: arbitrary tokens rejected, supported prefixes accepted")
sys.exit(0)
