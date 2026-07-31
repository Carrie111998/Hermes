"""Bounded attestation mode for the AI Factory admission hook.

The Kanban dispatcher must prove, before it opens a gated Code worker's gate,
that the worker will actually arm the fail-closed admission hook. Reading the
profile config only proves what the profile *declares*; running a probe with
the dispatcher's own interpreter only proves the *dispatcher's* install.

This module is the probe, and it lives in the trusted tree on purpose: the
dispatcher invokes it **through the worker's own launcher argv**
(``<launcher prefix> -p <profile> --factory-attest-admission <nonce>``), so the
whole wrapper chain, its environment mutations and the install it resolves are
exercised exactly as the worker will experience them. The verdict is echoed
back with the caller's challenge nonce so a stale or replayed line cannot be
reused.

The mode is recognized only in an exact, closed argv grammar. A worker task can
carry arbitrary text — a prompt, a model name, a skill — and any of it could
equal the sentinel; treating a match anywhere in argv as a mode switch would
let ordinary task data short-circuit the worker into attest mode and exit 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: argv sentinel; deliberately verbose so it cannot collide with a real flag.
ATTEST_FLAG = "--factory-attest-admission"
#: Prefix of the single machine-readable output line.
VERDICT_PREFIX = "HERMES_FACTORY_ATTEST "
#: The only options allowed to precede the sentinel, with their one value.
_ALLOWED_LEADING_OPTIONS = {"-p", "--profile"}


def find_attest_nonce(argv: list[str]) -> str | None:
    """Return the challenge nonce only for an exact attest invocation.

    Grammar (``argv`` is ``sys.argv[1:]``)::

        [(-p|--profile) VALUE] --factory-attest-admission NONCE

    Nothing may follow the nonce, the sentinel may appear once, and the nonce
    must be a non-empty value that is not itself the sentinel. Any other shape
    — including the sentinel appearing as a task value, after ``--``, or as the
    argument of another option — is ordinary data and returns ``None``.
    """
    if not isinstance(argv, list):
        return None
    index = 0
    while index < len(argv) and argv[index] in _ALLOWED_LEADING_OPTIONS:
        # The option must carry a value that is not the sentinel itself.
        if index + 1 >= len(argv) or argv[index + 1] == ATTEST_FLAG:
            return None
        index += 2
    if index >= len(argv) or argv[index] != ATTEST_FLAG:
        return None
    # Exactly one nonce, and it must terminate the command line.
    if len(argv) != index + 2:
        return None
    nonce = argv[index + 1]
    if not nonce or nonce == ATTEST_FLAG:
        return None
    return nonce


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
        "prefix": sys.prefix,
        # Reported, never assumed: the dispatcher refuses a verdict produced
        # without safe-path semantics, because the worker's own worktree would
        # then be able to shadow the trusted install.
        "safe_path": bool(getattr(sys.flags, "safe_path", False)),
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
