"""Tests for the external drain-control marker contract + gateway state machine.

Task 2.2/2.3. Two layers:
  * drain_control.py — the presence-based marker contract (write/clear/read,
    HERMES_HOME-scoped, never-raises).
  * GatewayRunner enter/exit/watcher + the new-turn accept gate — the
    reversible state machine driven by the marker.

Mocked tests are necessary-not-sufficient here (the HARD live-validation gate,
Q-B, exercises a real `hermes gateway run`); these lock the unit contract.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gateway.drain_control as dc
from gateway.run import GatewayRunner
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


# ---------------------------------------------------------------------------
# Marker contract (drain_control.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestMarkerContract:
    def test_absent_by_default(self, home):
        assert dc.drain_requested() is False
        assert dc.read_drain_request() is None

    def test_write_then_present(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert dc.drain_requested() is True
        assert payload["action"] == "drain"
        assert payload["principal"] == "nas"
        body = dc.read_drain_request()
        assert body is not None and body["principal"] == "nas"

    def test_clear_removes(self, home):
        dc.write_drain_request()
        assert dc.clear_drain_request() is True
        assert dc.drain_requested() is False
        # idempotent: clearing again is a no-op, returns False
        assert dc.clear_drain_request() is False

    def test_path_respects_hermes_home(self, home):
        assert dc.drain_request_path() == home / ".drain_request.json"
        assert dc.drain_request_lock_path() == home / ".drain_request.lock"

    def test_corrupt_marker_reads_as_present_contentless(self, home):
        # A half-written / malformed marker must still count as "drain active"
        # (fail-safe toward quiescing).
        dc.drain_request_path().write_text("{not valid json", encoding="utf-8")
        assert dc.drain_requested() is True
        assert dc.read_drain_request() == {}

    def test_write_is_atomic_json(self, home):
        dc.write_drain_request(principal="x")
        import json

        data = json.loads(dc.drain_request_path().read_text())
        assert data["action"] == "drain"

    def test_write_and_clear_share_one_mutation_lock(
        self,
        home,
        monkeypatch,
    ):
        entered_write = threading.Event()
        release_write = threading.Event()
        clear_finished = threading.Event()
        original = dc.atomic_json_write

        def blocked_write(path, payload):
            entered_write.set()
            assert release_write.wait(timeout=5)
            original(path, payload)

        monkeypatch.setattr(dc, "atomic_json_write", blocked_write)
        writer = threading.Thread(target=dc.write_drain_request)
        writer.start()
        assert entered_write.wait(timeout=5)

        clearer = threading.Thread(
            target=lambda: (
                dc.clear_drain_request(),
                clear_finished.set(),
            )
        )
        clearer.start()
        assert clear_finished.wait(timeout=0.1) is False
        release_write.set()
        writer.join(timeout=5)
        clearer.join(timeout=5)

        assert writer.is_alive() is False
        assert clearer.is_alive() is False
        assert clear_finished.is_set()
        assert dc.drain_request_path().exists() is False

    def test_lock_identity_fallback_does_not_require_geteuid(
        self,
        home,
        monkeypatch,
    ):
        monkeypatch.delattr(dc.os, "geteuid", raising=False)
        payload = dc.write_drain_request(principal="portable")
        assert payload["principal"] == "portable"

    def test_transaction_held_marker_rejects_generic_mutation(self, home):
        capability = "release-only-capability-preimage-32"
        transaction_sha256 = "a" * 64
        held = dc.write_drain_request(
            principal="offline-release",
            mutation_capability=capability,
            hold_transaction_sha256=transaction_sha256,
        )
        assert held["held_transaction_sha256"] == transaction_sha256
        assert "release-only-capability" not in dc.drain_request_path().read_text()
        original = dc.drain_request_path().read_bytes()

        with pytest.raises(PermissionError, match="transaction-held"):
            dc.write_drain_request(principal="dashboard")
        with pytest.raises(PermissionError, match="transaction-held"):
            dc.clear_drain_request()

        assert dc.drain_request_path().read_bytes() == original

    def test_transaction_held_marker_requires_matching_capability(self, home):
        capability = "root-manifest-preimage-at-least-32"
        transaction_sha256 = "b" * 64
        dc.write_drain_request(
            principal="offline-release",
            mutation_capability=capability,
            hold_transaction_sha256=transaction_sha256,
        )
        with pytest.raises(PermissionError, match="transaction-held"):
            dc.write_drain_request(
                principal="offline-release",
                mutation_capability="wrong-capability-preimage-at-least-32",
                hold_transaction_sha256=transaction_sha256,
            )
        refreshed = dc.write_drain_request(
            principal="offline-release",
            mutation_capability=capability,
        )
        assert refreshed["held_transaction_sha256"] == transaction_sha256
        assert dc.clear_drain_request(mutation_capability=capability) is True

    def test_transaction_held_marker_cannot_be_rebound(self, home):
        capability = "one-transaction-only-preimage-32xx"
        dc.write_drain_request(
            mutation_capability=capability,
            hold_transaction_sha256="c" * 64,
        )
        with pytest.raises(PermissionError, match="another transaction"):
            dc.write_drain_request(
                mutation_capability=capability,
                hold_transaction_sha256="d" * 64,
            )

    def test_malformed_held_binding_fails_closed(self, home):
        dc.drain_request_path().write_text(
            '{"action":"drain","held_transaction_sha256":"'
            + "e" * 64
            + '"}',
            encoding="utf-8",
        )
        dc.drain_request_path().chmod(0o600)
        with pytest.raises(PermissionError, match="malformed held"):
            dc.write_drain_request()
        with pytest.raises(PermissionError, match="malformed held"):
            dc.clear_drain_request()

    @pytest.mark.parametrize("raw", ("{not-json", "{}"))
    def test_present_corrupt_marker_mutation_fails_closed(self, home, raw):
        dc.drain_request_path().write_text(raw, encoding="utf-8")
        dc.drain_request_path().chmod(0o600)
        with pytest.raises(PermissionError, match="malformed drain"):
            dc.write_drain_request()
        with pytest.raises(PermissionError, match="malformed drain"):
            dc.clear_drain_request()

    @pytest.mark.parametrize("kind", ("symlink", "directory"))
    def test_unsafe_marker_identity_mutation_fails_closed(
        self,
        home,
        kind,
    ):
        path = dc.drain_request_path()
        if kind == "symlink":
            target = home / "target.json"
            target.write_text('{"action":"drain"}', encoding="utf-8")
            path.symlink_to(target)
        else:
            path.mkdir()
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.write_drain_request()
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.clear_drain_request()

    def test_marker_read_error_mutation_fails_closed(
        self,
        home,
        monkeypatch,
    ):
        dc.write_drain_request()
        path = dc.drain_request_path()
        original_open = dc.os.open

        def selective_open(target, *args, **kwargs):
            if Path(target) == path:
                raise OSError("synthetic unreadable marker")
            return original_open(target, *args, **kwargs)

        monkeypatch.setattr(dc.os, "open", selective_open)
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.write_drain_request()
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.clear_drain_request()

    @pytest.mark.parametrize("mode", (0o640, 0o666))
    def test_tampered_marker_mode_mutation_fails_closed(
        self,
        home,
        mode,
    ):
        dc.write_drain_request()
        dc.drain_request_path().chmod(mode)
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.write_drain_request()
        with pytest.raises(PermissionError, match="cannot be verified"):
            dc.clear_drain_request()

    def test_marker_extended_metadata_mutation_fails_closed(self, home):
        setxattr = getattr(dc.os, "setxattr", None)
        removexattr = getattr(dc.os, "removexattr", None)
        if setxattr is None or removexattr is None:
            pytest.skip("extended attributes unavailable")
        dc.write_drain_request()
        path = dc.drain_request_path()
        try:
            setxattr(path, "user.muncho-test", b"tampered")
        except OSError:
            pytest.skip("filesystem does not support user xattrs")
        try:
            with pytest.raises(PermissionError, match="metadata"):
                dc.write_drain_request()
            with pytest.raises(PermissionError, match="metadata"):
                dc.clear_drain_request()
        finally:
            removexattr(path, "user.muncho-test")

    def test_hold_capability_requires_minimum_entropy_shape(self, home):
        with pytest.raises(ValueError, match="capability"):
            dc.write_drain_request(
                mutation_capability="too-short",
                hold_transaction_sha256="f" * 64,
            )

    def test_active_observation_binds_exact_held_marker_bytes(
        self,
        home,
        monkeypatch,
    ):
        monkeypatch.setattr(
            dc,
            "current_instantiation_epoch",
            lambda: "boot-id:123",
        )
        capability = "release-only-observation-capability"
        transaction_sha256 = "a" * 64
        dc.write_drain_request(
            principal="offline-release",
            mutation_capability=capability,
            hold_transaction_sha256=transaction_sha256,
        )
        raw = dc.drain_request_path().read_bytes()

        assert dc.active_drain_observation() == {
            "marker_sha256": hashlib.sha256(raw).hexdigest(),
            "transaction_sha256": transaction_sha256,
            "mutation_capability_sha256": hashlib.sha256(
                capability.encode("utf-8")
            ).hexdigest(),
            "epoch": "boot-id:123",
        }

    def test_active_observation_rejects_stale_generic_and_duplicate_markers(
        self,
        home,
        monkeypatch,
    ):
        monkeypatch.setattr(
            dc,
            "current_instantiation_epoch",
            lambda: "boot-id:current",
        )
        path = dc.drain_request_path()
        path.write_text(
            json.dumps({
                "action": "drain",
                "epoch": "boot-id:stale",
                "held_transaction_sha256": "a" * 64,
                "held_mutation_capability_sha256": "b" * 64,
            }),
            encoding="utf-8",
        )
        assert dc.active_drain_observation() is None

        path.write_text(
            json.dumps({
                "action": "drain",
                "epoch": "boot-id:current",
            }),
            encoding="utf-8",
        )
        assert dc.active_drain_observation() is None

        path.write_text(
            '{"action":"drain","epoch":"boot-id:current",'
            '"epoch":"boot-id:current","held_transaction_sha256":"'
            + "a" * 64
            + '","held_mutation_capability_sha256":"'
            + "b" * 64
            + '"}',
            encoding="utf-8",
        )
        assert dc.active_drain_observation() is None


class TestSuppressNotification:
    """The generic suppress_notification flag on the drain marker.

    Gates ONLY the gateway's home-channel shutdown broadcast (NAS auto-update
    sets it true). Default-false so legacy/operator drains behave as before.
    The reader reuses the NS-570 epoch-staleness check so an orphaned marker
    can never silence a fresh gateway.
    """

    def test_default_false(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert payload["suppress_notification"] is False
        assert dc.drain_notification_suppressed() is False

    def test_flag_round_trips_true(self, home):
        payload = dc.write_drain_request(principal="nas", suppress_notification=True)
        assert payload["suppress_notification"] is True
        body = dc.read_drain_request()
        assert body is not None and body["suppress_notification"] is True
        assert dc.drain_notification_suppressed() is True


# ---------------------------------------------------------------------------
# Instantiation-epoch staleness (NS-570: orphaned marker on durable volume)
# ---------------------------------------------------------------------------


class TestInstantiationEpoch:
    def test_write_stamps_current_epoch(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert payload["epoch"] == dc.current_instantiation_epoch()
        body = dc.read_drain_request()
        assert body is not None and body["epoch"] == dc.current_instantiation_epoch()


    def test_marker_from_prior_instantiation_reads_as_absent(self, home, monkeypatch):
        # THE NS-570 REGRESSION. A begin-drain marker written by a PREVIOUS
        # container/VM instantiation survives on the durable HERMES_HOME volume
        # across a machine restart. The freshly-restarted gateway (new epoch)
        # must treat it as absent, NOT re-engage drain.
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-OLD")
        dc.write_drain_request(principal="nas")  # stamps "epoch-OLD"
        assert dc.drain_requested() is True  # same epoch → active

        # Simulate the restart: a brand-new instantiation epoch.
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-NEW")
        # The marker file is still physically present on the volume…
        assert dc.drain_request_path().exists() is True
        # …but it is ignored because its epoch belongs to a prior instantiation.
        assert dc.drain_requested() is False


    def test_current_epoch_empty_when_proc_unreadable(self, monkeypatch):
        # When neither /proc identity source is readable, the epoch is "" so
        # the staleness check is disabled rather than crashing.
        from pathlib import Path as _P

        orig_read_text = _P.read_text

        def _boom(self, *a, **k):
            if str(self).startswith("/proc/"):
                raise OSError("no /proc")
            return orig_read_text(self, *a, **k)

        dc.current_instantiation_epoch.cache_clear()
        monkeypatch.setattr(_P, "read_text", _boom)
        try:
            assert dc.current_instantiation_epoch() == ""
        finally:
            dc.current_instantiation_epoch.cache_clear()


# ---------------------------------------------------------------------------
# Gateway state machine (enter / exit / idempotency)
# ---------------------------------------------------------------------------


def _drain_runner():
    runner, adapter = make_restart_runner()
    runner._external_drain_active = False
    runner._external_drain_ack_marker_sha256 = ""
    runner._external_drain_ack_sequence = 0
    # Bind the real methods under test.
    runner._enter_external_drain = GatewayRunner._enter_external_drain.__get__(
        runner, GatewayRunner
    )
    runner._build_external_drain_ack = (
        GatewayRunner._build_external_drain_ack.__get__(
            runner,
            GatewayRunner,
        )
    )
    runner._exit_external_drain = GatewayRunner._exit_external_drain.__get__(
        runner, GatewayRunner
    )
    return runner, adapter


class TestDrainStateMachine:


    def test_enter_idempotent(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        runner._enter_external_drain()  # second call — no-op
        runner._update_runtime_status.assert_not_called()

    def test_exit_reverts_to_running(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        runner._exit_external_drain()
        assert runner._external_drain_active is False
        runner._update_runtime_status.assert_called_with(
            "running",
            external_drain_ack=None,
        )

    def test_gateway_ack_sequence_advances_and_resets_per_marker(
        self,
        monkeypatch,
    ):
        runner, _ = _drain_runner()
        invocation_id = "1" * 32
        pid = os.getpid()
        proc_path = f"/proc/{pid}/stat"
        process_stat = (
            f"{pid} (gateway) S "
            + " ".join(["0"] * 18 + ["4242"])
        )
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if str(path) == proc_path:
                return process_stat
            return original(path, *args, **kwargs)

        monkeypatch.setenv("INVOCATION_ID", invocation_id)
        monkeypatch.setattr(Path, "read_text", read_text)
        base = {
            "transaction_sha256": "a" * 64,
            "mutation_capability_sha256": "b" * 64,
            "epoch": "boot-id:123",
        }

        first = runner._build_external_drain_ack({
            **base,
            "marker_sha256": "c" * 64,
        })
        second = runner._build_external_drain_ack({
            **base,
            "marker_sha256": "c" * 64,
        })
        replacement = runner._build_external_drain_ack({
            **base,
            "marker_sha256": "d" * 64,
        })

        assert first == {
            **base,
            "marker_sha256": "c" * 64,
            "process_start_ticks": "4242",
            "systemd_invocation_id": invocation_id,
            "ack_sequence": 1,
        }
        assert second is not None and second["ack_sequence"] == 2
        assert replacement is not None
        assert replacement["ack_sequence"] == 1

    def test_exit_idempotent_when_not_draining(self):
        runner, _ = _drain_runner()
        runner._exit_external_drain()  # never entered — no-op
        runner._update_runtime_status.assert_not_called()

    def test_exit_during_shutdown_does_not_revert_to_running(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        # A shutdown drain is now in progress — exit must NOT resurrect running.
        runner._draining = True
        runner._exit_external_drain()
        assert runner._external_drain_active is False
        runner._update_runtime_status.assert_not_called()


# ---------------------------------------------------------------------------
# Watcher reconciliation
# ---------------------------------------------------------------------------


class TestDrainWatcher:

    @pytest.mark.asyncio
    async def test_watcher_enters_then_exits_with_marker(self, home):
        runner, _ = _drain_runner()
        runner._drain_control_watcher = GatewayRunner._drain_control_watcher.__get__(
            runner, GatewayRunner
        )
        # Drive a few ticks manually rather than spinning the loop.
        dc.write_drain_request()
        task = asyncio.create_task(runner._drain_control_watcher(interval=0.02))
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is True
        dc.clear_drain_request()
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is False
        runner._running = False
        await asyncio.sleep(0.04)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# New-turn accept gate
# ---------------------------------------------------------------------------


class TestNewTurnGate:
    @pytest.mark.asyncio
    async def test_new_turn_refused_during_external_drain(self):
        runner, _ = _drain_runner()
        runner._external_drain_active = True
        event = MessageEvent(
            text="hello",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            message_id="m1",
        )
        result = await runner._handle_message(event)
        assert result is not None
        assert "draining" in result.lower()
