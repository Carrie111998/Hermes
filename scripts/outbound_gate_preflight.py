#!/usr/bin/env python3
"""Delivery-free outbound-gate preflight for Neo's one-restart activation check.

This harness deliberately has no adapter, gateway client, socket, or platform
credential. It exercises the exact policy function with an injected deterministic
fetch result, so the deliberately dead URL can never leave process memory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ``python -I -S scripts/outbound_gate_preflight.py`` is the documented safe
# invocation: isolated mode prevents ambient site customization and this single
# explicit repository root is the only import path added.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.outbound_message_gate import gate_outbound_message


def run_preflight() -> dict[str, Any]:
    settings = {"protected_targets": ["preflight:protected"]}
    dead_link = gate_outbound_message(
        platform="preflight",
        chat_id="protected",
        content="Do not deliver https://dead.example/never-send?token=secret",
        metadata={},
        settings=settings,
        fetcher=lambda _url: {
            "ok": False,
            "status": 404,
            "final_url": "",
            "error": "controlled preflight dead destination",
        },
    )
    naked_claim = gate_outbound_message(
        platform="preflight",
        chat_id="protected",
        content="The outbound gate is fixed.",
        metadata={"_hermes_session_id": "preflight", "_hermes_turn_id": "preflight"},
        settings=settings,
        fetcher=lambda _url: (_ for _ in ()).throw(
            AssertionError("claim preflight must not perform network I/O")
        ),
    )
    passed = (
        dead_link.get("action") == "rewrite"
        and "dead.example" not in str(dead_link.get("content") or "")
        and naked_claim.get("action") == "rewrite"
        and str(naked_claim.get("content") or "").startswith("UNVERIFIED")
    )
    return {
        "schema": "hermes-outbound-gate-preflight/v1",
        "transport": "in-memory-only",
        "dead_link": dead_link,
        "naked_fixed_claim": naked_claim,
        "pass": passed,
    }


def main() -> int:
    report = run_preflight()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
