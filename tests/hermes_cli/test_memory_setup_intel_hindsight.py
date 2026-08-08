"""Regression tests for #81421: Hindsight local-embedded dependency repair
must not install the bare full bundle on Intel macOS.

Before the fix, ``_provider_pip_dependencies`` appended ``hindsight-all``
for every local/local_embedded install.  On Intel macOS the full local-ML
dependency set pulls MLX packages that have no x86_64 wheels, so the
resolver silently backtracks to ancient ``hindsight-all``/``hindsight-api``
releases.  Their overlapping ``hindsight_api`` files override the working
slim API and the daemon crashes with "Unknown embeddings provider: onnx"
(#81421).
"""

from unittest.mock import patch

import hermes_cli.memory_setup as memory_setup
from hermes_cli.memory_setup import _is_intel_macos, _provider_pip_dependencies

DECLARED = ["hindsight-client>=0.6.1"]


def _write_hindsight_config(home, mode="local_embedded"):
    (home / "hindsight").mkdir(parents=True, exist_ok=True)
    (home / "hindsight" / "config.json").write_text(
        '{"mode": "%s"}' % mode, encoding="utf-8"
    )


class TestIntelMacosGuard:
    def test_detects_intel_macos(self):
        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="x86_64"
        ):
            assert _is_intel_macos() is True

    def test_arm64_macos_is_not_intel(self):
        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="arm64"
        ):
            assert _is_intel_macos() is False

    def test_linux_is_not_intel_macos(self):
        with patch("platform.system", return_value="Linux"), patch(
            "platform.machine", return_value="x86_64"
        ):
            assert _is_intel_macos() is False


class TestHindsightLocalEmbeddedDeps:
    def test_intel_macos_uses_slim_stack_not_bare_bundle(self, tmp_path, monkeypatch):
        """The issue's regression case: Intel macOS local_embedded must NOT
        get a bare ``hindsight-all`` spec (which backtracks to ancient
        full-package releases)."""
        _write_hindsight_config(tmp_path)
        monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)

        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="x86_64"
        ):
            deps = _provider_pip_dependencies("hindsight", DECLARED)

        assert "hindsight-all" not in deps
        assert "hindsight-all-slim" in deps
        assert "hindsight-api-slim[local-onnx]" in deps
        # Declared bridge deps are preserved.
        assert "hindsight-client>=0.6.1" in deps

    def test_non_intel_keeps_bare_bundle(self, tmp_path, monkeypatch):
        """Apple Silicon / Linux keep the full bundle — the existing heal
        path for #70636 is unchanged."""
        _write_hindsight_config(tmp_path)
        monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)

        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="arm64"
        ):
            deps = _provider_pip_dependencies("hindsight", DECLARED)

        assert "hindsight-all" in deps
        assert "hindsight-all-slim" not in deps

    def test_non_local_modes_unaffected(self, tmp_path, monkeypatch):
        """A remote/API-mode Hindsight config never gets local deps at all."""
        _write_hindsight_config(tmp_path, mode="remote")
        monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)

        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="x86_64"
        ):
            deps = _provider_pip_dependencies("hindsight", DECLARED)

        assert deps == DECLARED

    def test_missing_config_falls_back_to_declared(self, tmp_path, monkeypatch):
        """No hindsight config.json → declared bridge deps only (same as
        before the fix)."""
        monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)

        deps = _provider_pip_dependencies("hindsight", DECLARED)

        assert deps == DECLARED
