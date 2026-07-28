"""H-25 — writing a project .env must be denied, exactly as reading one is.

agent/file_safety.py defines _BLOCKED_PROJECT_ENV_BASENAMES and consults it in
exactly one place: get_read_block_error. The write side --
build_write_denied_paths / _classify_write_denial -- never looked at it, and
file_tools._check_sensitive_path covers only system prefixes and the Hermes
config.

So the agent could not READ your .env but could silently REPLACE it. The
natural single-call implementation of "the app needs a STRIPE_KEY, add it to
.env" is write_file(".env", "STRIPE_KEY=...\n"), which overwrites
DATABASE_URL, live API keys and OAuth secrets with one line -- no prompt, no
backup. The identical `echo ... > .env` through the terminal tool is classified
dangerous and gated.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agent.file_safety import (
    _BLOCKED_PROJECT_ENV_BASENAMES,
    _classify_write_denial,
    get_read_block_error,
)


@pytest.mark.parametrize("basename", sorted(_BLOCKED_PROJECT_ENV_BASENAMES))
def test_every_blocked_env_basename_is_write_denied(basename):
    d = tempfile.mkdtemp()
    p = os.path.join(d, basename)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("DATABASE_URL=postgres://real\n")
    assert _classify_write_denial(p) is not None, (
        f"{basename} is readable-blocked but writable -- it can be destroyed"
    )


def test_read_and_write_protection_agree():
    """The asymmetry was the whole bug: never block one direction only."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".env")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("SECRET=x\n")
    assert bool(get_read_block_error(p)) is True
    assert bool(_classify_write_denial(p)) is True


def test_case_insensitive_on_this_platform():
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".ENV")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("SECRET=x\n")
    assert _classify_write_denial(p) is not None


@pytest.mark.parametrize("basename", [
    ".env.example", ".env.sample", ".env.template",
    "env.py", "environment.yml", "README.md", "settings.env.json",
])
def test_lookalike_files_stay_writable(basename):
    """Templates and docs are the files people legitimately generate; blocking
    them would push the agent to shell redirection and defeat the gate."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, basename)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("x\n")
    assert _classify_write_denial(p) is None, f"{basename} should stay writable"


def test_nonexistent_env_is_still_denied():
    """Creating a .env is how you clobber one that appears later; and the read
    side does not require existence either."""
    d = tempfile.mkdtemp()
    assert _classify_write_denial(os.path.join(d, ".env")) is not None
