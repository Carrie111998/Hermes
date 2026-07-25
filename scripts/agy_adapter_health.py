#!/usr/bin/env python3
"""Privacy-safe health check for the agy-backed Hermes adapter."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.agy_cli_adapter import (  # noqa: E402
    AGY_SENTINEL_BASE_URL,
    AgyCliClient,
    get_adapter_health,
)
from hermes_constants import get_hermes_home  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Perform one real OAuth model probe")
    parser.add_argument("--model", default="gemini-3.6-flash-high")
    parser.add_argument(
        "--workdir",
        default=str(get_hermes_home() / "agy-adapter-workdir"),
    )
    args = parser.parse_args()

    snapshot = get_adapter_health(workdir=args.workdir)
    result: dict[str, object] = {"local": snapshot, "probe": "skipped"}
    if args.probe:
        phase = "client_init"
        try:
            client = AgyCliClient(base_url=AGY_SENTINEL_BASE_URL, workdir=args.workdir)
            phase = "invoke"
            response = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": "Reply exactly AGY_HEALTH_OK"}],
                tools=[],
                timeout=60,
            )
            phase = "validate"
            content = response.choices[0].message.content or ""
            result["probe"] = {
                "ok": content.strip() == "AGY_HEALTH_OK",
                "phase": phase,
                "model": response.model,
                "error_type": None if content.strip() == "AGY_HEALTH_OK" else "unexpected_output",
                "fallback_attempted": response.model != args.model,
            }
        except Exception as exc:  # expose controlled taxonomy, never CLI diagnostics
            result["probe"] = {
                "ok": False,
                "phase": phase,
                "model": args.model,
                "error_type": str(getattr(exc, "error_type", type(exc).__name__)),
                "fallback_attempted": False,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    local_ok = snapshot.get("status") == "ok"
    probe = result["probe"]
    probe_ok = probe == "skipped" or (isinstance(probe, dict) and probe.get("ok") is True)
    return 0 if local_ok and probe_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
