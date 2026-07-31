"""Bounded attestation mode for the AI Factory admission hook.

The Kanban dispatcher must prove, before it opens a gated Code worker's gate,
that the worker will actually arm the fail-closed admission hook. Reading the
profile config only proves what the profile *declares*; running a probe with
the dispatcher's own interpreter only proves the *dispatcher's* install.

This module is the probe, and it lives in the trusted tree on purpose: the
dispatcher invokes it **through the worker's real launcher** (``hermes -p
<profile> --factory-attest-admission <nonce>``), so the whole wrapper chain,
its environment mutations and the install it resolves are exercised exactly as
the worker will experience them. The verdict is echoed back with the caller's
challenge nonce so a stale or replayed line cannot be reused.

Nothing here is user-facing: the flag is intentionally undocumented, takes a
single opaque argument and prints one machine-readable line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: argv sentinel; deliberately verbose so it cannot collide with a real flag.
ATTEST_FLAG = "--factory-attest-admission"
#: Prefix of the single machine-readable output line.
VERDICT_PREFIX = "HERMES_FACTORY_ATTEST "


def find_attest_nonce(argv: list[str]) -> str | None:
    """Return the challenge nonce when argv requests attestation mode.

    Scans rather than indexing: the dispatcher passes ``-p <profile>`` first so
    the profile override applies before this runs.
    """
    for index, value in enumerate(argv):
        if value == ATTEST_FLAG:
            if index + 1 < len(argv):
                return argv[index + 1]
            return ""
    return None


def build_verdict(nonce: str) -> dict:
    """Run the worker's real config + hook-registration path once.

    Never raises: a failure is data the dispatcher must see, not a traceback it
    would have to parse out of stderr.
    """
    verdict: dict = {
        "nonce": nonce,
        "armed": [],
        "tree": None,
        "executable": sys.executable,
        "error": None,
    }
    try:
        import hermes_cli

        verdict["tree"] = str(Path(hermes_cli.__file__).resolve().parents[1])
        from agent.shell_hooks import register_from_config
        from hermes_cli.config import load_config

        specs = register_from_config(load_config(), accept_hooks=True)
        verdict["armed"] = [
            spec.command
            for spec in specs
            if getattr(spec, "event", None) == "pre_tool_call"
            and getattr(spec, "fail_closed", False)
            and "factory_admission_hook.py" in getattr(spec, "command", "")
            and "--require-owned-git" in getattr(spec, "command", "")
        ]
    except Exception as exc:  # pragma: no cover — reported, never raised
        verdict["error"] = f"{type(exc).__name__}: {exc}"
    return verdict


def run_attestation(nonce: str) -> int:
    """Print exactly one verdict line and exit successfully."""
    print(VERDICT_PREFIX + json.dumps(build_verdict(nonce)))
    return 0
