"""A spawn ledger that cannot be read is not an empty fleet (#99450 R2-1).

``ledger_entries()`` used to collapse three different states into one
answer: "no serve/dashboard backends are registered", "the ledger file is
corrupt", and "the ledger file cannot be read at all" all returned ``[]``.
The update's runtime inventory consumed that list, saw no rows, recorded
no discovery error, and authorized a checkout mutation while a live
``hermes serve`` kept importing from it.

These tests use REAL ledger files on disk — corrupt bytes, and a path that
genuinely cannot be read as a file — rather than a monkeypatched raiser,
because the failure lived in the reader's own error handling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import hermes_cli.process_identity as pi
import hermes_cli.update_inventory as ui
from hermes_cli import update_quiesce


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Point the machine spawn ledger at a real file under ``tmp_path``."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    return root / pi.LEDGER_FILENAME


@pytest.fixture()
def quiet_fleet(tmp_path, monkeypatch):
    """Every inventory probe except the ledger answers, and finds nothing."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: home / "profiles"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._PROFILE_ID_RE",
        re.compile(r"^[a-z0-9][a-z0-9_-]*$"),
        raising=False,
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr(
        "hermes_cli.gateway._get_service_pids", lambda all_profiles=False: set()
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes",
        lambda exclude_pids=None: [],
    )
    monkeypatch.setattr("hermes_cli.gateway.find_windows_gateway_services", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": "a" * 40, "version": "1.0"},
    )
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *a, **k: "git")
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)


def _ledger_errors(plan) -> list[str]:
    return [e for e in plan.discovery_errors if "ledger" in e.lower()]


# ---------------------------------------------------------------------------
# The reader itself
# ---------------------------------------------------------------------------


class TestLedgerReaderDistinguishesEmptyFromUnreadable:
    def test_missing_ledger_is_positively_empty(self, ledger):
        assert pi.ledger_entries(strict=True) == []

    def test_empty_roster_is_positively_empty(self, ledger):
        ledger.write_text("[]", encoding="utf-8")
        assert pi.ledger_entries(strict=True) == []

    def test_corrupt_ledger_raises_instead_of_reading_as_empty(self, ledger):
        ledger.write_text("{not json at all", encoding="utf-8")

        # A caller about to authorize a mutation is told.
        with pytest.raises(pi.LedgerUnreadable):
            pi.ledger_entries(strict=True)
        # Lenient callers (startup reapers) keep the degraded behaviour:
        # quarantine the damage, answer with an empty roster.
        assert pi.ledger_entries() == []

    def test_unreadable_ledger_raises_instead_of_reading_as_empty(self, ledger):
        # A directory where the ledger file belongs: a real OSError from the
        # real reader, on every OS and as any user (including root).
        ledger.mkdir()
        with pytest.raises(pi.LedgerUnreadable):
            pi.ledger_entries(strict=True)

    def test_strict_read_does_not_quarantine_the_evidence(self, ledger):
        """A retry must fail closed too — quarantining would fake an empty
        roster on the second run."""
        ledger.write_text("{not json at all", encoding="utf-8")
        with pytest.raises(pi.LedgerUnreadable):
            pi.ledger_entries(strict=True)
        assert ledger.is_file(), "strict reads must leave the file in place"
        with pytest.raises(pi.LedgerUnreadable):
            pi.ledger_entries(strict=True)


# ---------------------------------------------------------------------------
# Inventory → quiesce wiring
# ---------------------------------------------------------------------------


class TestInventoryFailsClosedOnAnUnreadableLedger:
    def test_positively_empty_ledger_authorizes_mutation(self, ledger, quiet_fleet):
        ledger.write_text("[]", encoding="utf-8")
        plan = ui.collect_runtime_inventory()
        assert plan.discovery_errors == []
        assert update_quiesce.verify_inventory_complete(plan) == []

    def test_corrupt_ledger_records_a_discovery_error(self, ledger, quiet_fleet):
        ledger.write_text("{not json at all", encoding="utf-8")
        plan = ui.collect_runtime_inventory()
        assert _ledger_errors(plan), plan.discovery_errors

    def test_corrupt_ledger_refuses_the_mutation(self, ledger, quiet_fleet):
        ledger.write_text("{not json at all", encoding="utf-8")
        plan = ui.collect_runtime_inventory()
        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_quiesce.verify_inventory_complete(plan)
        assert "ledger" in str(excinfo.value).lower()

    def test_unreadable_ledger_refuses_the_mutation(self, ledger, quiet_fleet):
        ledger.mkdir()
        plan = ui.collect_runtime_inventory()
        assert _ledger_errors(plan), plan.discovery_errors
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.verify_inventory_complete(plan)

    def test_a_truncated_ledger_is_not_mistaken_for_no_backends(
        self, ledger, quiet_fleet
    ):
        """The realistic corruption: a write cut short mid-array."""
        full = json.dumps(
            [
                {
                    "pid": 4321,
                    "create_time": 111.0,
                    "purpose": "serve",
                    "install": pi.install_id(),
                    "registered_at": 222.0,
                    "argv": "hermes serve",
                }
            ]
        )
        ledger.write_text(full[: len(full) // 2], encoding="utf-8")
        plan = ui.collect_runtime_inventory()
        assert plan.runtimes == []
        assert _ledger_errors(plan), (
            "a half-written ledger reads as zero rows — the inventory must "
            "say it could not look, not that nothing is running"
        )
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.verify_inventory_complete(plan)


def test_lenient_callers_keep_quarantining_corruption(ledger):
    """The startup reapers must still self-heal a corrupt ledger."""
    ledger.write_text("{not json at all", encoding="utf-8")
    assert pi.ledger_entries() == []
    assert not ledger.is_file()
    assert Path(str(ledger) + ".corrupt").is_file()
