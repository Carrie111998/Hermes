"""Hermetic isolation for the credential security tests.

WHY THIS EXISTS
---------------
The credential tripwire's whole job in production is to read the operator's
real credential files, so it can recognise those values and scrub them out of
tool results. That is correct in production and wrong in a test run: during
development of this packet, a failing assertion in a tripwire test printed
real (as it turned out, dead) credential strings from ``~/.zshrc`` into the
session transcript.

Measured before this fixture existed, a plain test run had the seeder opening
**18 real files** under the operator's home -- including ``~/.ssh`` private
keys, ``~/.docker/config.json`` and ``~/.config/gh/hosts.yml``.

The global ``tests/conftest.py::_hermetic_environment`` redirects HERMES_HOME
and scrubs credential-shaped env vars, but deliberately does NOT redirect HOME
-- redirecting it globally broke CI, because subprocesses inherit it
(``tests/conftest.py:264-269``). That same comment prescribes the fix used
here:

    "If a test genuinely needs HOME isolated, it should set it explicitly in
    its own fixture."

So this is a directory-scoped fixture, not a change to the global one.

FOUR VECTORS, ALL OF WHICH MUST BE CLOSED
-----------------------------------------
Closing HOME alone is not sufficient:

1. real home    -- expanduser("~") / Path.home(): denylist paths, shell rc
                   files, ~/.ssh, ~/.docker, ~/.config/gh
2. hermes home  -- get_hermes_home()
3. working dir  -- TERMINAL_CWD or os.getcwd(); the repo root contains a real
                   ``.envrc``, which is_credential_basename() classifies as
                   credential-bearing, so this vector is live, not theoretical
4. process env  -- os.environ entries with credential-shaped names
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Anything at or under this must never be read by a test in this directory.
#: pytest's own tmp dirs live outside it, so this is a safe absolute guard.
REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def real_home_paths(paths) -> list:
    """Return the subset of *paths* that live under the operator's real home.

    Used by the hermeticity regressions. Returns paths, never file contents.
    """
    out = []
    for p in paths:
        try:
            resolved = os.path.realpath(str(p))
        except OSError:
            resolved = str(p)
        if resolved == REAL_HOME or resolved.startswith(REAL_HOME + os.sep):
            # pytest tmp dirs can legitimately sit under the real home on some
            # platforms; those are not contamination.
            if "/pytest-" in resolved or "/tmp" in resolved:
                continue
            out.append(resolved)
    return out


def assert_seeder_is_hermetic():
    """Assert the tripwire seeder touches nothing under the real home.

    Deliberately reports PATHS ONLY. Rendering the seed set in an assertion
    message is the exact defect that caused the original disclosure, so this
    helper must never be rewritten to print values.
    """
    from agent.credential_tripwire import _seed_paths

    escaped = real_home_paths(_seed_paths())
    assert not escaped, (
        f"seeder escaped the synthetic root and would read "
        f"{len(escaped)} real file(s): {escaped}"
    )


@pytest.fixture(autouse=True)
def _hermetic_credential_environment(tmp_path, monkeypatch, _hermetic_environment):
    """Point every credential source root at a per-test synthetic tree.

    Depends on ``_hermetic_environment`` by name so it is guaranteed to run
    after the global fixture rather than racing it.
    """
    home = tmp_path / "synthetic_home"
    home.mkdir(exist_ok=True)

    # 1. Real home. Env vars cover expanduser(); the Path.home patch covers
    #    code that calls Path.home() directly, which env vars do not reach.
    #    classmethod(...) is the correct signature -- some sites in this repo
    #    use a bare lambda, which breaks on Path.home().
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # 2. Hermes home, inside the synthetic tree.
    hermes_home = home / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # 3. Working directory. The global fixture already deletes TERMINAL_CWD,
    #    so os.getcwd() is the live vector -- chdir closes it, and setting
    #    TERMINAL_CWD explicitly keeps the two consistent.
    monkeypatch.setenv("TERMINAL_CWD", str(home))
    monkeypatch.chdir(home)

    # 4. XDG. ~/.config/gh already follows HOME; set this too so a future
    #    XDG-aware source root cannot silently escape.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    # 5. Credential-shaped env vars. The global fixture scrubs these, but this
    #    is a security boundary in a security test -- do not depend on another
    #    fixture's name list staying complete.
    for name in list(os.environ):
        upper = name.upper()
        if any(tok in upper for tok in
               ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                "CREDENTIAL", "AUTH", "_KEY")):
            monkeypatch.delenv(name, raising=False)

    # 6. The tripwire caches its seed set on a generation key; clear it either
    #    side so no state crosses a test boundary in either direction.
    from agent import credential_tripwire

    credential_tripwire.reset_cache()
    yield home
    credential_tripwire.reset_cache()
