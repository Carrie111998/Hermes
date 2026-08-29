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
    """``$HERMES_HOME/logs/routing.jsonl`` for the CURRENT profile."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "routing.jsonl"


# Stable, bounded reason codes for a record whose owner cannot be resolved.
# They deliberately carry no part of the rejected value: it is untrusted input,
# and both sinks it would otherwise reach — the quarantine column and the
# application log — are persistent. The outbox id and run id are already
# sufficient non-sensitive correlation identifiers, and the value itself
# remains in the row's own ``profile`` column for recovery.
OWNER_INVALID = "invalid_profile_owner"
OWNER_MISSING = "missing_profile_owner"
OWNER_ESCAPED = "escaped_profile_owner"
OWNER_UNRESOLVABLE = "unresolvable_profile_owner"


def resolve_profile_log_owner(
    profile: "str | None",
) -> "tuple[Path | None, str | None]":
    """``(path, None)`` when *profile* owns a log, else ``(None, reason_code)``.

    The kanban board is shared across profiles, so whichever process drains the
    outbox must still write each record to the log of the profile that produced
    it — otherwise a PM or default process performing recovery silently
    relocates a coder's accounting into its own log.

    Resolution takes a profile **name**, never a path, and the name is treated
    as untrusted input:

    1. normalise to the canonical on-disk id;
    2. **validate** with Hermes' own ``validate_profile_name`` — normalisation
       alone lowercases and strips, and does *not* reject ``/``, ``\`` or
       ``..``. A name like ``../../escape`` once resolved to a directory
       outside the profiles root and was written to;
    3. ``default`` is handled explicitly as the root home, not as a child
       directory of the profiles root;
    4. the caller's own profile resolves to this process's home;
    5. any other name must be an existing directory whose **resolved real
       path** lies inside the **resolved profiles root**. That containment
       check is also the symlink policy: a symlinked profile directory is
       accepted only when it still lands inside the permitted root, and
       anything pointing outside fails closed.

    A reason code accompanies every rejection so the caller can quarantine the
    record and say *why* without repeating what it was given.
    """
    from hermes_constants import get_hermes_home

    if profile is None or (isinstance(profile, str) and not profile.strip()):
        # No owner recorded (a legacy row): this process's own log is the only
        # defensible destination.
        return get_hermes_home() / "logs" / "routing.jsonl", None
    if not isinstance(profile, str):
        # Never stringified: a non-string owner may have a __str__ that raises,
        # and it is not something any supported writer stages.
        return None, OWNER_INVALID
    try:
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            get_active_profile_name,
            normalize_profile_name,
            validate_profile_name,
        )

        try:
            canon = normalize_profile_name(profile)
            validate_profile_name(canon)      # rejects separators + traversal
        except Exception:
            return None, OWNER_INVALID

        if canon == "default":
            return (
                Path(_get_default_hermes_home()) / "logs" / "routing.jsonl",
                None,
            )

        try:
            active = normalize_profile_name(get_active_profile_name() or "")
        except Exception:
            active = ""
        if canon and canon == active:
            return get_hermes_home() / "logs" / "routing.jsonl", None

        root = Path(_get_profiles_root())
        candidate = root / canon
        if not candidate.is_dir():
            return None, OWNER_MISSING
        real_root = root.resolve()
        real_candidate = candidate.resolve()
        if real_candidate != real_root and real_root not in real_candidate.parents:
            # Escaped the permitted root — by traversal, or by a symlink
            # pointing outside it. Fail closed.
            return None, OWNER_ESCAPED
        return real_candidate / "logs" / "routing.jsonl", None
    except Exception as exc:
        # Only the exception CLASS, never its message: an exception raised
        # while handling an untrusted name tends to quote that name.
        _log.debug("routing log path unresolvable: %s", type(exc).__name__)
    return None, OWNER_UNRESOLVABLE


def resolve_profile_log_path(profile: "str | None") -> "Path | None":
    """The routing log belonging to *profile*, or ``None`` if unresolvable.

    Thin wrapper over :func:`resolve_profile_log_owner` for callers that do not
    need the rejection reason.
    """
    return resolve_profile_log_owner(profile)[0]


_MAX_REDACT_DEPTH = 6
_MAX_REDACT_ITEMS = 32


def _redact_value(value: Any, *, depth: int = 0, seen: "set | None" = None) -> Any:
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
    if depth >= _MAX_REDACT_DEPTH:
        return "[too deep]"
    if isinstance(value, (Mapping, list, tuple, set)):
        # A container can contain itself. Track identity down this branch so a
        # cycle becomes a marker instead of a RecursionError escaping the
        # never-raise boundary.
        marks = set(seen or ())
        if id(value) in marks:
            return "[cycle]"
        marks.add(id(value))
        if isinstance(value, Mapping):
            return {
                # Keys are serialised strings too, so they are redacted.
                _redact_value(k, depth=depth + 1, seen=marks):
                    _redact_value(v, depth=depth + 1, seen=marks)
                for k, v in list(value.items())[:_MAX_REDACT_ITEMS]
            }
        return [
            _redact_value(v, depth=depth + 1, seen=marks)
            for v in list(value)[:_MAX_REDACT_ITEMS]
        ]
    return value


def record_routing_decision(**fields: Any) -> bool:
    """Append one routing decision. Returns True when it reached disk.

    Never raises. The boolean exists so a caller can log a miss, not so it can
    fail: every call site treats a False as "the audit line was lost", never as
    "the operation failed".
    """
    # Internal only: the projector supplies the OWNING profile's resolved path.
    # It is popped before sanitisation so it can never appear in the record,
    # and it is a `Path` the projector resolved from a profile NAME — a caller
    # (or a stored payload) cannot smuggle a destination through `fields`.
    destination = fields.pop("_log_path", None)
    if destination is not None and not isinstance(destination, Path):
        destination = None
    try:
        safe = {
            _redact_value(k): _redact_value(v) for k, v in fields.items()
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
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        path = destination if destination is not None else _resolve_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception as exc:
        # Deliberately logs the exception TYPE only: the message could embed
        # the very value redaction was meant to keep out of a log file.
        _log.warning("routing decision log write failed: %s", type(exc).__name__)
        return False


def usage_for_session(session_id: str) -> "dict[str, Any] | None":
    """Join this profile's recorded accounting for one session. Never raises.

    Everything here is **read from ``state.db.session_model_usage``**, never
    recomputed: that table is already written per API call by the agent loop,
    and a second accounting path would drift from it.

    The first version selected only token counts, then labelled the record
    ``cost_status: "joined"`` while discarding the cost columns that were
    sitting in the same rows. It now reports what is actually recorded:

    * **actual and estimated cost stay separate.** They are different claims —
      a subscription-included call has a real charge of zero and an estimate
      that is not — and blending or relabelling them would be a lie about
      money.
    * **route identity is preserved.** A session that fell back across models
      or providers gets one ``routes`` entry per (model, provider, base_url,
      billing_mode), because a single flattened number hides which upstream
      actually served the work.
    * **the aggregate status is derived, not asserted.** One status across all
      rows is reported as-is; several become ``mixed``. It is never invented.

    Resolution is deliberately the *current* profile's ``state.db``: a run's
    terminal record is staged by the worker process, which IS that profile.
    Cross-profile joins are not attempted; a caller in the wrong process gets
    ``None`` rather than a wrong number.
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
            rows = conn.execute(
                """SELECT model, billing_provider, billing_base_url,
                          billing_mode, api_call_count, input_tokens,
                          output_tokens, cache_read_tokens, cache_write_tokens,
                          reasoning_tokens, estimated_cost_usd,
                          actual_cost_usd, cost_status, cost_source
                     FROM session_model_usage
                    WHERE session_id = ?
                    ORDER BY api_call_count DESC""",
                (text,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        _log.debug("routing usage join unavailable: %s", exc)
        return None
    if not rows:
        return None

    def _i(row, key):
        try:
            return int(row[key] or 0)
        except (TypeError, ValueError):
            return 0

    def _f(row, key):
        try:
            return float(row[key] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    totals = {k: 0 for k in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens", "api_call_count",
    )}
    estimated = 0.0
    actual = 0.0
    statuses: set = set()
    sources: set = set()
    routes: list = []
    for row in rows:
        for key in totals:
            totals[key] += _i(row, key)
        estimated += _f(row, "estimated_cost_usd")
        actual += _f(row, "actual_cost_usd")
        if row["cost_status"]:
            statuses.add(str(row["cost_status"]))
        if row["cost_source"]:
            sources.add(str(row["cost_source"]))
        routes.append({
            "model": row["model"],
            "billing_provider": row["billing_provider"],
            "billing_base_url": row["billing_base_url"],
            "billing_mode": row["billing_mode"],
            "api_call_count": _i(row, "api_call_count"),
            "input_tokens": _i(row, "input_tokens"),
            "output_tokens": _i(row, "output_tokens"),
            "cache_read_tokens": _i(row, "cache_read_tokens"),
            "cache_write_tokens": _i(row, "cache_write_tokens"),
            "reasoning_tokens": _i(row, "reasoning_tokens"),
            "estimated_cost_usd": _f(row, "estimated_cost_usd"),
            "actual_cost_usd": _f(row, "actual_cost_usd"),
            "cost_status": row["cost_status"],
            "cost_source": row["cost_source"],
        })

    if not statuses:
        aggregate = "unknown"
    elif len(statuses) == 1:
        aggregate = next(iter(statuses))
    else:
        aggregate = "mixed"

    return {
        **totals,
        "estimated_cost_usd": round(estimated, 10),
        "actual_cost_usd": round(actual, 10),
        "cost_status": aggregate,
        "cost_sources": sorted(sources),
        "routes": routes,
    }
