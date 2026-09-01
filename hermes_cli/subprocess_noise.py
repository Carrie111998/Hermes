"""Filter for known-benign subprocess stderr noise (issue #54833).

On macOS, short-lived children of the Hermes runtime occasionally print the
libmalloc/MallocStackLogging teardown line

    Python(123) MallocStackLogging: can't turn off malloc stack logging
    because it was not enabled.

to stderr as they exit.  The message is emitted by Apple's private
MallocStackLogging framework during process teardown; it is harmless, but
because every Hermes stderr boundary (launchd wrapper log, execute-code tool
results, Desktop backend log, plain terminal inheritance) forwards child
stderr verbatim, it pollutes ``gateway.error.log``, ``desktop.log``, user
terminals, and model context.

The root toggle lives inside macOS internals, not Hermes, so this module is a
containment seam, not a source fix: it suppresses ONLY the exact whole line,
ONLY on Darwin, at the capture boundaries that already own the text.  Every
other byte — including other MallocStackLogging diagnostics, the same sentence
embedded in a traceback or a tool payload, and all stdout — is preserved.

Keep this module import-cheap (stdlib only) and free of logging so it can be
used from the launchd stderr wrapper before the gateway is up.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

# The one known-benign sentence.  The optional "<name>(<pid>) " prefix is the
# os.asprintf banner macOS prepends (seen both as "Python(56273) ..." from
# Framework Python.app and "python(16414) ..." from console interpreters);
# everything after it must match byte-for-byte.
_BENIGN_MALLOC_STACK_LOGGING_RE = re.compile(
    r"^(?:[Pp]ython\([0-9]+\) )?"
    r"MallocStackLogging: can't turn off malloc stack logging "
    r"because it was not enabled\.$"
)


def is_benign_darwin_malloc_stack_logging_line(
    line: str,
    *,
    platform: Optional[str] = None,
) -> bool:
    """Return True only for the exact known-benign libmalloc teardown line.

    ``platform`` is a test seam defaulting to ``sys.platform``; it is not a
    user setting.  Matching is done after stripping line endings only, so a
    line with any other prefix, suffix, or wording never matches.
    """
    if (platform if platform is not None else sys.platform) != "darwin":
        return False
    # splitlines() on the single line removes a trailing \r\n / \n / \r
    # without touching inner content; strip() would eat meaningful whitespace.
    core = line.splitlines()[0] if line else ""
    return bool(_BENIGN_MALLOC_STACK_LOGGING_RE.match(core))


def filter_benign_darwin_subprocess_stderr(
    text: str,
    *,
    platform: Optional[str] = None,
) -> str:
    """Remove only whole benign-malloc-logging lines from captured stderr.

    * Non-Darwin hosts return the input unchanged (same object — this sits on
      hot paths for Linux/Windows users and must cost nothing).
    * ``splitlines(keepends=True)`` guarantees that deleting one line can
      never splice neighbouring diagnostics together.
    """
    if (platform if platform is not None else sys.platform) != "darwin":
        return text
    if not text:
        return text
    if "MallocStackLogging" not in text:
        # Fast bail: the marker is present in every matchable line.
        return text
    kept = [
        chunk
        for chunk in text.splitlines(keepends=True)
        if not is_benign_darwin_malloc_stack_logging_line(
            chunk, platform=platform
        )
    ]
    return "".join(kept)
