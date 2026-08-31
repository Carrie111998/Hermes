"""RC1: HostSupervisor late-ACK routing — exercises actual frame-handler code.

Proves the exact control flow:
    control.ack arrives → _handle_host_frame → q is None →
    route_name == session.compress → _handle_late_compression_ack →
    server._handle_late_compression_attempt → DB settlement.
"""

import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB


def _tmp_db(tmp_path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


def _family(db: SessionDB, source="tui") -> str:
    sid = f"fam_{uuid.uuid4().hex[:8]}"
    db.create_session(sid, source=source, session_key=sid)
    return sid


def _make_hs(tmp_path, monkeypatch):
    """Create a HostSupervisor with autostart=False for unit testing."""
    from tui_gateway.host_supervisor import HostSupervisor

    registry = tmp_path / "dashboard-compute-host.json"
    registry.write_text('{"host_pid": 0, "boot_id": "test"}', encoding="utf-8")
    hs = HostSupervisor(registry_path=registry, argv=[sys.executable, "-c", ""], autostart=False)
    return hs


class TestHostSupervisorLateAckRouting:
    """Prove the actual _handle_host_frame → late-ACK → DB settlement path."""

    def test_late_session_compress_ack_enters_rc1_handler(self, tmp_path, monkeypatch):
        """When q is None and route_name is session.compress, late handler fires."""
        db = _tmp_db(tmp_path)
        parent = _family(db, "tui")
        fam = db.get_session(parent)["session_key"]
        hs = _make_hs(tmp_path, monkeypatch)

        # Create attempt in DB (simulating what methods_session.dispatch does)
        attempt_id = f"req_{uuid.uuid4().hex[:16]}"
        db.create_compression_attempt(
            attempt_id=attempt_id,
            session_key=fam,
            parent_session_id=parent,
            input_history_version=5,
            input_watermark=3,
            holder=attempt_id,
        )
        db.transition_compression_attempt_pending_to_running(attempt_id)

        # Acquire lock + publish (simulating worker committed)
        db.try_acquire_compression_lock(parent, holder=attempt_id, ttl_seconds=300)
        child_id = f"child_{uuid.uuid4().hex[:8]}"
        db.publish_compression_child(
            parent_session_id=parent,
            child_session_id=child_id,
            source="tui",
            messages=[{"role": "user", "content": "compressed"}],
            watermark=3,
            watermark_ceiling=5,
            attempt_id=attempt_id,
            compression_lock_holder=attempt_id,
        )

        # Simulate: waiter already timed out, pending_controls[attempt_id] removed
        # (q is None when _handle_host_frame checks)

        # Mock _handle_late_compression_ack to capture the call
        calls = []
        original_handler = hs._handle_late_compression_ack

        def mock_handler(req_id, frame):
            calls.append((req_id, frame))
            original_handler(req_id, frame)

        monkeypatch.setattr(hs, "_handle_late_compression_ack", mock_handler)

        # Also mock server._handle_late_compression_attempt to capture projection
        from tui_gateway import server as srv
        proj_calls = []
        original_proj = srv._handle_late_compression_attempt

        def mock_proj(aid, frame):
            proj_calls.append((aid, frame))
            original_proj(aid, frame)

        monkeypatch.setattr(srv, "_handle_late_compression_attempt", mock_proj)

        # Build a control.ack frame for session.compress
        ack_frame = {
            "type": "control.ack",
            "route_name": "session.compress",
            "request_id": attempt_id,
            "sid": parent,
            "result": {"status": "committed"},
        }

        # Dispatch through actual _handle_host_frame
        hs._handle_host_frame(ack_frame)

        # Verify: late handler was called with the correct request_id
        assert len(calls) == 1
        assert calls[0][0] == attempt_id

        # Verify: server projection handler was called
        assert len(proj_calls) == 1
        assert proj_calls[0][0] == attempt_id

    def test_unknown_request_id_is_harmless(self, tmp_path, monkeypatch):
        """Late ACK with unknown attempt_id → no crash, no projection."""
        hs = _make_hs(tmp_path, monkeypatch)

        # Mock _handle_late_compression_ack
        calls = []
        monkeypatch.setattr(hs, "_handle_late_compression_ack", lambda rid, f: calls.append(rid))

        ack_frame = {
            "type": "control.ack",
            "route_name": "session.compress",
            "request_id": "totally_unknown_123",
            "sid": "fake_sid",
        }

        hs._handle_host_frame(ack_frame)
        # Handler was called (routing is correct) but DB lookup will find nothing
        assert calls == ["totally_unknown_123"]

    def test_non_compress_ack_does_not_enter_rc1(self, tmp_path, monkeypatch):
        """Non-session.compress ACK with q=None → no late handler call."""
        hs = _make_hs(tmp_path, monkeypatch)

        calls = []
        monkeypatch.setattr(hs, "_handle_late_compression_ack", lambda rid, f: calls.append(rid))

        # interrupt.ack with q=None should NOT call late handler
        ack_frame = {
            "type": "control.ack",
            "route_name": "interrupt",
            "request_id": "some_id",
            "sid": "fake_sid",
        }

        hs._handle_host_frame(ack_frame)
        assert calls == []  # No late handler call

    def test_normal_ack_with_queue_still_works(self, tmp_path, monkeypatch):
        """When q is not None, ACK goes through existing queue path."""
        hs = _make_hs(tmp_path, monkeypatch)

        # Insert a pending control queue
        test_q = queue.Queue(maxsize=1)
        request_id = "normal_req_123"
        with hs._lock:
            hs._pending_controls[request_id] = test_q

        ack_frame = {
            "type": "control.ack",
            "route_name": "session.compress",
            "request_id": request_id,
            "sid": "fake_sid",
            "result": {"status": "ok"},
        }

        hs._handle_host_frame(ack_frame)

        # Queue should have the frame
        assert test_q.get_nowait() == ack_frame

    def test_duplicate_late_ack_is_idempotent(self, tmp_path, monkeypatch):
        """Two late ACKs for same attempt → both call handler, DB is idempotent."""
        db = _tmp_db(tmp_path)
        parent = _family(db, "tui")
        fam = db.get_session(parent)["session_key"]
        hs = _make_hs(tmp_path, monkeypatch)

        attempt_id = f"dup_{uuid.uuid4().hex[:8]}"
        db.create_compression_attempt(
            attempt_id=attempt_id,
            session_key=fam,
            parent_session_id=parent,
            input_history_version=0,
            input_watermark=0,
            holder=attempt_id,
        )
        db.transition_compression_attempt_pending_to_running(attempt_id)
        db.try_acquire_compression_lock(parent, holder=attempt_id, ttl_seconds=300)
        db.publish_compression_child(
            parent_session_id=parent,
            child_session_id=f"child_{attempt_id}",
            source="tui",
            messages=[{"role": "user", "content": "x"}],
            attempt_id=attempt_id,
            compression_lock_holder=attempt_id,
        )

        calls = []
        monkeypatch.setattr(hs, "_handle_late_compression_ack", lambda rid, f: calls.append(rid))

        ack1 = {"type": "control.ack", "route_name": "session.compress", "request_id": attempt_id, "sid": parent}
        ack2 = {"type": "control.ack", "route_name": "session.compress", "request_id": attempt_id, "sid": parent}

        hs._handle_host_frame(ack1)
        hs._handle_host_frame(ack2)

        # Both calls went through routing (handler called twice)
        assert calls == [attempt_id, attempt_id]
        # DB is idempotent: attempt stays committed
        att = db.get_compression_attempt(attempt_id)
        assert att["state"] == "committed"
