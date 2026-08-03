"""Full MoA turn trace persistence (opt-in via config ``moa.save_traces``).

When enabled, every Mixture-of-Agents turn that actually runs the reference
fan-out (a cache MISS in ``MoAChatCompletions.create``) appends one JSON line
to ``<hermes_home>/moa-traces/<session_id>.jsonl``. The record is the TRUE
FULL turn — the exact messages array each reference model received (system
prompt + advisory view, not the truncated display preview), each reference's
full output, and the exact messages array the aggregator received (including
the injected reference-context guidance block) plus its output when available
— so a run can be audited end-to-end offline: what every model saw, what every
model said, and what it cost.

This is a side-channel trace. It is NOT the conversation ``messages`` table and
never enters message history or replay — MoA references are advisory side-calls
with their own system prompt, not conversation turns, so persisting them as
message rows would corrupt role alternation / replay. Traces live in their own
files, keyed by session id, and are safe to delete.

Cost model note: gated OFF by default. When off, the only overhead is the
``_traces_enabled()`` config read (cheap) — no file I/O, no serialization.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _traces_enabled_and_dir() -> Optional[Path]:
    """Return the trace directory if ``moa.save_traces`` is on, else None.

    Reads config lazily per call (config is cheap to load and this only runs on
    a cache-MISS MoA turn, i.e. once per user turn, not per tool iteration).
    ``moa.trace_dir`` overrides the default ``<hermes_home>/moa-traces/``.
    """
    try:
        from hermes_cli.config import load_config

        moa_cfg = (load_config() or {}).get("moa") or {}
    except Exception:  # pragma: no cover - defensive: never break a turn over tracing
        return None
    if not moa_cfg.get("save_traces"):
        return None
    override = moa_cfg.get("trace_dir")
    if override:
        base = Path(os.path.expandvars(os.path.expanduser(str(override))))
    else:
        base = get_hermes_home() / "moa-traces"
    return base


def _sanitize_session_id(session_id: Optional[str]) -> str:
    """Make a session id safe as a filename component."""
    if not session_id:
        return "unknown-session"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session_id))


def _managed_install() -> bool:
    """True when a package manager (NixOS) owns this install's modes.

    Mirrors the check ``hermes_cli.config._secure_dir`` makes internally, read
    here so the *creation* mode honours the same carve-out as reconciliation.
    Import is local and failure means "not managed": an unimportable config
    module is the single-user source-install case, where 0700 is correct.

    Why the trace dir needs the carve-out at creation: ``nix/nixosModules.nix``
    runs the gateway with ``UMask = "0007"`` precisely so "files created by the
    gateway should be group-writable so interactive users in the hermes group
    can read/write them", pins ``stateDir/.hermes`` to 2770 (setgid,
    group-rwx), and avoids ``chown -R`` to keep the setgid bit alive "for group
    access by hostUsers" — who get a ``~/.hermes`` symlink to that same
    stateDir. Gateway and interactive CLI therefore share one ``$HERMES_HOME``,
    so a 0700 trace dir created by whichever ran first locks the other out with
    EACCES: the tracing feature itself, not just its permissions. Omitting the
    mode lets the inherited setgid + umask land 2770, matching
    ``ensure_hermes_home``'s managed branch and its ``logs/curator`` lazy-mkdir
    precedent in ``hermes_cli.config``.

    Deliberately a local helper. ``hermes_cli.config.secure_mkdir`` (PR #77655)
    centralises exactly this branch, but it is not on ``main`` yet, so importing
    it would couple this PR's merge to that one. Consolidate when it lands.
    """
    try:
        from hermes_cli.config import is_managed

        return bool(is_managed())
    except Exception:  # pragma: no cover - defensive
        return False


def _slot_trace(acct: Any, label: str) -> dict[str, Any]:
    """Render one reference's _RefAccounting into a full trace dict.

    Includes the FULL input messages the reference received and its FULL
    output — not the truncated display preview.
    """
    usage = getattr(acct, "usage", None)
    usage_dict: dict[str, Any] = {}
    if usage is not None:
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_tokens", 0),
            "cache_write_tokens": getattr(usage, "cache_write_tokens", 0),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", 0),
        }
    return {
        "label": label,
        "model": getattr(acct, "model", None),
        "provider": getattr(acct, "provider", None),
        "temperature": getattr(acct, "temperature", None),
        "input_messages": getattr(acct, "messages", None),
        "output": getattr(acct, "output", None),
        "usage": usage_dict,
        "cost_usd": getattr(acct, "cost_usd", None),
        "cost_status": getattr(acct, "cost_status", None),
        "cost_source": getattr(acct, "cost_source", None),
    }


def save_moa_turn(
    *,
    session_id: Optional[str],
    preset_name: str,
    reference_outputs: list[tuple[str, str, Any]],
    aggregator_label: str,
    aggregator_model: Optional[str],
    aggregator_provider: Optional[str],
    aggregator_temperature: Any,
    aggregator_input_messages: Any,
    aggregator_output: Optional[str],
    aggregator_streamed: bool,
) -> None:
    """Append one full MoA turn record to the session's trace JSONL, if enabled.

    Best-effort: any failure is logged at debug and swallowed — tracing must
    never break a live turn. Called once per turn on a reference cache MISS.

    ``aggregator_output`` is the aggregator's synthesized text. On the
    non-streaming path (eval / quiet-mode / subagents) it was captured inline
    at call time. On the streaming path it is captured after the fact from the
    caller's resolved assistant text (``aggregator_output_fallback`` in
    ``consume_and_save_trace``) so the trace is self-contained either way; if
    that resolved text was unavailable, it falls back to None and the record
    points at the session store via ``output_location``.
    """
    base = _traces_enabled_and_dir()
    if base is None:
        return
    try:
        # Trace records embed the full messages every reference model saw, so
        # create the directory owner-only and the JSONL owner-only on first
        # write. Two limits on that ``mode``, both deliberate:
        #
        # * It applies only to the *final* component. ``Path.mkdir(parents=
        #   True, mode=…)`` creates intermediates at the umask default, so a
        #   ``moa.trace_dir`` override of ``a/b/traces`` leaves ``a`` and ``b``
        #   at 0755 (measured). Harmless — the leaf stays 0700 and the JSONL
        #   0600, so traversal into the traces themselves is still blocked —
        #   but it is not the blanket "dirs this call creates" guarantee the
        #   original comment claimed.
        # * An existing dir keeps its permissions (``exist_ok=True`` does not
        #   re-apply ``mode``), matching ``open_private_append``'s create-only
        #   contract: never fight a mode the user widened on purpose.
        #
        # Managed mode is carved out at creation, not just at reconciliation.
        # ``moa-traces`` is NOT among the dirs ``nix/nixosModules.nix``
        # pre-creates via ``systemd.tmpfiles`` (stateDir, .hermes, cron,
        # sessions, logs, memories, plugins — all 2770), so on a managed host it
        # is always made lazily here and this ``mode`` is the only thing setting
        # it. See ``_managed_install`` for why forcing 0700 there breaks the
        # module's hermes-group sharing. ``HERMES_HOME_MODE`` is deliberately
        # not honoured: that hatch exists so a web server can *traverse*
        # HERMES_HOME to reach a served subdir, and nothing is served from here.
        if _managed_install():
            base.mkdir(parents=True, exist_ok=True)
        else:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = base / f"{_sanitize_session_id(session_id)}.jsonl"
        # output_location tells an offline reader where the acting text lives:
        # embedded here when we have it (both non-streaming inline capture and
        # streaming after-the-fact capture), else the session-db assistant row.
        _have_output = bool(aggregator_output)
        if not aggregator_streamed:
            _output_location = "inline"
        elif _have_output:
            _output_location = "inline_from_stream"
        else:
            _output_location = "assistant_message_in_session_db"
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "preset": preset_name,
            "references": [
                _slot_trace(acct, label)
                for label, _text, acct in reference_outputs
            ],
            "aggregator": {
                "label": aggregator_label,
                "model": aggregator_model,
                "provider": aggregator_provider,
                "temperature": aggregator_temperature,
                "input_messages": aggregator_input_messages,
                "output": aggregator_output,
                "streamed": aggregator_streamed,
                # Where the aggregator's acting output lives for this record.
                # "inline"             — non-streaming inline capture
                # "inline_from_stream" — streamed, then captured from the
                #                        caller's resolved assistant text
                # "assistant_message_in_session_db" — streamed and the resolved
                #                        text was unavailable at flush time
                "output_location": _output_location,
            },
        }
        from utils import open_private_append

        with open_private_append(path) as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - tracing must never break a turn
        logger.debug("MoA trace write failed (session=%s): %s", session_id, exc)
