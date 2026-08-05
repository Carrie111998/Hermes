"""Regression tests for wave-1 s1 extraction (clusters c22 + c3).

Covers the module-level functions moved out of gateway/run.py into
gateway/restart_mixin.py (c22) and gateway/hygiene_mixin.py (c3) by the
blind implementer w1b.  The functions are re-imported into gateway.run, so
both the public contract (``from gateway.run import ...``) and the new
module paths are exercised.  Deferred ``from gateway.run import ...`` lines
inside the moved bodies mean monkeypatching gateway.run module attributes
still works exactly as before the extraction.
"""

import time

import pytest

import gateway.run as gateway_run
from gateway.hygiene_mixin import (
    _record_hygiene_cooldown,
    _seed_hygiene_system_prompt,
)
from gateway.restart_mixin import (
    _clear_planned_restart_notification,
    _planned_restart_notification_path,
    _planned_restart_notification_pending,
    _restart_notification_pending,
)
from gateway.run import (
    _clear_planned_restart_notification as run_clear_restart,
    _planned_restart_notification_path as run_planned_path,
    _planned_restart_notification_pending as run_planned_pending,
    _record_hygiene_cooldown as run_record_cooldown,
    _restart_notification_pending as run_restart_pending,
    _seed_hygiene_system_prompt as run_seed_prompt,
)


# ---------------------------------------------------------------------------
# c22 — /restart notification marker helpers
# ---------------------------------------------------------------------------

class TestRestartNotificationMarkers:
    def test_no_marker_not_pending(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        assert _restart_notification_pending() is False

    def test_marker_makes_pending(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        (tmp_path / ".restart_notify.json").write_text("{}")
        assert _restart_notification_pending() is True

    def test_planned_path_under_hermes_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        assert _planned_restart_notification_path() == tmp_path / ".restart_pending.json"

    def test_planned_pending_false_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        assert _planned_restart_notification_pending() is False

    def test_clear_removes_planned_marker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        (tmp_path / ".restart_pending.json").write_text("{}")
        assert _planned_restart_notification_pending() is True
        _clear_planned_restart_notification()
        assert _planned_restart_notification_pending() is False

    def test_clear_missing_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        # unlink(missing_ok=True) must not raise when no marker exists.
        _clear_planned_restart_notification()

    def test_run_reexports_identity(self):
        # The public contract: gateway.run still exposes the same function objects.
        assert gateway_run._restart_notification_pending is _restart_notification_pending
        assert gateway_run._planned_restart_notification_path is _planned_restart_notification_path
        assert gateway_run._planned_restart_notification_pending is _planned_restart_notification_pending
        assert gateway_run._clear_planned_restart_notification is _clear_planned_restart_notification

    def test_run_contract_via_reexport(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        (tmp_path / ".restart_notify.json").write_text("{}")
        assert run_restart_pending() is True
        assert run_planned_path() == tmp_path / ".restart_pending.json"
        assert run_planned_pending() is False
        (tmp_path / ".restart_pending.json").write_text("{}")
        assert run_planned_pending() is True
        run_clear_restart()
        assert run_planned_pending() is False


# ---------------------------------------------------------------------------
# c3 — session-hygiene compression helpers
# ---------------------------------------------------------------------------

class _StubAgent:
    def __init__(self):
        self._cached_system_prompt = None


class TestSeedHygieneSystemPrompt:
    def test_seeds_stored_prompt(self):
        agent = _StubAgent()
        row = {"system_prompt": "  the persisted prompt  "}
        assert _seed_hygiene_system_prompt(agent, row) is True
        assert agent._cached_system_prompt == "  the persisted prompt  "

    def test_blank_prompt_seeds_empty(self):
        agent = _StubAgent()
        assert _seed_hygiene_system_prompt(agent, {"system_prompt": "   "}) is False
        assert agent._cached_system_prompt == ""

    def test_non_dict_row_seeds_empty(self):
        agent = _StubAgent()
        assert _seed_hygiene_system_prompt(agent, None) is False
        assert agent._cached_system_prompt == ""

    def test_run_reexports_identity(self):
        assert gateway_run._seed_hygiene_system_prompt is _seed_hygiene_system_prompt
        assert run_seed_prompt is _seed_hygiene_system_prompt


class TestRecordHygieneCooldown:
    def test_records_cooldown(self):
        captured = {}

        class Recorder:
            def record_compression_failure_cooldown(self, session_id, ts):
                captured["session_id"] = session_id
                captured["ts"] = ts

        class SessionDb:
            _db = Recorder()

        class Gateway:
            _session_db = SessionDb()

        before = time.time()
        _record_hygiene_cooldown(Gateway(), "sess-1", 30.0)
        assert captured["session_id"] == "sess-1"
        assert captured["ts"] >= before + 30.0

    def test_no_session_db_is_noop(self):
        class Gateway:
            _session_db = None

        # Must not raise.
        _record_hygiene_cooldown(Gateway(), "sess-1", 30.0)

    def test_missing_recorder_is_noop(self):
        class SessionDb:
            pass

        class Gateway:
            _session_db = SessionDb()

        # No record_compression_failure_cooldown attr -> silent no-op.
        _record_hygiene_cooldown(Gateway(), "sess-1", 30.0)

    def test_recorder_exception_swallowed(self):
        class Recorder:
            def record_compression_failure_cooldown(self, session_id, ts):
                raise RuntimeError("boom")

        class SessionDb:
            _db = Recorder()

        class Gateway:
            _session_db = SessionDb()

        # logger.debug fallback; must not raise.
        _record_hygiene_cooldown(Gateway(), "sess-1", 30.0)

    def test_run_reexports_identity(self):
        assert gateway_run._record_hygiene_cooldown is _record_hygiene_cooldown
        assert run_record_cooldown is _record_hygiene_cooldown
