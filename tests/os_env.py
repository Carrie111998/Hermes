"""Platform-essential environment for tests that clear ``os.environ``.

Clearing the whole environment is a common way to prove a code path reads
nothing from it. On POSIX that is harmless, but on Windows a few variables are
load-bearing for the interpreter itself:

* ``SYSTEMROOT`` — OpenSSL cannot initialise without it, so *any* later
  ``ssl.SSLContext(...)`` raises ``SSLError: [SSL] unknown error``. Anything
  that constructs an HTTP client (aiohttp, lark_oapi, httpx) dies.
* ``USERPROFILE`` / ``HOMEDRIVE`` + ``HOMEPATH`` — without these ``Path.home()``
  raises ``RuntimeError: Could not determine home directory``.

Clearing them therefore fails tests for reasons that have nothing to do with
the behaviour under test, and only on Windows. Pass :data:`PLATFORM_ENV`
instead of an empty dict to keep the isolation while leaving the OS able to
function::

    @patch.dict(os.environ, PLATFORM_ENV, clear=True)

Every application/credential variable is still cleared — this whitelist holds
no Hermes, provider, or platform configuration.
"""

from __future__ import annotations

import os

_ESSENTIAL_NAMES = (
    # Windows interpreter/OS essentials.
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    # POSIX equivalents and cross-platform basics.
    "HOME",
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
)

#: Snapshot of the OS-essential variables present in this process.
PLATFORM_ENV: dict[str, str] = {
    name: os.environ[name] for name in _ESSENTIAL_NAMES if name in os.environ
}
