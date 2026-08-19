"""Pre-argparse fast path for ``hermes send``.

Why this module exists
----------------------
``hermes_cli/send_cmd.py`` is already careful: it imports
``tools.send_message_tool`` *inside* ``cmd_send``, so importing
``hermes_cli.send_cmd`` on its own costs only ~12 modules over the bare
interpreter floor.

That care was defeated upstream. ``hermes_cli/main.py`` is a 15k-line module
that, at module scope, imported all ~38 ``build_*_parser`` subcommand modules
and then loaded ``hermes_cli.config`` / ``env_loader`` and called
``setup_logging()`` -- which starts a QueueListener *thread* -- before argparse
had any idea which subcommand was being run. ``hermes send --help`` therefore
imported **444 modules**.

The cost is not merely cosmetic. Windows Task Scheduler's default
``<Priority>7</Priority>`` is BelowNormal and is inherited by child processes,
so this import work starves under scheduler contention: 388.2s cold / 458.0s
warm at Priority 7 versus 8.8s / 4.0s at Priority 5, both exiting 0. That is
what made ``hermes send`` appear to "hang" when fired from a scheduled task.
Raising task priority is a saturated lever on this box, so the import graph is
the remaining one.

Design
------
The same shape as the ``_try_termux_fast_cli_launch()`` hook already at the top
of ``main()``: look at argv, and if we can service the request without the full
parser, do it and tell the caller we handled it.

The trigger is deliberately **narrow**: ``sys.argv[1]`` must be exactly
``send``. Any leading top-level flag (``hermes -m gpt5 send ...``) falls through
to the full parser, which understands those flags. This is the conservative
direction: over-triggering would change argument parsing behaviour, whereas
under-triggering merely costs the old startup time. ``--profile``/``-p`` is
*not* a problem despite preceding the subcommand, because
``_apply_profile_override()`` strips it from ``sys.argv`` before this runs --
which is also why ``main.py`` must call us only *after* that.

``HERMES_NO_FAST_SEND=1`` disables the fast path entirely, restoring the
original full-parser route without a rollback.

Regression tests: ``tests/hermes_cli/test_send_import_cost.py``.
"""

from __future__ import annotations

import os
import sys

# Top-level flags that take a value. Needed by ``first_positional_argv`` so
# that in ``hermes -m gpt5 chat``, ``gpt5`` is correctly skipped as a flag value
# rather than misclassified as a subcommand. Kept in sync with the top-level
# flags declared in ``hermes_cli/_parser.py``.
#
# Correctness-safe either way: a missing entry only makes the caller's
# fast-path bail out too eagerly (plugin discovery runs when it need not);
# an extra entry would make us skip a real positional.
#
# Lives here rather than in ``main.py`` so that this module -- and the cheap
# argv inspection it provides -- carries no dependency on the 15k-line module.
# ``main.py`` re-imports ``first_positional_argv``, so
# ``hermes_cli.main._first_positional_argv`` keeps resolving unchanged. The flag
# table itself has no callers outside this module.
TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "-z", "--oneshot",
        "-m", "--model",
        "--provider",
        "-t", "--toolsets",
        "-r", "--resume",
        "-s", "--skills",
        "--usage-file",
        # ``-c / --continue`` is nargs='?' (optional value). Treat it as
        # value-taking: if the next token is a subcommand-looking word the user
        # almost certainly meant it as the session name, and either
        # interpretation keeps us on the safe side.
        "-c", "--continue",
    }
)


def first_positional_argv() -> str | None:
    """Return the first non-flag, non-flag-value token in ``sys.argv[1:]``.

    Used to decide whether plugin discovery has to run at argparse-setup time.
    Handles common invocations like ``hermes -m gpt5 --provider openai chat
    "msg"`` by skipping the values attached to known top-level flags.

    Does NOT fully simulate argparse -- unknown ``--foo=bar`` / ``--foo bar``
    flags degrade gracefully (``bar`` may be wrongly classified as a
    positional, which at worst forces a one-time plugin discovery).
    """
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # Everything after ``--`` is positional.
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("-"):
            # ``--flag=value`` carries its value inline -- single token.
            if "=" in tok:
                i += 1
                continue
            if tok in TOP_LEVEL_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        return tok
    return None


def _fast_send_applies() -> bool:
    """True when this invocation is a plain ``hermes send ...``.

    Narrow on purpose -- see the module docstring. ``sys.argv[1]`` must be
    exactly ``send``; anything with leading top-level flags goes the slow way.
    """
    if os.environ.get("HERMES_NO_FAST_SEND", "").strip().lower() in {"1", "true", "yes"}:
        return False
    # ``main.py`` calls us from module scope, so a test process that merely
    # ``import hermes_cli.main`` while its own argv happens to be ``[..., "send"]``
    # would otherwise try to deliver a message. The fast path exists purely for
    # the real CLI entry point; under pytest the full parser is what tests mean.
    # (This module's own tests drive real subprocesses, so they are unaffected.)
    if "pytest" in sys.modules:
        return False
    return sys.argv[1:2] == ["send"]


class _FastSendParseError(Exception):
    """Raised instead of argparse's print-usage-and-exit on a bad argument."""


def _make_parser():
    """Build a throwaway top-level parser carrying only the ``send`` subparser.

    ``prog="hermes"`` so the subparser's usage line renders as
    "usage: hermes send ..." -- byte-identical to the full-parser route.

    ``error()`` is overridden to raise rather than print. The stand-in parser
    only knows about ``send``, so its top-level usage banner would read
    ``usage: hermes {send} ...`` where the real CLI prints the full command
    list. Rather than approximate that banner, a parse error aborts the fast
    path entirely and lets ``main()`` rebuild the real parser, which then emits
    the canonical message. Errors are not the hot path, so paying full startup
    cost there is free -- and it keeps stderr byte-identical.
    """
    import argparse

    from hermes_cli.send_cmd import register_send_subparser

    class _Parser(argparse.ArgumentParser):
        def error(self, message):  # noqa: D102 - argparse override
            raise _FastSendParseError(message)

    parser = _Parser(prog="hermes", add_help=False)
    subparsers = parser.add_subparsers(dest="command", parser_class=_Parser)
    register_send_subparser(subparsers)
    return parser


def try_fast_send() -> bool:
    """Service ``hermes send ...`` without building the full CLI parser.

    Returns ``True`` when the invocation was handled (the caller must stop),
    ``False`` when it must fall through to the normal ``main()`` path.

    ``cmd_send`` signals its exit status with ``sys.exit`` (0 ok, 1 delivery
    failure, 2 usage error) and argparse's own ``--help`` / usage errors raise
    ``SystemExit`` too. Both are allowed to propagate untouched so exit codes
    stay exactly what the slow path produced.

    Any *other* exception means the fast path itself is broken rather than the
    send failing. Returning ``False`` there makes main() rebuild the real
    parser and try again, so a bug here degrades to the old slow behaviour
    instead of breaking ``hermes send`` outright.
    """
    if not _fast_send_applies():
        return False

    try:
        parser = _make_parser()
        args = parser.parse_args(sys.argv[1:])
    except SystemExit:
        # argparse's ``--help`` path: it printed and wants to exit. That output
        # is verified byte-identical to the slow path, so let it through.
        raise
    except Exception:
        # _FastSendParseError, or a genuine bug in the fast path. Either way,
        # fall through to the full parser rather than guessing.
        return False

    args.func(args)
    return True


__all__ = ["TOP_LEVEL_VALUE_FLAGS", "first_positional_argv", "try_fast_send"]
