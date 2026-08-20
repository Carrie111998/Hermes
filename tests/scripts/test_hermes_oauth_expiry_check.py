"""Tests for scripts/hermes-oauth-expiry-check.py (Part 2 of the
oauth-reauth-expiry-check feature).

Covers: the 2-day warning-window boundary math, the jid-vs-family-member
differentiated notification behavior (daily-until-fixed vs. one-time
heads-up+expired), the no-sidecar fallback (purely reactive), state reset on
a fresh re-auth cycle, and the delivery-target resolution gap (skip rather
than guess a chat id).

Identity generality: nothing here special-cases "jid" or "zarkash" by name —
every differentiated-behavior test constructs its own registry with
synthetic identity names to prove the primary/non-primary split is driven by
``is_primary_identity()``'s structural rule (credentials_dir == HERMES_HOME),
not a hardcoded name list, per the generality requirement.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "hermes-oauth-expiry-check.py"
)


def _load_module(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "hermes_oauth_expiry_check_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(tmp_path):
    return _load_module(tmp_path)


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    return home


def _write_sidecar(entry_dir: Path, recorded_at_epoch: float, identity: str = "x"):
    entry_dir.mkdir(parents=True, exist_ok=True)
    sidecar = entry_dir / "google_token_reauth_at.json"
    sidecar.write_text(
        json.dumps(
            {
                "identity": identity,
                "recorded_at": datetime.fromtimestamp(
                    recorded_at_epoch, tz=timezone.utc
                ).isoformat(),
                "recorded_at_epoch": recorded_at_epoch,
            }
        ),
        encoding="utf-8",
    )


class TestWarningWindowMath:
    def test_far_from_expiry_not_in_window(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expiry = now + timedelta(days=5)
        assert mod.in_warning_window(expiry, now) is False

    def test_exactly_two_days_is_inclusive(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expiry = now + timedelta(days=2)
        assert mod.in_warning_window(expiry, now) is True

    def test_just_over_two_days_not_in_window(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expiry = now + timedelta(days=2, seconds=1)
        assert mod.in_warning_window(expiry, now) is False

    def test_just_under_two_days_in_window(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expiry = now + timedelta(days=1, hours=23)
        assert mod.in_warning_window(expiry, now) is True

    def test_already_past_expiry_stays_in_window(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expiry = now - timedelta(days=10)
        assert mod.in_warning_window(expiry, now) is True

    def test_none_expiry_never_in_window(self, mod):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert mod.in_warning_window(None, now) is False

    def test_estimate_expiry_is_seven_days_after_recorded_at(self, mod):
        recorded = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        expiry = mod.estimate_expiry(recorded)
        assert expiry == datetime(2026, 1, 8, tzinfo=timezone.utc)

    def test_estimate_expiry_none_when_no_recorded_at(self, mod):
        assert mod.estimate_expiry(None) is None


class TestIsPrimaryIdentity:
    def test_root_credentials_dir_is_primary(self, mod, hermes_home):
        entry = {"credentials_dir": hermes_home}
        assert mod.is_primary_identity(entry, hermes_home) is True

    def test_nested_credentials_dir_is_not_primary(self, mod, hermes_home):
        entry = {"credentials_dir": hermes_home / "family_credentials" / "someone"}
        assert mod.is_primary_identity(entry, hermes_home) is False

    def test_future_identity_name_irrelevant_to_the_rule(self, mod, hermes_home):
        """A brand-new identity name never seen before must still be
        classified correctly purely by directory structure."""
        entry = {"credentials_dir": hermes_home / "family_credentials" / "newcomer99"}
        assert mod.is_primary_identity(entry, hermes_home) is False


def _patch_common(mod, monkeypatch, *, check_ok: bool, auth_url: str = "https://accounts.google.com/fake-auth"):
    sent = []

    monkeypatch.setattr(mod, "check_auth_live", lambda hermes_home, identity: check_ok)
    monkeypatch.setattr(mod, "fetch_fresh_auth_url", lambda hermes_home, identity: auth_url)
    monkeypatch.setattr(mod, "stage_reminder", lambda identity, *, kind, expiry: f"pend-{identity}-{kind}")

    def _fake_send(chat_id, message):
        sent.append((chat_id, message))
        return True, "ok"

    monkeypatch.setattr(mod, "send_telegram_message", _fake_send)
    return sent


class TestPrimaryVsFamilyMemberDifferentiation:
    """Uses synthetic identity names ("admin_person" / "family_person") to
    prove the daily-vs-one-time split is driven by is_primary_identity()'s
    structural rule, not by name."""

    def test_primary_sends_daily_with_no_suppression(self, mod, hermes_home, monkeypatch):
        entry_dir = hermes_home  # primary: credentials_dir == HERMES_HOME
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()  # 1 day left -> in window
        _write_sidecar(entry_dir, recorded_at, identity="admin_person")
        entry = {"credentials_dir": entry_dir}

        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__ADMIN_PERSON", "111")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs1 = mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, now=now, state=state,
        )
        assert any("sent daily_warning" in line for line in logs1)
        assert len(sent) == 1

        # Run again "the next day" with the SAME sidecar (no re-auth
        # happened) -- primary must send AGAIN, no one-time suppression.
        now2 = now + timedelta(days=1)
        logs2 = mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, now=now2, state=state,
        )
        assert any("sent daily_warning" in line for line in logs2)
        assert len(sent) == 2

    def test_family_member_sends_heads_up_exactly_once(self, mod, hermes_home, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()  # in window, not yet revoked
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}

        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__FAMILY_PERSON", "222")
        sent = _patch_common(mod, monkeypatch, check_ok=True)  # check_ok=True -> not revoked

        state: dict = {}
        logs1 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, now=now, state=state,
        )
        assert any("sent heads_up" in line for line in logs1)
        assert len(sent) == 1

        # Re-run same day / next day, still not revoked, no new sidecar --
        # must NOT send again.
        now2 = now + timedelta(hours=12)
        logs2 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, now=now2, state=state,
        )
        assert any("already sent one-time heads-up" in line for line in logs2)
        assert len(sent) == 1

    def test_family_member_sends_expired_exactly_once_when_revoked(self, mod, hermes_home, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=8)).timestamp()  # already past 7 days
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}

        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__FAMILY_PERSON", "222")
        sent = _patch_common(mod, monkeypatch, check_ok=False)  # revoked

        state: dict = {}
        logs1 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, now=now, state=state,
        )
        assert any("sent expired" in line for line in logs1)
        assert len(sent) == 1

        logs2 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, now=now + timedelta(days=1),
            state=state,
        )
        assert any("already sent one-time EXPIRED" in line for line in logs2)
        assert len(sent) == 1


class TestFallbackNoSidecar:
    def test_no_sidecar_ok_check_is_silent(self, mod, hermes_home, monkeypatch):
        entry = {"credentials_dir": hermes_home / "family_credentials" / "legacy_person"}
        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__LEGACY_PERSON", "333")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home,
            now=datetime.now(timezone.utc), state=state,
        )
        assert any("fallback reactive mode, --check OK" in line for line in logs)
        assert sent == []

    def test_no_sidecar_failed_check_sends_once_for_family_member(self, mod, hermes_home, monkeypatch):
        entry = {"credentials_dir": hermes_home / "family_credentials" / "legacy_person"}
        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__LEGACY_PERSON", "333")
        sent = _patch_common(mod, monkeypatch, check_ok=False)

        state: dict = {}
        now = datetime.now(timezone.utc)
        logs1 = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home, now=now, state=state,
        )
        assert any("sent reactive_expired_once" in line for line in logs1)
        assert len(sent) == 1

        logs2 = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home,
            now=now + timedelta(days=1), state=state,
        )
        assert any("already sent one-time expired notice" in line for line in logs2)
        assert len(sent) == 1

    def test_no_sidecar_failed_check_sends_daily_for_primary(self, mod, hermes_home, monkeypatch):
        entry = {"credentials_dir": hermes_home}
        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__ADMIN_PERSON", "111")
        sent = _patch_common(mod, monkeypatch, check_ok=False)

        state: dict = {}
        now = datetime.now(timezone.utc)
        mod.process_identity("admin_person", entry, hermes_home=hermes_home, now=now, state=state)
        mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home,
            now=now + timedelta(days=1), state=state,
        )
        assert len(sent) == 2


class TestStateResetOnFreshReauth:
    def test_new_sidecar_timestamp_resets_one_time_flags(self, mod, hermes_home, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}

        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__FAMILY_PERSON", "222")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        mod.process_identity("family_person", entry, hermes_home=hermes_home, now=now, state=state)
        assert len(sent) == 1
        assert state["family_person"]["heads_up_sent_at"] is not None

        # Simulate a fresh re-auth: new sidecar timestamp recorded (as Part 3
        # would do after a successful --auth-code exchange), well outside
        # the window now.
        new_recorded_at = now.timestamp()
        _write_sidecar(entry_dir, new_recorded_at, identity="family_person")

        logs = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home,
            now=now + timedelta(minutes=5), state=state,
        )
        assert any("new re-auth cycle detected" in line for line in logs)
        assert state["family_person"]["heads_up_sent_at"] is None
        assert state["family_person"]["last_known_recorded_at"] == new_recorded_at

    def test_reset_identity_cycle_helper_clears_state(self, mod, hermes_home):
        state = {
            "family_person": {
                "last_known_recorded_at": 123.0,
                "heads_up_sent_at": 456.0,
                "expired_sent_at": None,
            }
        }
        mod.save_state(hermes_home, state)

        mod.reset_identity_cycle(hermes_home, "family_person")

        reloaded = mod.load_state(hermes_home)
        assert reloaded["family_person"] == {
            "last_known_recorded_at": None,
            "heads_up_sent_at": None,
            "expired_sent_at": None,
        }


class TestDeliveryTargetGap:
    def test_missing_env_var_skips_without_guessing(self, mod, hermes_home, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "mystery_person"
        now = datetime.now(timezone.utc)
        _write_sidecar(entry_dir, (now - timedelta(days=6)).timestamp(), identity="mystery_person")
        entry = {"credentials_dir": entry_dir}
        # Deliberately NOT setting HERMES_IDENTITY_TELEGRAM_CHAT_ID__MYSTERY_PERSON
        monkeypatch.delenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__MYSTERY_PERSON", raising=False)
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "mystery_person", entry, hermes_home=hermes_home, now=now, state=state,
        )
        assert any("SKIPPED" in line and "not set" in line for line in logs)
        assert sent == []

    def test_resolve_telegram_chat_id_is_env_driven_not_hardcoded(self, mod, monkeypatch):
        monkeypatch.delenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__WHOEVER", raising=False)
        assert mod.resolve_telegram_chat_id("whoever") is None
        monkeypatch.setenv("HERMES_IDENTITY_TELEGRAM_CHAT_ID__WHOEVER", "999")
        assert mod.resolve_telegram_chat_id("whoever") == "999"


class TestReminderMessageShape:
    def test_message_contains_required_elements(self, mod):
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        msg = mod.build_reminder_message(
            "abc123", "https://accounts.google.com/fake", kind="heads_up", expiry=expiry
        )
        assert "[ref:oauth_reauth:abc123]" in msg
        assert "https://accounts.google.com/fake" in msg
        assert "reply to this message" in msg.lower() or "reply to THIS message" in msg
        assert "swipe" in msg.lower() or "hold" in msg.lower()
