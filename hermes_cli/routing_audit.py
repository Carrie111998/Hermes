"""Append-only record of routing decisions.

Profile-aware location: ``$HERMES_HOME/logs/routing.jsonl``. One JSON object
per line, in the shape ``hermes_cli/dashboard_auth/audit.py`` established:

* **profile-aware** — resolved through ``get_hermes_home()``, so a per-profile
  board writes to that profile's log;
* **redacting** — token-like fields are dropped before serialisation;
* **never raises** — a decision is recorded *about* work that is happening
  anyway. If the log cannot be written, the board write must still succeed;
  losing an audit line is bad, failing a task because of one is worse.

In M3b every line carries ``selection: "manual"``. There is no classifier and
no inference: a lane is an explicit human or PM-agent choice recorded on the
card. This file is the dataset that would justify automating that later — it is
not the automation.

Token and cost figures are deliberately absent unless a caller supplies them.
They are **joined, never recomputed**: ``task_runs.session_id`` (the key this
slice adds) plus ``task_runs.profile`` resolve to that profile's
``state.db.session_model_usage``, which already carries every field.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_write_lock = threading.Lock()

# Never written raw, whatever a caller passes. Mirrors the dashboard-auth list
# and adds the provider-credential names a routing record could plausibly be
# handed by mistake.
_REDACTED_FIELDS: frozenset = frozenset({
    "access_token", "refresh_token", "code", "code_verifier", "state",
    "ticket", "cookie", "Authorization", "authorization",
    "api_key", "apikey", "key", "secret", "token", "password",
})

ROUTING_DECIDED = "routing_decided"


def _resolve_log_path() -> Path:
    """``$HERMES_HOME/logs/routing.jsonl``, honouring profile overrides."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "routing.jsonl"


def record_routing_decision(**fields: Any) -> bool:
    """Append one routing decision. Returns True when it reached disk.

    Never raises. The boolean exists so a caller can log a miss, not so it can
    fail: every call site treats a False as "the audit line was lost", never as
    "the operation failed".
    """
    safe = {k: v for k, v in fields.items() if k not in _REDACTED_FIELDS}
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": ROUTING_DECIDED,
        "selection": "manual",
        **safe,
    }
    # `selection` is not a caller's to override: M3b has no other value.
    entry["selection"] = "manual"
    try:
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        path = _resolve_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception as exc:
        _log.warning("routing decision log write failed: %s", exc)
        return False
