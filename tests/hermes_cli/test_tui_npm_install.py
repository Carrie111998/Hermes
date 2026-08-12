"""_tui_need_npm_install: auto npm when node_modules is behind the lockfile."""

import json
import os
import types
from pathlib import Path

import pytest


@pytest.fixture
def main_mod():
    import hermes_cli.main as m

    return m


def _touch_ink(root: Path) -> None:
    ink = root / "node_modules" / "@hermes" / "ink" / "package.json"
    ink.parent.mkdir(parents=True, exist_ok=True)
    ink.write_text("{}")


def _touch_tui_entry(root: Path) -> None:
    entry = root / "dist" / "entry.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("console.log('tui')")


def _assert_utf8_replace_capture(kwargs: dict) -> None:
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"














def test_make_tui_argv_uses_bundled_tui_when_workspace_missing(
    tmp_path: Path, main_mod, monkeypatch
) -> None:
    """Prebuilt-install regression (#56665): a prebuilt install (Docker
    image, Nix build, or prior `npm run build`) ships
    hermes_cli/tui_dist/entry.js but never ships ui-tui/ (that directory only
    exists in a git checkout). _make_tui_argv must try the bundled entry.js
    BEFORE _ensure_tui_workspace() — requiring the workspace first hard-exits
    every prebuilt dashboard Chat tab connection with `sys.exit(1)` (surfaced
    to the user as the unhelpful "Chat unavailable: 1") despite a perfectly
    runnable bundled TUI on disk. The bundled shortcut must succeed without
    ever touching the (missing) ui-tui workspace or git.
    """
    monkeypatch.delenv("HERMES_TUI_DIR", raising=False)
    monkeypatch.setattr(main_mod, "_ensure_tui_node", lambda: None)

    bundled_entry = tmp_path / "bundled" / "entry.js"
    bundled_entry.parent.mkdir(parents=True)
    bundled_entry.write_text("// bundled TUI")
    monkeypatch.setattr(main_mod, "_find_bundled_tui", lambda: bundled_entry)

    def which(name: str) -> str | None:
        if name == "node":
            return "/usr/bin/node"
        raise AssertionError(f"unexpected shutil.which({name!r}) call — bundled path must not need npm/git")

    monkeypatch.setattr(main_mod.shutil, "which", which)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("bundled TUI path must not spawn any subprocess (no npm install/build, no git restore)")

    monkeypatch.setattr(main_mod.subprocess, "run", fail_run)

    # ui-tui/ deliberately does not exist under tmp_path, and there is no
    # .git either — this mirrors a prebuilt (Docker/Nix) install exactly.
    tui_dir = tmp_path / "ui-tui"
    assert not tui_dir.exists()

    argv, cwd = main_mod._make_tui_argv(tui_dir, tui_dev=False)

    assert argv == ["/usr/bin/node", "--expose-gc", str(bundled_entry)]
    assert cwd == bundled_entry.parent


# ── _workspace_root helper ──────────────────────────────────────────




    # (Smoke test: just confirm _tui_need_npm_install doesn't crash)
    # It won't need install because the lockfile exists and there's no
    # hidden lockfile to compare against, and ink is missing → True.
    # But the key invariant is: ws_root for the need-check == ws_root
    # for the install cwd — both use _workspace_root(sub).


def test_no_stray_lockfiles_in_workspace_subdirs(main_mod) -> None:
    """Workspace sub-directories must not contain their own package-lock.json.

    With a single workspace root lockfile, per-directory lockfiles are
    always accidental (typically from running ``npm install`` inside the
    wrong directory).  They cause ``_workspace_root`` to treat the
    sub-package as standalone, which breaks hoisted ``node_modules``
    resolution and can silently diverge the install cwd from the
    lockfile-check root.

    This is an invariant, not a change-detector: the workspace structure
    is not expected to gain per-dir lockfiles.
    """
    root = main_mod.PROJECT_ROOT
    # Workspace members that live one level below the root and should
    # NOT have their own lockfile.  (ui-tui/packages/* members are
    # two levels deep and even less likely to get accidental lockfiles,
    # but we check them too for completeness.)
    subdirs = [
        root / "ui-tui",
        root / "web",
        root / "apps" / "desktop",
        root / "apps" / "shared",
    ]
    # Also sweep ui-tui/packages/* (hermes-ink etc.)
    tui_pkgs = root / "ui-tui" / "packages"
    if tui_pkgs.is_dir():
        subdirs.extend(d for d in tui_pkgs.iterdir() if d.is_dir())

    stray = [d for d in subdirs if (d / "package-lock.json").is_file()]
    assert not stray, (
        "stray package-lock.json found in workspace sub-directory(es); "
        "delete them and run `npm install` from the repo root instead: "
        + ", ".join(str(d / "package-lock.json") for d in stray)
    )


