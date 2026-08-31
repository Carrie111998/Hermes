"""Test: multi-venv sync for installs carrying BOTH venv/ and .venv/ (#97340).

An install can have both a ``venv`` (older git-install flow) and a ``.venv``
(uv/bootstrap flow). The ``~/.local/bin/hermes`` shim and Desktop-spawned
``hermes --profile <p> serve --isolated`` processes run from ``.venv`` while
gateway/webui/dashboard services run from ``venv``. ``hermes update`` used to
sync deps / probe health / detect holders against ``venv`` ONLY, leaving the
``.venv`` the launcher actually uses stale (``ModuleNotFoundError:
snowballstemmer`` after a release adds a dependency).

These tests prove the fixed behavior: every present project venv is enumerated
by the shared ``_project_venv_dirs()`` helper and the health probe covers ALL
present venvs, so a stale ``.venv`` trips a repair. They FAIL against the
pre-fix code, which only ever looked at ``venv``.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermes_cli.update_cmd as update_mod
import hermes_cli.main as main_mod


def _make_project_root(with_both: bool = True) -> Path:
    """Create a temp PROJECT_ROOT with ``venv/bin/python`` and (optionally)
    ``.venv/bin/python`` present — the minimal shape a present venv dir has."""
    root = Path(tempfile.mkdtemp(prefix="hc-97340-"))
    for name in (("venv", ".venv") if with_both else ("venv",)):
        venv = root / name / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "python").write_text("#! /usr/bin/env python\n")
    return root


class MultiVenvUpdateTest(unittest.TestCase):
    def _patch_root(self, root: Path):
        """Point PROJECT_ROOT at *root* so ``_project_venv_dirs()`` / the probe
        see our fake layout."""
        return mock.patch.object(main_mod, "PROJECT_ROOT", root)

    # ── _project_venv_dirs ────────────────────────────────────────────────
    def test_project_venv_dirs_lists_both_when_present(self):
        """The sync loop iterates _project_venv_dirs(); with both present it
        must yield venv THEN .venv so deps land in both. Pre-fix there was no
        such helper — code hardcoded PROJECT_ROOT/"venv" only."""
        with self._patch_root(_make_project_root(with_both=True)):
            dirs = update_mod._project_venv_dirs()
        self.assertEqual([d.name for d in dirs], ["venv", ".venv"])

    def test_project_venv_dirs_single_when_only_venv(self):
        """Back-compat: a plain git-install (only venv/) still yields a single
        target, so nothing regresses for single-venv installs."""
        with self._patch_root(_make_project_root(with_both=False)):
            dirs = update_mod._project_venv_dirs()
        self.assertEqual([d.name for d in dirs], ["venv"])

    # ── health probe covers .venv ─────────────────────────────────────────
    def test_health_probe_unhealthy_when_dotvenv_missing_core_import(self):
        """THE regression: with both venv/ and .venv/ present, a stale .venv
        (missing a core import) must make the install report unhealthy so a
        repair is triggered. Pre-fix code only probed venv/ and returned
        healthy, leaving the shim's env stale forever."""
        root = _make_project_root(with_both=True)
        dot_venv_dir = root / ".venv"

        def fake_probe(venv_dir):
            # .venv lacks 'fastapi' -> unhealthy; venv is fine.
            if venv_dir == dot_venv_dir:
                return False, "fastapi: No module named 'fastapi'"
            return True, ""

        with self._patch_root(root), \
             mock.patch.object(
                 update_mod, "_probe_single_venv_imports", side_effect=fake_probe
             ) as probe_mock, \
             mock.patch.object(main_mod, "_is_windows", return_value=False):
            healthy, detail = update_mod._venv_core_imports_healthy()
        self.assertFalse(healthy, "stale .venv must trip an unhealthy probe")
        self.assertIn("fastapi", detail)
        # Both venvs were probed (the probe was invoked for venv AND .venv).
        self.assertEqual(probe_mock.call_count, 2)

    def test_health_probe_healthy_when_only_venv_probed(self):
        """Back-compat: with only venv/ present, the probe runs once against it
        and must not be forced to fail on a nonexistent .venv."""
        root = _make_project_root(with_both=False)

        def fake_probe(venv_dir):
            return True, ""

        with self._patch_root(root), \
             mock.patch.object(
                 update_mod, "_probe_single_venv_imports", side_effect=fake_probe
             ) as probe_mock:
            healthy, _ = update_mod._venv_core_imports_healthy()
        self.assertTrue(healthy)
        self.assertEqual(probe_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
