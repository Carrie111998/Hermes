"""Shared ANSI color utilities for Hermes CLI modules."""

import os
import sys


def should_use_color(stream=None) -> bool:
    """Return True when colored output is appropriate.

    Respects the NO_COLOR environment variable (https://no-color.org/)
    and TERM=dumb, in addition to the existing TTY check. Defaults to
    ``sys.stdout``; pass ``stream=sys.stderr`` when writing to stderr
    so the decision reflects that stream (e.g. a TTY terminal whose
    stderr is piped to a log file).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", None)
    return bool(isatty) and isatty()


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def color(text: str, *codes, stream=None) -> str:
    """Apply color codes to text (only when color output is appropriate).

    ``stream`` selects the stream the output will be written to (default
    ``sys.stdout``); the TTY check applies to that stream, so pass
    ``stream=sys.stderr`` for stderr writers.
    """
    if not should_use_color(stream=stream):
        return text
    return "".join(codes) + text + Colors.RESET
