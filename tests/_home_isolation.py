"""Helper for tests that need the user's home directory isolated.

Setting ``$HOME`` alone does **not** isolate a test on Windows. Production
code resolves the home directory through ``Path.home()`` /
``os.path.expanduser("~")``, and on Windows those read ``USERPROFILE``
(``ntpath.expanduser``) and ignore ``HOME`` entirely. ``HOME`` is usually not
even set there. A test that pins only ``HOME`` therefore keeps resolving the
developer's *real* home, and any code under test that writes to
``~/.something`` overwrites live config on the developer's machine.

Redirect the env vars *and* the resolver, so both mechanisms land in the
tempdir regardless of platform.

This is the ``$HOME`` sibling of the ``HERMES_HOME`` rule in ``conftest.py``:
code reading ``~/.hermes/*`` via ``Path.home() / ".hermes"`` instead of the
canonical ``get_hermes_home()`` is a bug to fix at the callsite. This helper is
for the genuinely user-level paths (``~/.honcho``, ``~/.local/bin``,
``~/.hindsight``) where ``Path.home()`` is the correct production call.
"""

from __future__ import annotations

from pathlib import Path


def redirect_home(monkeypatch, path) -> Path:
    """Point every home-resolution mechanism at *path*.

    Covers ``$HOME`` (POSIX), ``USERPROFILE`` / ``HOMEDRIVE`` + ``HOMEPATH``
    (Windows ``os.path.expanduser``), and ``Path.home()`` itself. Returns the
    redirected path for convenience.
    """
    home = Path(path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # ntpath.expanduser falls back to HOMEDRIVE+HOMEPATH when USERPROFILE is
    # absent; drop them so a stale pair can't out-vote the redirect. With
    # USERPROFILE (Windows) and HOME (POSIX) both pointed at the tempdir,
    # os.path.expanduser("~") follows without needing to be patched itself.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
