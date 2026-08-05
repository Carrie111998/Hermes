from __future__ import annotations

import json
import os
from pathlib import Path

from agent.account_usage import fetch_account_usage
from ai_usage.collector import collect, write_atomic


def _home() -> str:
    return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def main() -> int:
    home = _home()
    out = Path(home) / "architecture-map" / "ai-tokens.json"
    # state.db lives at the ~/.hermes ROOT, never profile-scoped (see CLAUDE.md).
    db = os.environ.get("HERMES_STATE_DB") or os.path.join(home, ".hermes", "state.db")

    prev = None
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            prev = None

    data = collect(db_path=db, prev=prev, fetch_usage=fetch_account_usage)
    write_atomic(out, data)
    print(f"wrote {out} ({len(data['providers'])} providers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
