"""Regression tests for the deferred-callback env-resolution detector.

Each test locks one of the resolution rules that made the detector usable.
Two of them are dead ends that were already paid for once (see the script
docstring and GBrain ``concepts/import-time-hermes-home-snapshot-bug``):

* dropping the import-rename / local-alias edges makes the reference bug
  invisible — the detector then finds ZERO on an unfixed tree;
* allowing bare-name matching across modules returned 177 hits over 1129
  modules, almost all ``list.append`` / ``cursor.execute`` collisions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_deferred_env_resolution import scan


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip("\n"), encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A tree whose only env resolver + writer lives two modules away."""
    _write(
        tmp_path,
        "constants.py",
        """
from pathlib import Path


def get_hermes_home() -> Path:
    return Path("/real/home")
""",
    )
    _write(
        tmp_path,
        "diag/writer.py",
        """
from constants import get_hermes_home


def write_diag(tag):
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "diag.log", "a") as fh:
        fh.write(tag)
""",
    )
    return tmp_path


def test_follows_import_rename_and_local_alias(tree: Path) -> None:
    """The reference shape: two modules and two renames from the resolution."""
    _write(
        tree,
        "app/main.py",
        """
import atexit as _atexit

from diag.writer import write_diag as _diag


def run():
    _exit_diag = _diag

    def _atexit_hook():
        _exit_diag("atexit.hook")

    _atexit.register(_atexit_hook)
""",
    )

    result = scan(tree, include_threads=False)

    assert result.registrations == 1
    assert len(result.candidates) == 1
    hit = result.candidates[0]
    assert hit.rel == "app/main.py"
    assert hit.kind == "atexit"
    assert hit.callback == "_atexit_hook"
    assert hit.env.name == "get_hermes_home"
    # The rename edge is rendered, not collapsed -- that edge is exactly what
    # a grep-based search cannot cross.
    assert hit.env.chain == ["_atexit_hook", "_exit_diag(=write_diag)"]


def test_refuses_bare_name_matching_across_modules(tree: Path) -> None:
    """An unimported name must NOT bind to a same-named def in another module."""
    _write(
        tree,
        "app/main.py",
        """
import atexit


def cb():
    write_diag("atexit.hook")  # never imported -- unresolvable


atexit.register(cb)
""",
    )

    result = scan(tree, include_threads=False)

    assert result.registrations == 1
    assert result.candidates == []


def test_positive_control_for_the_refusal(tree: Path) -> None:
    """Same file as above plus the import edge -- proves the test is not vacuous."""
    _write(
        tree,
        "app/main.py",
        """
import atexit

from diag.writer import write_diag


def cb():
    write_diag("atexit.hook")


atexit.register(cb)
""",
    )

    result = scan(tree, include_threads=False)

    assert [c.callback for c in result.candidates] == ["cb"]


@pytest.mark.parametrize(
    "statement",
    [
        "records.append(str(home))",
        "handle.write(str(home))",
        "conn.commit()",
        "cursor.execute(str(home))",
        'str(home).replace("a", "b")',
    ],
)
def test_write_set_excludes_in_memory_lookalikes(tmp_path: Path, statement: str) -> None:
    """`.append` / `.write` / `.commit` / `.execute` / `.replace` are NOT writes."""
    _write(
        tmp_path,
        "app/main.py",
        f"""
import atexit

from pathlib import Path


def get_hermes_home():
    return Path("/real/home")


def cb():
    home = get_hermes_home()
    {statement}


atexit.register(cb)
""",
    )

    result = scan(tmp_path, include_threads=False)

    assert result.registrations == 1
    assert result.candidates == []


@pytest.mark.parametrize(
    "statement",
    [
        "home.mkdir(parents=True)",
        'home.write_text("x")',
        'home.write_bytes(b"x")',
        "home.touch()",
        "home.unlink()",
        "os.makedirs(home)",
        "os.replace(home, home)",
        "shutil.rmtree(home)",
        "shutil.copy(home, home)",
        "json.dump({}, home)",
        "sqlite3.connect(home)",
        'open(home, "a")',
    ],
)
def test_write_set_covers_the_narrow_list(tmp_path: Path, statement: str) -> None:
    _write(
        tmp_path,
        "app/main.py",
        f"""
import atexit
import json
import os
import shutil
import sqlite3

from pathlib import Path


def get_hermes_home():
    return Path("/real/home")


def cb():
    home = get_hermes_home()
    {statement}


atexit.register(cb)
""",
    )

    result = scan(tmp_path, include_threads=False)

    assert [c.callback for c in result.candidates] == ["cb"], statement


def test_detects_finalize_signal_and_del(tree: Path) -> None:
    _write(
        tree,
        "app/main.py",
        """
import signal
import weakref

from diag.writer import write_diag


class Holder:
    def __del__(self):
        write_diag("del")


def _on_signal(signum, frame):
    write_diag("signal")


def _on_finalize():
    write_diag("finalize")


def arm(obj):
    weakref.finalize(obj, _on_finalize)
    signal.signal(signal.SIGTERM, _on_signal)
""",
    )

    result = scan(tree, include_threads=False)

    assert {c.kind for c in result.candidates} == {"__del__", "finalize", "signal"}


def test_threads_are_opt_in(tree: Path) -> None:
    _write(
        tree,
        "app/main.py",
        """
import threading

from diag.writer import write_diag


def _loop():
    write_diag("thread")


def arm():
    threading.Thread(target=_loop, daemon=True).start()
""",
    )

    assert scan(tree, include_threads=False).candidates == []
    assert [c.kind for c in scan(tree, include_threads=True).candidates] == ["thread"]


def test_skip_list_is_applied_relative_to_the_repo_root(tmp_path: Path) -> None:
    """A checkout living under ``.claude/worktrees/`` must still be scanned.

    Matching the skip list against the ABSOLUTE path makes ``.claude`` swallow
    the repo root: the scan walks zero files and reports success.
    """
    root = tmp_path / ".claude" / "worktrees" / "some-worktree"
    _write(
        root,
        "app/main.py",
        """
import atexit

from pathlib import Path


def get_hermes_home():
    return Path("/real/home")


def cb():
    get_hermes_home().mkdir(parents=True)


atexit.register(cb)
""",
    )

    result = scan(root, include_threads=False)

    assert result.discovered == 1
    assert [c.callback for c in result.candidates] == ["cb"]


def test_nested_skip_dirs_are_still_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/test_thing.py",
        """
import atexit

from pathlib import Path


def get_hermes_home():
    return Path("/real/home")


def cb():
    get_hermes_home().mkdir(parents=True)


atexit.register(cb)
""",
    )

    result = scan(tmp_path, include_threads=False)

    assert result.discovered == 0
    assert result.candidates == []
