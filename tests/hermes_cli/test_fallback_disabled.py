"""Master fallback kill-switch: policy readers in ``hermes_cli.fallback_config``.

Manual mode: when ``fallback.enabled`` is false, NO automatic provider/model
route change happens anywhere. The resolver default is True for backward
compatibility with the historical behaviour and the existing test suite;
a deployment sets ``fallback.enabled: false`` explicitly to opt in. Flipping
the module default to False is a one-line change.

These tests exercise ONLY the pure config-policy readers, not any agent
construction, so they are self-contained.
"""
from __future__ import annotations

import logging

import pytest

import hermes_cli.fallback_config as fc


class TestFallbackEnabledResolver:
    def test_default_true_when_absent(self):
        # backward-compatible default; a config opts out to false explicitly
        assert fc.fallback_enabled(None) is True
        assert fc.fallback_enabled({}) is True

    def test_explicit_false_disables(self):
        assert fc.fallback_enabled({"fallback": {"enabled": False}}) is False

    def test_explicit_true_enables(self):
        assert fc.fallback_enabled({"fallback": {"enabled": True}}) is True

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("false", False), ("False", False), ("no", False), ("off", False), (0, False),
            ("true", True), ("True", True), ("yes", True), ("on", True), (1, True),
        ],
    )
    def test_scalar_coercion(self, raw, expected):
        assert fc.fallback_enabled({"fallback": {"enabled": raw}}) is expected

    def test_malformed_fallback_key_defaults_true(self):
        assert fc.fallback_enabled({"fallback": "nonsense"}) is True
        assert fc.fallback_enabled({"fallback": None}) is True
        assert fc.fallback_enabled({"fallback": {}}) is True

    def test_non_dict_config_is_default(self):
        # Runs on the hot construction path for every implicit agent — a
        # non-mapping config must not raise.
        assert fc.fallback_enabled("garbage") is True
        assert fc.fallback_enabled(7) is True
        assert fc.fallback_enabled([{"fallback": {"enabled": False}}]) is True  # list


class TestResolveFallbackEnabledDefault:
    def test_reads_config_false(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda *a, **k: {"fallback": {"enabled": False}},
        )
        assert fc.resolve_fallback_enabled_default() is False

    def test_reads_config_true(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda *a, **k: {"fallback": {"enabled": True}},
        )
        assert fc.resolve_fallback_enabled_default() is True

    def test_absent_key_is_historical_default(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
        assert fc.resolve_fallback_enabled_default() is True

    def test_load_error_logs_and_defaults(self, monkeypatch, caplog):
        def _boom(*a, **k):
            raise RuntimeError("yaml boom")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        with caplog.at_level(logging.WARNING):
            # Must NOT silently swallow: a config read error is logged loudly,
            # then falls back to the historical default (does not hard-break
            # startup).
            assert fc.resolve_fallback_enabled_default() is True
        assert any(
            "config" in r.getMessage().lower() and "fallback" in r.getMessage().lower()
            for r in caplog.records
        )

    def test_garbage_string_config_does_not_crash(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda *a, **k: "garbage-string"
        )
        with caplog.at_level(logging.WARNING):
            assert fc.resolve_fallback_enabled_default() is True
        assert any("not a mapping" in r.getMessage() for r in caplog.records)

    def test_garbage_int_config_does_not_crash(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: 7)
        assert fc.resolve_fallback_enabled_default() is True


class TestGetFallbackChainNonDict:
    def test_non_dict_config_is_empty(self):
        assert fc.get_fallback_chain("garbage") == []
        assert fc.get_fallback_chain(7) == []
        assert fc.get_fallback_chain(None) == []
        assert fc.get_fallback_chain([{"provider": "openai", "model": "gpt-4"}]) == []
