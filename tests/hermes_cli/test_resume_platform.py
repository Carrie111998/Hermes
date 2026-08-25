"""Tests for `--resume @<platform>` — continue a gateway platform's most
recent session in the terminal (the reverse direction of /handoff).

`@claude` / `@codex` are resolved earlier by the foreign-session import block;
this helper only sees the remaining `@` forms.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def main_mod():
    import hermes_cli.main as mod

    return mod


class TestResolvePlatformResume:
    def test_platform_with_session_resolves_to_its_mru_id(self, main_mod, monkeypatch, capsys):
        seen = {}

        def fake_mru(source="cli"):
            seen["source"] = source
            return "20260824_104314_b8ec37eb"

        monkeypatch.setattr(main_mod, "_resolve_last_session", fake_mru)
        resolved = main_mod._resolve_platform_resume("@telegram")
        assert resolved == "20260824_104314_b8ec37eb"
        assert seen["source"] == "telegram"
        assert "telegram" in capsys.readouterr().out

    def test_case_and_whitespace_are_normalized(self, main_mod, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            main_mod, "_resolve_last_session",
            lambda source="cli": seen.setdefault("source", source) and "sid" or "sid",
        )
        assert main_mod._resolve_platform_resume("  @WhatsApp ") == "sid"
        assert seen["source"] == "whatsapp"

    def test_non_at_values_pass_through_untouched(self, main_mod, monkeypatch):
        monkeypatch.setattr(
            main_mod, "_resolve_last_session",
            lambda source="cli": pytest.fail("must not query MRU for non-@ values"),
        )
        assert main_mod._resolve_platform_resume("20260824_1043") is None
        assert main_mod._resolve_platform_resume("latest") is None
        assert main_mod._resolve_platform_resume("my session title") is None
        # Bare "@" has no platform name — treated as a normal resume value.
        assert main_mod._resolve_platform_resume("@") is None

    def test_platform_without_sessions_exits_with_hint(self, main_mod, monkeypatch, capsys):
        monkeypatch.setattr(main_mod, "_resolve_last_session", lambda source="cli": None)
        with pytest.raises(SystemExit) as exc:
            main_mod._resolve_platform_resume("@signal")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "signal" in out
        assert "hermes sessions list" in out
