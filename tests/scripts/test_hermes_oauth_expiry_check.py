"""Tests for scripts/hermes-oauth-expiry-check.py (Part 2 of the
oauth-reauth-expiry-check feature).

Covers: the 2-day warning-window boundary math, the jid-vs-family-member
differentiated notification behavior (daily-until-fixed vs. one-time
heads-up+expired), the no-sidecar fallback (purely reactive), state reset on
a fresh re-auth cycle, and vault-Profile.md-based delivery-target resolution
(scripts/_family_delivery.py) — including the missing/malformed-profile
failure modes, which must skip loudly rather than guess or misdeliver.

Identity generality: nothing here special-cases "jid" or "zarkash" by name —
every differentiated-behavior test constructs its own registry with
synthetic identity names to prove the primary/non-primary split is driven by
``is_primary_identity()``'s structural rule (credentials_dir == HERMES_HOME),
not a hardcoded name list, per the generality requirement. Delivery-target
tests similarly use synthetic names to prove ``_family_delivery.py``'s path
convention (``Family/<Capitalized name>/<Capitalized name> Profile.md``)
generalizes to a brand-new identity with zero code changes.
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
FAMILY_DELIVERY_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "_family_delivery.py"
)


def _load_module(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "hermes_oauth_expiry_check_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_family_delivery_module():
    spec = importlib.util.spec_from_file_location(
        "family_delivery_test", FAMILY_DELIVERY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(tmp_path):
    return _load_module(tmp_path)


@pytest.fixture
def fd(tmp_path):
    return _load_family_delivery_module()


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    return home


@pytest.fixture
def vault_root(tmp_path):
    root = tmp_path / "Obsidian Core"
    root.mkdir()
    return root


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


def _profile_path(vault_root: Path, identity: str, *, is_primary: bool) -> Path:
    if is_primary:
        return vault_root / "Hermes" / "Profile" / "JID Profile.md"
    name = identity.capitalize()
    return vault_root / "Hermes" / "Profile" / "Family" / name / f"{name} Profile.md"


def _write_profile(
    vault_root: Path, identity: str, chat_id: str, *, is_primary: bool = False,
    table_row: "str | None" = None,
) -> Path:
    """Write a realistic vault Profile.md fixture with a Platform Identity
    table, matching the real format observed on the live vault."""
    path = _profile_path(vault_root, identity, is_primary=is_primary)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = table_row if table_row is not None else f"| Telegram | — | {chat_id} |"
    path.write_text(
        "---\nstatus: canonical\n---\n\n"
        f"# {identity.capitalize()} Profile\n\n"
        "## Platform Identity\n\n"
        "| Platform | Username | User ID |\n"
        "|---|---|---|\n"
        "| Slack | someone | U0123456789 |\n"
        f"{row}\n"
        "| Discord | — | 1519435081708736513 |\n\n"
        "Recorded here for consistency.\n",
        encoding="utf-8",
    )
    return path


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

    def test_primary_sends_daily_with_no_suppression(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home  # primary: credentials_dir == HERMES_HOME
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()  # 1 day left -> in window
        _write_sidecar(entry_dir, recorded_at, identity="admin_person")
        entry = {"credentials_dir": entry_dir}
        _write_profile(vault_root, "admin_person", "111", is_primary=True)

        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs1 = mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("sent daily_warning" in line for line in logs1)
        assert len(sent) == 1

        # Run again "the next day" with the SAME sidecar (no re-auth
        # happened) -- primary must send AGAIN, no one-time suppression.
        now2 = now + timedelta(days=1)
        logs2 = mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now2, state=state,
        )
        assert any("sent daily_warning" in line for line in logs2)
        assert len(sent) == 2

    def test_family_member_sends_heads_up_exactly_once(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()  # in window, not yet revoked
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}
        _write_profile(vault_root, "family_person", "222")

        sent = _patch_common(mod, monkeypatch, check_ok=True)  # check_ok=True -> not revoked

        state: dict = {}
        logs1 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("sent heads_up" in line for line in logs1)
        assert len(sent) == 1

        # Re-run same day / next day, still not revoked, no new sidecar --
        # must NOT send again.
        now2 = now + timedelta(hours=12)
        logs2 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now2, state=state,
        )
        assert any("already sent one-time heads-up" in line for line in logs2)
        assert len(sent) == 1

    def test_family_member_sends_expired_exactly_once_when_revoked(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=8)).timestamp()  # already past 7 days
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}
        _write_profile(vault_root, "family_person", "222")

        sent = _patch_common(mod, monkeypatch, check_ok=False)  # revoked

        state: dict = {}
        logs1 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("sent expired" in line for line in logs1)
        assert len(sent) == 1

        logs2 = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now + timedelta(days=1), state=state,
        )
        assert any("already sent one-time EXPIRED" in line for line in logs2)
        assert len(sent) == 1


class TestFallbackNoSidecar:
    def test_no_sidecar_ok_check_is_silent(self, mod, hermes_home, vault_root, monkeypatch):
        entry = {"credentials_dir": hermes_home / "family_credentials" / "legacy_person"}
        _write_profile(vault_root, "legacy_person", "333")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=datetime.now(timezone.utc), state=state,
        )
        assert any("fallback reactive mode, --check OK" in line for line in logs)
        assert sent == []

    def test_no_sidecar_failed_check_sends_once_for_family_member(self, mod, hermes_home, vault_root, monkeypatch):
        entry = {"credentials_dir": hermes_home / "family_credentials" / "legacy_person"}
        _write_profile(vault_root, "legacy_person", "333")
        sent = _patch_common(mod, monkeypatch, check_ok=False)

        state: dict = {}
        now = datetime.now(timezone.utc)
        logs1 = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("sent reactive_expired_once" in line for line in logs1)
        assert len(sent) == 1

        logs2 = mod.process_identity(
            "legacy_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now + timedelta(days=1), state=state,
        )
        assert any("already sent one-time expired notice" in line for line in logs2)
        assert len(sent) == 1

    def test_no_sidecar_failed_check_sends_daily_for_primary(self, mod, hermes_home, vault_root, monkeypatch):
        entry = {"credentials_dir": hermes_home}
        _write_profile(vault_root, "admin_person", "111", is_primary=True)
        sent = _patch_common(mod, monkeypatch, check_ok=False)

        state: dict = {}
        now = datetime.now(timezone.utc)
        mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        mod.process_identity(
            "admin_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now + timedelta(days=1), state=state,
        )
        assert len(sent) == 2


class TestStateResetOnFreshReauth:
    def test_new_sidecar_timestamp_resets_one_time_flags(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "family_person"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()
        _write_sidecar(entry_dir, recorded_at, identity="family_person")
        entry = {"credentials_dir": entry_dir}
        _write_profile(vault_root, "family_person", "222")

        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert len(sent) == 1
        assert state["family_person"]["heads_up_sent_at"] is not None

        # Simulate a fresh re-auth: new sidecar timestamp recorded (as Part 3
        # would do after a successful --auth-code exchange), well outside
        # the window now.
        new_recorded_at = now.timestamp()
        _write_sidecar(entry_dir, new_recorded_at, identity="family_person")

        logs = mod.process_identity(
            "family_person", entry, hermes_home=hermes_home, vault_root=vault_root,
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
    """Vault-Profile.md-based resolution must skip loudly -- never guess,
    never misdeliver -- when the profile is missing or malformed."""

    def test_missing_profile_skips_without_guessing(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "mystery_person"
        now = datetime.now(timezone.utc)
        _write_sidecar(entry_dir, (now - timedelta(days=6)).timestamp(), identity="mystery_person")
        entry = {"credentials_dir": entry_dir}
        # Deliberately NOT writing a Profile.md for this identity.
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "mystery_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("SKIPPED" in line for line in logs)
        assert sent == []

    def test_malformed_telegram_row_skips_without_guessing(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "mystery_person"
        now = datetime.now(timezone.utc)
        _write_sidecar(entry_dir, (now - timedelta(days=6)).timestamp(), identity="mystery_person")
        entry = {"credentials_dir": entry_dir}
        # Telegram row present but the ID cell is the "not recorded yet"
        # placeholder, not a real numeric id.
        _write_profile(vault_root, "mystery_person", "—", table_row="| Telegram | — | — |")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "mystery_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("SKIPPED" in line for line in logs)
        assert sent == []

    def test_missing_telegram_row_entirely_skips(self, mod, hermes_home, vault_root, monkeypatch):
        entry_dir = hermes_home / "family_credentials" / "mystery_person"
        now = datetime.now(timezone.utc)
        _write_sidecar(entry_dir, (now - timedelta(days=6)).timestamp(), identity="mystery_person")
        entry = {"credentials_dir": entry_dir}
        path = _profile_path(vault_root, "mystery_person", is_primary=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nstatus: canonical\n---\n\n# Mystery Person\n\nNo platform table here.\n", encoding="utf-8")
        sent = _patch_common(mod, monkeypatch, check_ok=True)

        state: dict = {}
        logs = mod.process_identity(
            "mystery_person", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("SKIPPED" in line for line in logs)
        assert sent == []

    def test_new_identity_resolves_via_generic_family_path_convention(self, mod, hermes_home, vault_root, monkeypatch):
        """A brand-new identity ("brandnew") never referenced by name
        anywhere in this codebase must still resolve correctly, proving the
        Family/<Capitalized>/<Capitalized> Profile.md convention is
        mechanical, not a lookup table."""
        entry_dir = hermes_home / "family_credentials" / "brandnew"
        now = datetime.now(timezone.utc)
        recorded_at = (now - timedelta(days=6)).timestamp()
        _write_sidecar(entry_dir, recorded_at, identity="brandnew")
        entry = {"credentials_dir": entry_dir}
        _write_profile(vault_root, "brandnew", "999888777")

        sent = _patch_common(mod, monkeypatch, check_ok=True)
        state: dict = {}
        logs = mod.process_identity(
            "brandnew", entry, hermes_home=hermes_home, vault_root=vault_root,
            now=now, state=state,
        )
        assert any("sent heads_up" in line for line in logs)
        assert len(sent) == 1
        assert sent[0][0] == "999888777"


class TestFamilyDeliveryResolutionUnit:
    """Direct unit tests of scripts/_family_delivery.py."""

    def test_resolves_primary_identity_chat_id(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "admin_person", "111", is_primary=True)
        chat_id = fd.resolve_telegram_chat_id(
            "admin_person", is_primary=True, vault_root=vault_root, hermes_home=hermes_home,
        )
        assert chat_id == "111"

    def test_resolves_family_member_chat_id(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "zarkash", "5542989100")
        chat_id = fd.resolve_telegram_chat_id(
            "zarkash", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
        )
        assert chat_id == "5542989100"

    def test_profile_path_convention_for_family_member(self, fd, vault_root):
        path = fd.profile_path_for_identity("zarkash", is_primary=False, vault_root=vault_root)
        assert path == vault_root / "Hermes" / "Profile" / "Family" / "Zarkash" / "Zarkash Profile.md"

    def test_profile_path_convention_for_primary(self, fd, vault_root):
        path = fd.profile_path_for_identity("jid", is_primary=True, vault_root=vault_root)
        assert path == vault_root / "Hermes" / "Profile" / "JID Profile.md"

    def test_missing_profile_raises(self, fd, vault_root, hermes_home):
        with pytest.raises(fd.DeliveryTargetResolutionError):
            fd.resolve_telegram_chat_id(
                "nobody", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
            )

    def test_missing_telegram_row_raises(self, fd, vault_root, hermes_home):
        path = _profile_path(vault_root, "nobody", is_primary=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nstatus: canonical\n---\n\n# Nobody\n", encoding="utf-8")
        with pytest.raises(fd.DeliveryTargetResolutionError, match="no '\\| Telegram"):
            fd.resolve_telegram_chat_id(
                "nobody", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
            )

    def test_non_numeric_id_raises(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "nobody", "—", table_row="| Telegram | — | — |")
        with pytest.raises(fd.DeliveryTargetResolutionError, match="non-numeric"):
            fd.resolve_telegram_chat_id(
                "nobody", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
            )

    def test_config_yaml_mismatch_raises_rather_than_trusting_vault_alone(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "someone", "111222333")
        (hermes_home / "config.yaml").write_text(
            "telegram:\n  allow_from:\n    - '999999999'\n", encoding="utf-8",
        )
        with pytest.raises(fd.DeliveryTargetResolutionError, match="NOT present"):
            fd.resolve_telegram_chat_id(
                "someone", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
            )

    def test_config_yaml_match_succeeds(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "someone", "111222333")
        (hermes_home / "config.yaml").write_text(
            "telegram:\n  allow_from:\n    - '111222333'\n", encoding="utf-8",
        )
        chat_id = fd.resolve_telegram_chat_id(
            "someone", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
        )
        assert chat_id == "111222333"

    def test_no_config_yaml_skips_cross_check_but_still_resolves(self, fd, vault_root, hermes_home):
        _write_profile(vault_root, "someone", "111222333")
        # hermes_home exists but has no config.yaml at all.
        chat_id = fd.resolve_telegram_chat_id(
            "someone", is_primary=False, vault_root=vault_root, hermes_home=hermes_home,
        )
        assert chat_id == "111222333"


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
