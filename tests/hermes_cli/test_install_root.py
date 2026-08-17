"""Tests for hermes_cli.install_root — WHICH checkout a spawned gateway runs.

2026-08-17: ``hermes gateway restart`` launched from an agent worktree deployed
THAT worktree. ``python -m hermes_cli.main`` puts the caller's cwd on
``sys.path[0]``, so ``hermes_cli`` resolved from the worktree, and every path
that derived the module root from ``__file__`` (``PROJECT_ROOT``) then
propagated the worktree into the spawned gateway's cwd and PYTHONPATH. The
gateway ran the worktree's commits (~10 behind main) and the worktree reaper was
free to delete the running gateway's module root. It was silent: a worktree is a
real checkout, so every health check passed.

``installed_package_root()`` answers the question ``__file__`` cannot: where was
this distribution INSTALLED, regardless of which copy of the source tree the
current process happens to be executing.
"""

import sys
from pathlib import Path

import pytest

import hermes_cli.install_root as install_root


class _FakeEditableFinder:
    """Stand-in for setuptools' generated ``__editable___*_finder``.

    The real object is a class on ``sys.meta_path`` carrying a ``MAPPING``
    class attribute: ``{top_level_package: absolute source directory}`` as
    recorded by ``pip install -e``. That mapping is the authoritative record of
    the install root — unlike ``sys.path``, it cannot be shadowed by a cwd.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self.MAPPING = mapping

    def find_spec(self, fullname, path=None, target=None):
        return None


def _make_checkout(root: Path) -> Path:
    """Create a directory that looks like a Hermes source/install root."""
    pkg = root / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    return root


def _set_editable_finders(monkeypatch, *fakes: _FakeEditableFinder) -> None:
    """Control editable resolution without unloading the real import system.

    Drops THIS venv's own editable finder (its MAPPING points at the real
    checkout) and prepends the fakes, but keeps ``PathFinder`` and friends so a
    lazy import inside the code under test still resolves.
    """
    monkeypatch.setattr(
        install_root.sys,
        "meta_path",
        [
            *fakes,
            *(f for f in sys.meta_path if install_root._finder_mapping(f) is None),
        ],
    )


def test_editable_mapping_wins_over_the_worktree_we_were_imported_from(
    tmp_path, monkeypatch
):
    """The install root is what ``pip install -e`` recorded, not our own path."""
    checkout = _make_checkout(tmp_path / "agent-src")
    worktree = _make_checkout(
        checkout / ".claude" / "worktrees" / "wizardly-pasteur-947fe1"
    )

    monkeypatch.chdir(worktree)
    monkeypatch.setattr(install_root, "_running_package_root", lambda: worktree)
    _set_editable_finders(
        monkeypatch, _FakeEditableFinder({"hermes_cli": str(checkout / "hermes_cli")})
    )

    assert install_root.installed_package_root() == checkout


def test_a_stale_mapping_target_is_not_returned(tmp_path, monkeypatch):
    """A mapping pointing at a deleted checkout must not win over reality."""
    running = _make_checkout(tmp_path / "agent-src")
    monkeypatch.setattr(install_root, "_running_package_root", lambda: running)
    _set_editable_finders(
        monkeypatch,
        _FakeEditableFinder({"hermes_cli": str(tmp_path / "deleted" / "hermes_cli")}),
    )
    monkeypatch.setattr(install_root.sys, "path", [str(running)])

    assert install_root.find_installed_package_root() is None
    assert install_root.installed_package_root() == running


def test_site_packages_install_is_found_without_an_editable_mapping(
    tmp_path, monkeypatch
):
    """A non-editable install has no MAPPING — fall back to a sys.path scan.

    The scan must skip the running copy, because ``hermes_cli/main.py`` itself
    does ``sys.path.insert(0, PROJECT_ROOT)``: a worktree is on ``sys.path``
    explicitly, not only via the cwd.
    """
    site_packages = _make_checkout(tmp_path / "venv" / "Lib" / "site-packages")
    worktree = _make_checkout(tmp_path / "worktrees" / "objective-gates")

    _set_editable_finders(monkeypatch)
    monkeypatch.setattr(install_root, "_running_package_root", lambda: worktree)
    monkeypatch.setattr(
        install_root.sys, "path", [str(worktree), str(site_packages)]
    )

    assert install_root.installed_package_root() == site_packages


def test_the_callers_cwd_is_never_the_answer(tmp_path, monkeypatch):
    """``''``/cwd on sys.path is exactly the hazard — it must be ignored."""
    site_packages = _make_checkout(tmp_path / "site-packages")
    worktree = _make_checkout(tmp_path / "worktree")

    _set_editable_finders(monkeypatch)
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(
        install_root, "_running_package_root", lambda: tmp_path / "gone"
    )
    monkeypatch.setattr(
        install_root.sys, "path", ["", str(worktree), str(site_packages)]
    )

    assert install_root.installed_package_root() == site_packages


def test_falls_back_to_the_running_root_when_nothing_is_installed(
    tmp_path, monkeypatch
):
    """Running straight out of a clone with no install must keep working.

    ``find_installed_package_root()`` reports None so callers that have a
    better stable anchor than a possibly-transient checkout (the Windows task
    script's ``cd /d``) can still prefer it.
    """
    clone = _make_checkout(tmp_path / "clone")

    _set_editable_finders(monkeypatch)
    monkeypatch.setattr(install_root, "_running_package_root", lambda: clone)
    monkeypatch.setattr(install_root.sys, "path", [str(clone)])

    assert install_root.find_installed_package_root() is None
    assert install_root.installed_package_root() == clone


def test_real_install_root_holds_the_hermes_cli_package():
    """Unmonkeypatched, the resolver must name a real, plausible root.

    Guards against a resolver that only ever works under test doubles.
    """
    root = install_root.installed_package_root()

    assert (root / "hermes_cli" / "__init__.py").is_file(), root


def test_this_environments_real_editable_install_is_recognized():
    """The fake finder above must not be the only shape we understand.

    setuptools' generated finder keeps ``MAPPING`` as a MODULE-level global and
    reads it from a classmethod, so ``getattr(finder, "MAPPING")`` alone finds
    nothing — and the failure is silent, falling through to the running copy
    (the worktree). This test reads the mapping straight out of the loaded
    finder module and demands the resolver agree with it.
    """
    mappings = [
        module.MAPPING
        for module in list(sys.modules.values())
        if module is not None
        and isinstance(getattr(module, "MAPPING", None), dict)
        and install_root.PACKAGE in module.MAPPING
    ]
    if not mappings:
        pytest.skip("no editable install of hermes_cli in this environment")

    expected = Path(mappings[0][install_root.PACKAGE]).parent

    assert install_root.find_installed_package_root() == expected
