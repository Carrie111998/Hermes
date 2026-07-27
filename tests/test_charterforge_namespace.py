from __future__ import annotations

import os

from charterforge import __version__
from charterforge.compat import install_legacy_environment_aliases


def test_public_namespace_has_version():
    assert __version__ == "0.19.0"


def test_canonical_environment_is_translated_for_legacy_readers(monkeypatch):
    monkeypatch.setenv("CHARTERFORGE_HOME", "/tmp/charterforge-test")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    install_legacy_environment_aliases()
    assert os.environ["HERMES_HOME"] == "/tmp/charterforge-test"

