#!/usr/bin/env python3
"""Repro: load_hermes_dotenv clobbers shell-set env vars (12-factor violation).

Bug class: #18705 / #19201 — .env loaded with override=True, so a value
already set in the shell (the 12-factor source of truth) is silently
overwritten by the profile .env, breaking credential-rotation footguns.

On main: FAILS (env var clobbered). With the fix: PASSES (shell wins).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402

home = Path(tempfile.mkdtemp(prefix="hermes-repro-"))
(home / ".env").write_text("HERMES_ACP_AUTH_METHOD=claude_code_cli\n", encoding="utf-8")

os.environ["HERMES_ACP_AUTH_METHOD"] = "cursor_login"  # shell-set value

load_hermes_dotenv(hermes_home=home)

got = os.environ.get("HERMES_ACP_AUTH_METHOD")
print(f"shell value: cursor_login | after load: {got!r}")
if got == "cursor_login":
    print("PASS: shell value preserved (12-factor)")
    sys.exit(0)
print("FAIL: .env clobbered the shell value")
sys.exit(1)