def test_make_tui_argv_omits_workspace_when_tui_has_own_lockfile(
    tmp_path: Path, main_mod, monkeypatch
) -> None:
    """When ui-tui/ has its own package-lock.json, _workspace_root returns
    tui_dir itself.  npm install --workspace ui-tui would fail in that case
    because npm cannot find a workspace named "ui-tui" inside ui-tui/.
    The fix omits --workspace and runs plain npm install from tui_dir.
    See #42973.
    """
    tui_dir = tmp_path / "ui-tui"
    tui_dir.mkdir()
    (tui_dir / "package.json").write_text("{}")
    # Simulate curl-install layout: tui_dir has its own lockfile
    (tui_dir / "package-lock.json").write_text("{}")
    # Parent also has lockfile (but _workspace_root prefers tui_dir's own)
    (tmp_path / "package-lock.json").write_text("{}")

    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr")
    monkeypatch.setattr(main_mod, "_tui_need_npm_install", lambda _root: True)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/bin/{name}")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    main_mod._make_tui_argv(tui_dir, tui_dev=False)

    install_cmd = calls[0][0][0]
    # Must NOT contain --workspace when npm_cwd == tui_dir
    assert "--workspace" not in install_cmd, (
        f"npm install should omit --workspace when tui_dir has its own lockfile, got: {install_cmd}"
    )
    assert install_cmd[:2] == ["/bin/npm", "install"]
    # cwd must be tui_dir (standalone), not parent
    assert calls[0][1]["cwd"] == str(tui_dir)


# ── _tui_need_npm_install: reduced npm >= 10 hidden lockfile ─────────


def _write_workspace(tmp_path: Path, wanted: dict, installed: dict) -> Path:
    """Scaffold a minimal npm-workspace checkout under *tmp_path*.

    Returns the ``ui-tui`` sub-directory to hand to ``_tui_need_npm_install``.
    """
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": wanted}), encoding="utf-8"
    )
    hidden = tmp_path / "node_modules" / ".package-lock.json"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(json.dumps({"packages": installed}), encoding="utf-8")
    # @hermes/ink marker — checked up front, must exist or the function
    # short-circuits to True before reaching the lockfile comparison.
    ink = tmp_path / "node_modules" / "@hermes" / "ink" / "package.json"
    ink.parent.mkdir(parents=True, exist_ok=True)
    ink.write_text("{}")
    tui = tmp_path / "ui-tui"
    tui.mkdir()
    (tui / "package.json").write_text("{}")
    return tui


def test_tui_need_npm_install_ignores_reduced_hidden_lockfile(
    tmp_path: Path, main_mod
) -> None:
    """npm >= 10 writes a *reduced* node_modules/.package-lock.json: only
    resolved/integrity survive, declarative fields (version/dependencies/dev/
    engines/...) are nulled, and hoisted packages gain ``extraneous``.  A fresh
    install must not look "behind".  Regression for #84617.
    """
    wanted = {
        "": {},
        "node_modules/react": {
            "version": "18.2.0",
            "resolved": "https://registry.npmjs.org/react/-/react-18.2.0.tgz",
            "integrity": "sha512-reacthash",
            "dependencies": {"loose-envify": "^1.1.0"},
            "dev": True,
            "engines": {"node": ">=0.10.0"},
        },
        # workspace link + workspace-root entry: never materialized by
        # `npm install --workspace ui-tui`
        "node_modules/hermes": {"link": True, "resolved": "apps/desktop"},
        "apps/desktop": {"name": "hermes-desktop", "version": "0.0.0"},
        # optional dep skipped by npm on this platform
        "node_modules/fsevents": {"optional": True, "version": "2.3.3"},
    }
    installed = {
        # reduced npm 11 form: no version/dependencies/dev, plus extraneous
        "node_modules/react": {
            "resolved": "https://registry.npmjs.org/react/-/react-18.2.0.tgz",
            "integrity": "sha512-reacthash",
            "extraneous": True,
        },
    }
    tui = _write_workspace(tmp_path, wanted, installed)
    assert main_mod._tui_need_npm_install(tui) is False


def test_tui_need_npm_install_detects_real_skew(tmp_path: Path, main_mod) -> None:
    """A bumped integrity/resolved still triggers a reinstall."""
    wanted = {
        "node_modules/react": {
            "version": "18.3.0",
            "resolved": "https://registry.npmjs.org/react/-/react-18.3.0.tgz",
            "integrity": "sha512-newhash",
        },
    }
    installed = {
        "node_modules/react": {
            "resolved": "https://registry.npmjs.org/react/-/react-18.2.0.tgz",
            "integrity": "sha512-oldhash",
        },
    }
    tui = _write_workspace(tmp_path, wanted, installed)
    assert main_mod._tui_need_npm_install(tui) is True


def test_tui_need_npm_install_detects_new_dependency(tmp_path: Path, main_mod) -> None:
    """A newly added (non-link, node_modules/) package triggers a reinstall."""
    wanted = {
        "node_modules/newpkg": {
            "version": "1.0.0",
            "resolved": "https://registry.npmjs.org/newpkg/-/newpkg-1.0.0.tgz",
            "integrity": "sha512-newpkghash",
        },
    }
    tui = _write_workspace(tmp_path, wanted, {})
    assert main_mod._tui_need_npm_install(tui) is True
