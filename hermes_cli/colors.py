"""Shared ANSI color utilities for Hermes CLI modules."""

import os
import sys

if sys.platform == "win32":
    try:
        import ctypes
        _h = ctypes.windll.kernel32.GetStdHandle(-11)
        _m = ctypes.c_uint32()
        ctypes.windll.kernel32.GetConsoleMode(_h, ctypes.byref(_m))
        ctypes.windll.kernel32.SetConsoleMode(_h, _m.value | 0x0004)
    except Exception:
        pass


def should_use_color() -> bool:
    """Return True when colored output is appropriate.

    Respects the NO_COLOR environment variable (https://no-color.org/)
    and TERM=dumb, in addition to the existing TTY check.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if not sys.stdout.isatty():
        return False
    return True


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


def color(text: str, *codes) -> str:
    """Apply color codes to text (only when color output is appropriate)."""
    if not should_use_color():
        return text
    return "".join(codes) + text + Colors.RESET
    