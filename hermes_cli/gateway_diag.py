"""Gateway lifecycle diagnostics (``logs/gateway-exit-diag.log``).

Deliberately stdlib-only and import-cheap: the whole point of this module is
to be importable at the very top of ``hermes_cli.main``'s module body, before
the ~50s of CLI startup (heavy imports + plugin discovery) that precedes
``run_gateway()``. Importing ``hermes_cli.gateway`` there instead would cost
seconds and defeat the purpose, so the writer lives here and both callers
share it.

Two records bracket that startup cost:

``gateway.spawn``
    Written from ``hermes_cli.main``'s module body, right after
    ``_apply_profile_override()`` resolves ``HERMES_HOME`` (the earliest point
    at which the log's location is even knowable). Minimal by necessity —
    nothing but the interpreter and raw argv is known yet.

``gateway.start``
    Written at ``run_gateway()`` entry with the fuller field set, and the
    record double-spawn detection keys on: a PAIR of ``gateway.start``
    records for a single launch is the signature. ``gateway.spawn`` uses a
    distinct tag precisely so it cannot perturb that pairing.

Everything here is best-effort by contract: a diagnostic must never be able
to break the gateway, so all failures are swallowed. Opt out entirely with
``HERMES_GATEWAY_EXIT_DIAG=0``.
"""

from __future__ import annotations

import os
import sys

DIAG_ENV_VAR = "HERMES_GATEWAY_EXIT_DIAG"
DIAG_LOG_NAME = "gateway-exit-diag.log"


def diag_enabled() -> bool:
    """Whether lifecycle records should be written (default: on)."""
    return os.environ.get(DIAG_ENV_VAR, "1") == "1"


def write_diag(tag: str, **extra: object) -> None:
    """Append one JSON lifecycle record to ``logs/gateway-exit-diag.log``."""
    if not diag_enabled():
        return
    try:
        import json
        from datetime import datetime, timezone

        from hermes_constants import get_hermes_home

        log_dir = get_hermes_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            **extra,
        }
        with open(log_dir / DIAG_LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass  # never let the diagnostic itself crash the gateway


def process_start_age_s() -> float | None:
    """Seconds between this process's creation and now, or None if unknown.

    Stamped onto the lifecycle records so each one self-documents how much
    boot latency preceded it — the residual blind spot is then readable from
    the log itself instead of needing a live ``Get-CimInstance Win32_Process``
    correlation after the fact.
    """
    try:
        import time

        import psutil  # type: ignore

        return round(time.time() - psutil.Process(os.getpid()).create_time(), 3)
    except Exception:
        return None


def argv_selects_gateway_run(argv: list[str]) -> bool:
    """Whether ``argv`` invokes ``hermes [flags] gateway [run] [flags]``.

    Used to decide whether a process importing ``hermes_cli.main`` is on its
    way to becoming a gateway. Raw-argv matching is unavoidable here: the
    record has to be written long before argparse runs.

    The asymmetry is deliberate. A false negative loses one diagnostic line.
    A false positive writes one spurious ``gateway.spawn`` line — and since
    the tag is distinct from ``gateway.start``, it cannot manufacture the
    double-spawn signature either way. Nothing here affects control flow.
    """
    try:
        args = list(argv[1:])
        gateway_at = args.index("gateway")
    except (ValueError, IndexError, TypeError):
        return False
    for token in args[gateway_at + 1 :]:
        if token.startswith("-"):
            continue  # `gateway -v run`
        return token == "run"
    return True  # bare `gateway` defaults to the run subcommand


def emit_gateway_spawn_diag(argv: list[str]) -> bool:
    """Write the earliest-possible ``gateway.spawn`` record for a gateway run.

    Returns whether a record was written, so the behavior is testable without
    reaching into ``hermes_cli.main``'s module body. ``argv`` is passed in (and
    logged) rather than read from ``sys.argv`` because
    ``_apply_profile_override()`` strips ``--profile`` from ``sys.argv`` before
    any of this runs: logging the pre-strip copy keeps the record honest about
    how the process was actually launched.
    """
    if not argv_selects_gateway_run(argv):
        return False
    write_diag("gateway.spawn", argv=list(argv), proc_age_s=process_start_age_s())
    return True
