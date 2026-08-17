"""The ``importorskip`` husk guard installed in ``tests/conftest.py``.

Regression cover for the 2026-08-17 incident: a gutted numpy (directories
present, files gone, no dist-info) imported cleanly as a PEP-420 namespace
package, so ``pytest.importorskip("numpy")`` passed instead of skipping. Eight
tests failed that should have skipped, and the husk then sat in ``sys.modules``
poisoning ``pytest.approx`` for the rest of the session — 14 further failures in
files that never mention numpy.
"""

import sys

import pytest


def _make_husk(root, name):
    """Build a files-less package directory, exactly as a half-uninstall leaves."""
    pkg = root / name
    (pkg / "_core").mkdir(parents=True)
    (pkg / "__pycache__").mkdir(parents=True)
    return pkg


class TestHuskDetection:
    def test_empty_namespace_package_is_skipped_not_imported(self, tmp_path, monkeypatch):
        _make_husk(tmp_path, "hermes_husk_probe")
        monkeypatch.syspath_prepend(str(tmp_path))

        with pytest.raises(pytest.skip.Exception) as excinfo:
            pytest.importorskip("hermes_husk_probe")

        # The message has to name the remedy, or the next person re-derives it.
        assert "partially uninstalled" in str(excinfo.value)

    def test_husk_is_evicted_from_sys_modules(self, tmp_path, monkeypatch):
        """The cascade half: a husk left in sys.modules changes pytest.approx.

        ``_pytest.python_api._as_numpy_array`` does ``sys.modules.get("numpy")``
        and then calls ``np.isscalar``. Any husk that survives in sys.modules
        under a name a pytest builtin feature-detects breaks that builtin for
        every later test in the process.
        """
        _make_husk(tmp_path, "hermes_husk_evicted")
        monkeypatch.syspath_prepend(str(tmp_path))

        with pytest.raises(pytest.skip.Exception):
            pytest.importorskip("hermes_husk_evicted")

        assert "hermes_husk_evicted" not in sys.modules

    def test_submodules_of_the_husk_are_evicted_too(self, tmp_path, monkeypatch):
        _make_husk(tmp_path, "hermes_husk_sub")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules["hermes_husk_sub._core"] = object()  # as a real import would leave
        try:
            with pytest.raises(pytest.skip.Exception):
                pytest.importorskip("hermes_husk_sub")
            assert "hermes_husk_sub._core" not in sys.modules
        finally:
            sys.modules.pop("hermes_husk_sub._core", None)


class TestHealthyImportsAreUntouched:
    def test_real_package_still_returns_the_module(self):
        mod = pytest.importorskip("json")
        assert mod is sys.modules["json"]
        assert mod.loads("[1]") == [1]

    def test_genuinely_absent_module_still_skips(self):
        with pytest.raises(pytest.skip.Exception):
            pytest.importorskip("hermes_module_that_does_not_exist_anywhere")

    def test_keyword_arguments_are_passed_through(self):
        """``exc_type`` / ``reason`` must survive the wrapper."""
        mod = pytest.importorskip("json", reason="unused when the import works")
        assert mod is sys.modules["json"]

    def test_a_populated_namespace_package_is_not_a_husk(self, tmp_path, monkeypatch):
        """Guard against over-eager rejection.

        A legitimate PEP-420 namespace package also has ``__file__ is None``.
        It is only a husk if it additionally exposes nothing at all.
        """
        pkg = tmp_path / "hermes_real_namespace"
        pkg.mkdir()
        (pkg / "leaf.py").write_text("VALUE = 42\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))

        leaf = pytest.importorskip("hermes_real_namespace.leaf")
        assert leaf.VALUE == 42
