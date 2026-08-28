"""Append-only record of routing decisions.

Profile-aware location: ``$HERMES_HOME/logs/routing.jsonl``. One JSON object
per line, in the shape ``hermes_cli/dashboard_auth/audit.py`` established:

* **profile-aware** — resolved through ``get_hermes_home()``, so a per-profile
  board writes to that profile's log;
* **redacting** — token-like *field names* are dropped, AND every serialised
  string value goes through Hermes' canonical
  ``agent.redact.redact_sensitive_text``. A name-only denylist was not enough:
  ``set_routing_lane`` accepts an arbitrary lane string, so a
  credential-shaped value reached disk verbatim through the ordinary writer,
  not merely through a contrived kwargs call;
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
from typing import Any, Mapping

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


def _redact_value(value: Any) -> Any:
    """Redact credential-shaped material from any value we are about to write.

    Applied to every string, including strings nested in lists and mappings,
    because the field-name denylist only catches fields someone thought to
    name. Non-string scalars pass through; anything unrenderable degrades to a
    marker rather than raising, since this module must never raise.
    """
    if isinstance(value, str):
        try:
            from agent.redact import redact_sensitive_text

            return redact_sensitive_text(
                value, force=True, redact_url_credentials=True
            )
        except Exception:
            # Redaction unavailable ⇒ nothing may leave.
            return "[redaction unavailable]"
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in list(value)[:32]]
    return value


def record_routing_decision(**fields: Any) -> bool:
    """Append one routing decision. Returns True when it reached disk.

    Never raises. The boolean exists so a caller can log a miss, not so it can
    fail: every call site treats a False as "the audit line was lost", never as
    "the operation failed".
    """
    safe = {
        k: _redact_value(v) for k, v in fields.items()
        if k not in _REDACTED_FIELDS
    }
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


def usage_for_session(session_id: str) -> "dict[str, Any] | None":
    """Join this profile's recorded usage for one session. Never raises.

    Token counts and API-call totals are **read from
    ``state.db.session_model_usage``**, never recomputed here — that table is
    already written per API call by the agent loop, and a second accounting
    path would drift from it.

    Resolution is deliberately the *current* profile's ``state.db``: a run's
    terminal record is written by the worker process, which IS that profile,
    so ``get_hermes_home()`` is the right database. Cross-profile joins are
    not attempted; a caller in the wrong process gets ``None`` rather than a
    wrong number.
    """
    text = str(session_id or "").strip()
    if not text:
        return None
    try:
        import sqlite3

        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "state.db"
        if not path.exists():
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                          COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                          COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
                          COALESCE(SUM(reasoning_tokens), 0)   AS reasoning_tokens,
                          COALESCE(SUM(api_call_count), 0)     AS api_call_count,
                          COUNT(*)                             AS rows_joined
                     FROM session_model_usage
                    WHERE session_id = ?""",
                (text,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _log.debug("routing usage join unavailable: %s", exc)
        return None
    if row is None or not int(row["rows_joined"] or 0):
        return None
    return {
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"]),
        "reasoning_tokens": int(row["reasoning_tokens"]),
        "api_call_count": int(row["api_call_count"]),
    }
